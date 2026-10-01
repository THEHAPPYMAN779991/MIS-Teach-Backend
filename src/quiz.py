from flask import jsonify, request, Blueprint, current_app, Response
import uuid
from accessories import mongo, sqldb, refresh_token
from src.api import get_user_info, verify_token
import jwt
from datetime import datetime
import random
import base64
import os
import json
import re
import ast
from sqlalchemy import text
from bson import ObjectId
from src.grade_answer import batch_grade_ai_questions
from src.question_concept_mapper import ensure_question_concept_mapping
import time
import hashlib
import logging
from typing import Any, Dict, List

quiz_bp = Blueprint('quiz', __name__)

# 設置日誌
logger = logging.getLogger(__name__)


DEFAULT_QUESTION_SOURCE = os.getenv("MIS_DEFAULT_QUESTION_SOURCE", "test5")
QUESTION_SOURCE_ENV = "MIS_QUESTION_SOURCES"


def _normalize_question_source(raw_source=None) -> str:
    """Normalize a Mongo question source.

    Accepted formats:
    - collection
    - database.collection
    """
    source = str(raw_source or DEFAULT_QUESTION_SOURCE or "test5").strip()
    if not source:
        source = "test5"
    if source.startswith(".") or source.endswith(".") or ".." in source:
        raise ValueError("Invalid MongoDB question source")
    if not re.match(r"^[\w.-]+$", source, flags=re.UNICODE):
        raise ValueError("Invalid MongoDB question source")
    return source


def _get_requested_question_source(payload=None) -> str:
    payload = payload or {}
    source = (
        payload.get("question_source")
        or payload.get("questionSource")
        or payload.get("mongo_source")
        or payload.get("mongoSource")
        or request.args.get("question_source")
        or request.args.get("questionSource")
    )
    return _normalize_question_source(source)


def _get_question_collection(source_or_payload=None):
    if isinstance(source_or_payload, dict) or source_or_payload is None:
        source = _get_requested_question_source(source_or_payload or {})
    else:
        source = _normalize_question_source(source_or_payload)

    if "." in source:
        db_name, collection_name = source.split(".", 1)
        db = mongo.cx[db_name]
    else:
        collection_name = source
        db = mongo.db

    return db[collection_name], source


def _to_comparison_text(value: Any) -> str:
    """Convert a quiz answer/value into stable text for comparison prompts."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _summarize_comparison_side(side: Dict[str, Any]) -> Dict[str, Any]:
    context = side.get("context") or {}
    chunks = side.get("chunk_summary") or {}
    return {
        "ok": bool(side.get("ok")),
        "latency_ms": side.get("latency_ms"),
        "hit_count": side.get("hit_count", 0),
        "trace_id": side.get("trace_id"),
        "context_chars": context.get("chars", 0),
        "context_utf8_bytes": context.get("utf8_bytes", 0),
        "context_estimated_tokens": context.get("estimated_tokens", 0),
        "retrieved_chunk_count": chunks.get("retrieved", 0),
        "unique_retrieved_chunk_count": chunks.get("unique_retrieved", 0),
        "injected_chunk_count": chunks.get("injected", 0),
        "complete_injected_chunk_count": chunks.get("complete_injected", 0),
        "shortened_chunk_count": chunks.get("shortened", 0),
        "omitted_chunk_count": chunks.get("omitted", 0),
        "valid_for_complete_context_comparison": chunks.get(
            "valid_for_complete_context_comparison", False
        ),
        "answer_chars": len(str(side.get("answer") or "")),
        "error": side.get("error"),
    }


def _run_submit_rag_three_way_comparisons(
    answered_questions: List[Dict[str, Any]],
    *,
    template_id: Any,
    quiz_history_id: Any,
    quiz_type: str,
    question_source: str,
    top_k: int = 5,
    max_items: int = 0,
) -> Dict[str, Any]:
    """Run GraphRAG / ChromaDB / pure LLM comparisons for submitted answers.

    The full comparison JSON is saved by src.rag_backend_api; this function
    returns lightweight summaries for the submit response and result page.
    """
    try:
        from src.rag_backend_api import run_three_way_comparison
    except Exception as exc:
        return {
            "enabled": True,
            "status": "failed",
            "error": f"cannot import comparison runner: {exc}",
            "records": [],
            "count": 0,
            "succeeded": 0,
            "failed": 1,
        }

    records: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    failures = 0
    processed = 0

    for q_data in answered_questions:
        if max_items and processed >= max_items:
            skipped.append({
                "reason": "max_items_reached",
                "question_index": q_data.get("index"),
            })
            continue

        question = q_data.get("question") or {}
        question_text = str(
            question.get("question_text")
            or question.get("parent_question_text")
            or question.get("group_question_text")
            or ""
        ).strip()
        if question.get("is_sub_question") and question.get("parent_question_text"):
            question_text = "\n".join([
                str(question.get("parent_question_text") or "").strip(),
                f"{question.get('question_number', '')}. {question.get('question_text', '')}".strip(),
            ]).strip()

        user_answer_text = _to_comparison_text(q_data.get("user_answer")).strip()
        correct_answer_text = _to_comparison_text(question.get("correct_answer")).strip()

        if not question_text or not user_answer_text:
            skipped.append({
                "reason": "missing_question_or_answer",
                "question_index": q_data.get("index"),
                "question_id": question.get("original_exam_id") or question.get("id"),
            })
            continue

        feedback = (q_data.get("ai_result") or {}).get("feedback") or {}
        metadata = {
            "source": "quiz_submit_experiment",
            "template_id": template_id,
            "quiz_history_id": quiz_history_id,
            "quiz_type": quiz_type,
            "question_source": question_source,
            "question_index": q_data.get("index"),
            "sub_index": q_data.get("sub_index"),
            "question_id": question.get("original_exam_id") or question.get("id"),
            "question_number": question.get("question_number"),
            "is_sub_question": bool(question.get("is_sub_question", False)),
        }

        try:
            comparison = run_three_way_comparison(
                {
                    "question": question_text,
                    "student_answer": user_answer_text,
                    "correct_answer": correct_answer_text,
                    "grading_feedback": feedback,
                    "top_k": top_k,
                },
                metadata=metadata,
            )
            records.append({
                **metadata,
                "comparison_id": comparison.get("comparison_id"),
                "comparison_json_path": comparison.get("comparison_json_path"),
                "status": comparison.get("status"),
                "started_at": comparison.get("started_at"),
                "finished_at": comparison.get("finished_at"),
                "latency_ms": comparison.get("latency_ms"),
                "top_k": comparison.get("top_k"),
                "comparison_mode": comparison.get("comparison_mode"),
                "question_preview": question_text[:180],
                "student_answer_preview": user_answer_text[:180],
                "correct_answer_preview": correct_answer_text[:180],
                "summary": {
                    "graphrag": _summarize_comparison_side(comparison.get("graphrag") or {}),
                    "chromadb": _summarize_comparison_side(comparison.get("chromadb") or {}),
                    "llm_only": _summarize_comparison_side(comparison.get("llm_only") or {}),
                },
            })
            processed += 1
        except Exception as exc:
            failures += 1
            records.append({
                **metadata,
                "status": "failed",
                "error": str(exc),
                "question_preview": question_text[:180],
            })

    succeeded = sum(1 for item in records if item.get("comparison_id"))
    status = "done" if records and failures == 0 else ("partial" if records else "skipped")
    return {
        "enabled": True,
        "status": status,
        "count": len(records),
        "succeeded": succeeded,
        "failed": failures,
        "skipped": skipped,
        "records": records,
    }


def _configured_question_sources():
    raw_sources = os.getenv(QUESTION_SOURCE_ENV, "")
    sources = [DEFAULT_QUESTION_SOURCE, "test5"]
    sources.extend([item.strip() for item in raw_sources.split(",") if item.strip()])

    unique = []
    seen = set()
    for source in sources:
        try:
            normalized = _normalize_question_source(source)
        except ValueError:
            continue
        if normalized not in seen:
            unique.append(normalized)
            seen.add(normalized)
    return unique


def _ensure_quiz_templates_question_source_column(conn):
    try:
        exists = conn.execute(text("""
            SELECT COUNT(*)
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'quiz_templates'
              AND COLUMN_NAME = 'question_source'
        """)).scalar()
        if not exists:
            conn.execute(text("""
                ALTER TABLE quiz_templates
                ADD COLUMN question_source VARCHAR(255) DEFAULT 'test5' AFTER question_ids,
                ADD INDEX idx_question_source (question_source)
            """))
            conn.commit()
    except Exception as e:
        print(f"⚠️ 檢查/新增 quiz_templates.question_source 欄位失敗: {e}")


def _resolve_question_type(question_doc: dict) -> str:
    """統一解析題目類型，避免 coding-answer 被誤判成 single-choice"""
    raw_type = question_doc.get('type', 'single-choice')
    answer_type = question_doc.get('answer_type', '')

    if raw_type == 'group':
        return 'group'
    if answer_type and str(answer_type).strip():
        return answer_type
    if raw_type == 'single':
        return 'single-choice'
    return raw_type or 'single-choice'


def _normalize_options(options):
    """統一處理 options 格式"""
    def _format_option(key, value):
        key_text = str(key).strip()
        value_text = str(value).strip()
        if not key_text:
            return value_text
        if re.match(r"^[A-Za-z0-9]+$", key_text):
            return f"{key_text}. {value_text}"
        return f"{key_text} {value_text}".strip()

    def _parse_string_options(raw: str):
        raw = (raw or "").strip()
        if not raw:
            return []

        # 支援 Mongo/Gemini 舊資料常見格式：
        # "{'A': 'stack', 'B': 'priority queue'}" 或 '{"A": "stack"}'
        if (raw.startswith("{") and raw.endswith("}")) or (raw.startswith("[") and raw.endswith("]")):
            for parser in (json.loads, ast.literal_eval):
                try:
                    parsed = parser(raw)
                    return _normalize_options(parsed)
                except Exception:
                    continue

        # 支援 "A: stack, B: priority queue" / "A. stack; B. priority queue"
        keyed = re.findall(
            r"(?:^|[,;\n]\s*)([A-Za-z])\s*[:：.)]\s*(.*?)(?=(?:[,;\n]\s*[A-Za-z]\s*[:：.)])|$)",
            raw,
            flags=re.DOTALL,
        )
        if len(keyed) >= 2:
            return [_format_option(k, v) for k, v in keyed if str(v).strip()]

        return [opt.strip() for opt in re.split(r"[,;\n]+", raw) if opt.strip()]

    if isinstance(options, dict):
        return [_format_option(k, v) for k, v in options.items() if str(v).strip()]

    if isinstance(options, str):
        return _parse_string_options(options)

    if isinstance(options, list):
        normalized = []
        for item in options:
            if isinstance(item, dict):
                normalized.extend(_normalize_options(item))
            elif isinstance(item, str):
                parsed = _parse_string_options(item)
                normalized.extend(parsed if parsed else [])
            elif item is not None:
                normalized.append(str(item).strip())
        return [opt for opt in normalized if opt]

    return []


def _normalize_key_points(value):
    """統一處理 key-points / key_points 格式"""
    if isinstance(value, list):
        return ', '.join(value) if value else ''
    return value or ''


# 第四題 OLS 表格在部分 JSON 只留下 P2-TABLE1 引用，沒有留下完整 latex_assets。
# 這裡提供可渲染的 fallback asset，讓前端「題目附加內容」仍能顯示上方 OLS Regression Results。
OLS_REGRESSION_RESULTS_LATEX = r"""
\begin{tabular}{|l|l|l|l|l|l|l|}
\hline
\multicolumn{7}{|c|}{OLS Regression Results} \\
\hline
Dep. Variable & Garbage & R-squared & 0.170 &  &  &  \\
Model & OLS & Adj. R-squared & [UNK1] &  &  &  \\
Method & Least Squares & F-statistic & 29.80 &  &  &  \\
Date & Thu, 18 Jun 2070 & Prob (F-statistic) & 1.52e-17 &  &  &  \\
Time & 16:28:57 & Log-Likelihood & -1194.7 &  &  &  \\
No. Observations & 440 & AIC & 2397 &  &  &  \\
Df Residuals & [UNK2] & BIC & 2414 &  &  &  \\
Df Model & 3 & Covariance Type & nonrobust &  &  &  \\
\hline
Variable & coef & std err & t & P>|t| & [0.025 & 0.975] \\
\hline
Intercept & 7.1943 & 1.092 & [UNK3] & 0.000 & --- & --- \\
Q(`House Size`) & 0.0019 & 0.001 & --- & 0.001 & 0.001 & 0.003 \\
Children & 1.1028 & 0.141 & --- & 0.000 & 0.826 & 1.379 \\
Adults & 1.0425 & 0.233 & --- & 0.000 & 0.585 & 1.500 \\
\hline
Omnibus & 0.193 & Durbin-Watson & 1.963 &  &  &  \\
Prob(Omnibus) & 0.908 & Jarque-Bera (JB) & 0.272 &  &  &  \\
Skew & 0.044 & Prob(JB) & 0.873 &  &  &  \\
Kurtosis & 2.916 & Cond. No. & 1.14e+04 &  &  &  \\
\hline
\end{tabular}
""".strip()

OLS_REGRESSION_RESULTS_DESCRIPTION = (
    "OLS Regression Results table: Dep. Variable = Garbage; Model = OLS; Method = Least Squares; "
    "No. Observations = 440; Df Residuals = [UNK2]; Df Model = 3; R-squared = 0.170; "
    "Adj. R-squared = [UNK1]; F-statistic = 29.80; Prob(F-statistic) = 1.52e-17; "
    "Log-Likelihood = -1194.7; AIC = 2397; BIC = 2414. Coefficients: Intercept coef 7.1943, "
    "std err 1.092, t [UNK3], P>|t| 0.000; Q('House Size') coef 0.0019, std err 0.001, "
    "P>|t| 0.001, 95% CI [0.001, 0.003]; Children coef 1.1028, std err 0.141, P>|t| 0.000, "
    "95% CI [0.826, 1.379]; Adults coef 1.0425, std err 0.233, P>|t| 0.000, 95% CI [0.585, 1.500]."
)


def _get_asset_refs_from_layout(question_doc: dict) -> List[str]:
    """從 layout_blocks 取出所有 asset_ref。"""
    refs = []
    for block in question_doc.get('layout_blocks', []) or []:
        if isinstance(block, dict) and block.get('asset_ref'):
            refs.append(str(block.get('asset_ref')))
    return refs


def _get_shared_asset_refs(question_doc: dict) -> List[str]:
    """合併 shared_asset_refs 與 layout_blocks asset_ref，並去重。"""
    refs = []
    for ref in (question_doc.get('shared_asset_refs', []) or []):
        if ref:
            refs.append(str(ref))
    refs.extend(_get_asset_refs_from_layout(question_doc))

    seen = set()
    unique_refs = []
    for ref in refs:
        if ref not in seen:
            unique_refs.append(ref)
            seen.add(ref)
    return unique_refs


def _has_asset_ref(question_doc: dict, target_ref: str) -> bool:
    """檢查 latex_assets 是否已有指定 asset_ref / asset_id。"""
    target_ref = str(target_ref)
    for asset in question_doc.get('latex_assets', []) or []:
        if not isinstance(asset, dict):
            continue
        if str(asset.get('asset_ref', '')) == target_ref or str(asset.get('asset_id', '')) == target_ref:
            return True
    return False


def _needs_ols_fallback(question_doc: dict) -> bool:
    """
    判斷是否需要補上 OLS 表格。
    第四題常見狀況：layout_blocks / shared_asset_refs 有 P2-TABLE1，
    但 latex_assets 裡缺少 P2-TABLE1，導致前端只渲染下方 F table。
    """
    refs = _get_shared_asset_refs(question_doc)
    if 'P2-TABLE1' in refs and not _has_asset_ref(question_doc, 'P2-TABLE1'):
        return True

    all_text = " ".join(
        str(block.get('text', ''))
        for block in (question_doc.get('layout_blocks', []) or [])
        if isinstance(block, dict)
    )
    question_text = str(question_doc.get('question_text', '') or '')
    return (
        ('OLS regression results' in all_text or 'OLS Regression Results' in all_text or 'regression routine' in question_text)
        and not _has_asset_ref(question_doc, 'P2-TABLE1')
    )


def _fallback_ols_asset() -> dict:
    """產生可讓前端渲染的 OLS Regression Results asset。"""
    return {
        'asset_id': 'P2-TABLE1',
        'asset_ref': 'P2-TABLE1',
        'asset_type': 'table_simple',
        'page_number': 2,
        'latex': OLS_REGRESSION_RESULTS_LATEX,
        'description': OLS_REGRESSION_RESULTS_DESCRIPTION,
        'labels': [
            'OLS Regression Results', 'Dep. Variable', 'Garbage', 'R-squared', '0.170',
            'Adj. R-squared', '[UNK1]', 'F-statistic', '29.80', 'Prob (F-statistic)', '1.52e-17',
            'No. Observations', '440', 'Df Residuals', '[UNK2]', 'Df Model', '3',
            'Intercept', '7.1943', '[UNK3]', 'Q(`House Size`)', '0.0019',
            'Children', '1.1028', 'Adults', '1.0425'
        ],
        'render_type': 'latex',
        'needs_review': False,
        'validation_issues': [],
        'confidence': 0.95,
        'notes': 'Auto-filled fallback because layout_blocks/shared_asset_refs referenced P2-TABLE1 but latex_assets did not contain it.',
        'origin': 'backend_fallback'
    }




def _strip_html_tags(value: str) -> str:
    """移除 HTML tag，保留儲存格文字。"""
    if not value:
        return ''
    value = re.sub(r'<br\s*/?>', '\n', value, flags=re.I)
    value = re.sub(r'<[^>]+>', '', value)
    value = (
        value.replace('&nbsp;', ' ')
             .replace('&alpha;', 'α')
             .replace('&infin;', '∞')
             .replace('&infty;', '∞')
             .replace('&lt;', '<')
             .replace('&gt;', '>')
             .replace('&amp;', '&')
    )
    return re.sub(r'\s+', ' ', value).strip()


def _escape_latex_cell(value: str) -> str:
    """簡單轉義 LaTeX 表格儲存格。"""
    value = str(value or '').strip()
    value = value.replace('\\', r'\textbackslash{}')
    replacements = {
        '&': r'\&',
        '%': r'\%',
        '$': r'\$',
        '#': r'\#',
        '_': r'\_',
        '{': r'\{',
        '}': r'\}',
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    value = value.replace('α', r'$\alpha$').replace('∞', r'$\infty$')
    return value


def _html_table_to_latex_tabular(html: str, max_cols: int = 24, max_rows: int = 80) -> str:
    """
    將 <table>...</table> 轉成目前前端較容易吃的 simple tabular。
    目的：避免畫面直接印出 <table> 原始碼，也避免需要前端新增 html_table 支援。
    """
    if not html or '<table' not in html.lower():
        return ''

    rows = []
    for tr in re.findall(r'<tr\b[^>]*>(.*?)</tr>', html, flags=re.I | re.S):
        cells = []
        for cell in re.findall(r'<t[hd]\b[^>]*>(.*?)</t[hd]>', tr, flags=re.I | re.S):
            txt = _strip_html_tags(cell)
            cells.append(txt)
        if cells:
            rows.append(cells[:max_cols])
        if len(rows) >= max_rows:
            break

    if not rows:
        return ''

    col_count = max(len(r) for r in rows)
    col_count = min(col_count, max_cols)
    spec = '|' + '|'.join(['c'] * col_count) + '|'
    latex_lines = [rf'\begin{{tabular}}{{{spec}}}', r'\hline']

    for row in rows:
        padded = (row + [''] * col_count)[:col_count]
        latex_lines.append(' & '.join(_escape_latex_cell(c) for c in padded) + r' \\')
        latex_lines.append(r'\hline')

    latex_lines.append(r'\end{tabular}')
    return '\n'.join(latex_lines)


def _is_bad_ols_asset(asset: dict) -> bool:
    """
    判斷 P2-TABLE1 是否其實是頁首/空內容，而不是 OLS 表格。
    你的新版 output 中 P2-TABLE1 被誤判成：
    'The image contains page header text... This is not a table.'
    這種情況要用後端 fallback 的 OLS 表格覆蓋。
    """
    if not isinstance(asset, dict):
        return False

    ref = str(asset.get('asset_ref') or asset.get('asset_id') or '')
    if ref != 'P2-TABLE1':
        return False

    latex = str(asset.get('latex') or '').strip()
    description = str(asset.get('description') or '').lower()
    asset_type = str(asset.get('asset_type') or '').lower()

    if 'ols regression results' in latex.lower():
        return False

    return (
        not latex
        or 'not a table' in description
        or 'page header' in description
        or asset_type == 'other'
    )


def _asset_image_url_from_path(raw_path: str) -> str:
    """把 final12.py 的本機/相對圖片路徑轉成前端可請求的 URL。"""
    raw = str(raw_path or '').strip()
    if not raw:
        return ''
    normalized = raw.replace('\\', '/')
    lower = normalized.lower()
    if lower.startswith(('http://', 'https://')) or normalized.startswith(('/api/', '/static/', '/output_json/')):
        return normalized

    stripped = normalized[2:] if normalized.startswith('./') else normalized.lstrip('/')
    if stripped.lower().startswith('output_json/'):
        return '/' + stripped

    marker = '/output_json/'
    marker_idx = lower.find(marker)
    if marker_idx >= 0:
        return normalized[marker_idx:]

    import os as _os
    filename = _os.path.basename(normalized)
    return f'/api/assets/{filename}' if filename else ''


def _asset_id_image_url(asset: dict) -> str:
    """當舊資料只剩 asset_id 時，回推可由 /api/assets 搜尋的圖片 URL。"""
    if not isinstance(asset, dict):
        return ''
    asset_id = str(asset.get('asset_id') or asset.get('asset_ref') or '').strip()
    if not asset_id or '/' in asset_id or '\\' in asset_id:
        return ''
    if not re.search(r'\.[a-z0-9]{2,5}$', asset_id, flags=re.IGNORECASE):
        asset_id = f'{asset_id}.png'
    return f'/api/assets/{asset_id}'


def _normalize_asset_for_frontend(asset: dict) -> dict:
    """
    把 renderer 產生的資產轉成目前前端比較安全的格式。

    修正重點：
    1. 不再把 HTML table 搬到 html 欄位並清空 latex，因為目前前端不讀 html。
    2. HTML table 會轉成 simple LaTeX tabular，讓原本的表格渲染流程可以顯示。
    3. TikZ/pgfplots 不支援前端渲染，移除 latex 欄位，改用 description，避免大片原始碼。
    4. P2-TABLE1 若被誤辨識成頁首，會在 _ensure_renderable_assets 裡被 OLS fallback 取代。
    """
    if not isinstance(asset, dict):
        return asset

    a = dict(asset)
    latex = str(a.get('latex', '') or '').strip()
    asset_type = str(a.get('asset_type', '') or '').lower()
    render_type = str(a.get('render_type', '') or '').lower()
    render_strategy = str(a.get('render_strategy', '') or '').lower()
    description = str(a.get('description', '') or '').strip()
    image_source = str(
        a.get('image_url') or a.get('image_path') or a.get('crop_path') or ''
    ).strip()
    visual_asset = any(
        token in asset_type
        for token in ('figure', 'plot', 'diagram', 'flowchart', 'circuit', 'tree', 'image')
    )
    if not visual_asset:
        ref_blob = str(a.get('asset_id') or a.get('asset_ref') or '').lower()
        visual_asset = bool(re.search(r'(^|[-_])(fig|img|flow|plot|diagram)([-_]|$)', ref_blob))
    if not image_source and visual_asset and not latex:
        image_source = _asset_id_image_url(a)

    # ★ PNG fallback 優先（前端 final12 自動標的 image_path）★
    # 若 asset 有 png_fallback=True 跟 image_path，直接轉成 image_url 走 <img> 渲染，
    # 完全不再嘗試 LaTeX / TikZ；這是無法 LaTeX 渲染的圖形最穩管道。
    if image_source and (
        a.get('png_fallback')
        or render_type in ('image', 'png_extracted')
        or render_strategy == 'png_extracted'
        or (visual_asset and not latex)
    ):
        image_url = _asset_image_url_from_path(image_source)
        if image_url:
            a['image_url'] = image_url
            a['render_type'] = 'image'
            a['needs_review'] = False
            a.pop('latex', None)  # 不需要 LaTeX 包了
            if not description:
                a['description'] = '此視覺資產以 PDF 裁切的原始圖片顯示。'
            return a

    # HTML table 不要顯示原始碼，轉成目前前端較可能支援的 simple tabular。
    if latex.lstrip().lower().startswith('<table'):
        converted = _html_table_to_latex_tabular(latex)
        if converted:
            a['latex'] = converted
            a['render_type'] = 'latex'
            a['asset_type'] = 'table_simple'
            a['notes'] = (str(a.get('notes', '') or '') + ' | backend converted html table to simple LaTeX tabular').strip(' |')
        else:
            a.pop('latex', None)
            a['render_type'] = 'description'
            if not description:
                a['description'] = '此表格原本是 HTML table，但轉換失敗，請重新抽取或前端支援 HTML table。'
        return a

    # Code blocks (lstlisting / verbatim) — 不像 TikZ 那麼難渲染。
    # 直接把 LaTeX wrapper 拆掉、保留純文字 code，並提供多個欄位讓前端容易顯示：
    #   - latex：**保留** \begin{verbatim}...\end{verbatim} 包裝（前端 stripCodeWrapper
    #     會去掉 wrapper 後渲染進 <pre><code>）。**不再 pop('latex')**，這樣現有前端
    #     模板 `{{ stripCodeWrapper(asset.latex) }}` 才能正常顯示。
    #   - code_text：純文字 code（無 verbatim wrapper），給未來新版前端用。
    #   - description：保留 vision 給的「C 語言 do-while 迴圈…」描述；不再把 code
    #     字面塞進 description（避免和 code 區重複內容）。
    #   - asset_type 維持 'code'
    code_markers = (r'\begin{lstlisting}', r'\begin{verbatim}')

    def _extract_code_from_text(blob: str) -> str:
        """從可能含混敘述的文字裡，抓出像 C/C++/Python 的程式碼行段。

        用 regex：含 ; { } 或 for(/while(/if(/int/void/printf/Printf 等關鍵 token
        的連續行視為 code。如果整段都沒有這些特徵，回空字串。
        """
        if not blob:
            return ''
        # 先試從 verbatim/lstlisting 抽
        m = re.search(r'\\begin\{(?:lstlisting|verbatim)\}(?:\[[^\]]*\])?\s*\n?(.*?)\\end\{(?:lstlisting|verbatim)\}',
                      blob, re.DOTALL)
        if m:
            return m.group(1).rstrip()
        # 抓「程式碼樣態」行
        code_indicators = re.compile(
            r'(\bint\b|\bvoid\b|\bfloat\b|\bdouble\b|\bchar\b|\bprintf\b|\bPrintf\b|\bscanf\b|'
            r'\bmain\b|\bfor\s*\(|\bwhile\s*\(|\bif\s*\(|\bdo\s*\{|\breturn\b|'
            r'[{};]|->|\+\+|--|==|!=|<=|>=)',
            re.IGNORECASE,
        )
        # 用 ; { } 或換行切片
        # 簡化方法：找連續含 code_indicators 的句子片段
        code_lines: List[str] = []
        # 用句號或全形句號切；保留 ; 的內容
        # 先試「。」「.」「!」「?」做粗切，每塊判定 code-likeness
        sentences = re.split(r'(?<=[。.!?])\s+', blob)
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if code_indicators.search(sent):
                code_lines.append(sent)
        if not code_lines:
            return ''
        # 把 ` ; ` 拆成獨立行，比較像 code 視覺
        joined = ' '.join(code_lines)
        # 把 `;` 後面的空白換成 newline，保留 `;`
        formatted = re.sub(r';\s+', ';\n', joined)
        # 把 `{` 前後空白拉換行
        formatted = re.sub(r'\s*\{\s*', ' {\n    ', formatted)
        formatted = re.sub(r'\s*\}\s*', '\n}\n', formatted)
        return formatted.strip()

    if latex and any(m in latex for m in code_markers):
        code_text = ''
        m = re.search(
            r'\\begin\{lstlisting\}(?:\[[^\]]*\])?\s*\n?(.*?)\\end\{lstlisting\}',
            latex, re.DOTALL,
        )
        if not m:
            m = re.search(
                r'\\begin\{verbatim\}\s*\n?(.*?)\\end\{verbatim\}',
                latex, re.DOTALL,
            )
        if m:
            code_text = m.group(1).rstrip()
        if not code_text:
            code_text = latex
        a['code_text'] = code_text
        a['language'] = a.get('language') or 'c'
        # 保留 verbatim 包裝形式給 latex，前端 stripCodeWrapper 會去掉 wrapper
        a['latex'] = f'\\begin{{verbatim}}\n{code_text}\n\\end{{verbatim}}'
        a['render_type'] = 'code'
        return a

    # 其餘 TikZ / pgfplots / circuitikz / chemfig / forest 仍視為不可直接渲染。
    unsupported_markers = [
        r'\begin{tikzpicture}', r'\begin{axis}', r'\addplot', r'\node', r'\draw',
        r'\begin{circuitikz}', r'\chemfig',
        r'\begin{forest}', r'\begin{tree}',  # 樹狀圖
    ]
    if latex and any(m in latex for m in unsupported_markers):
        a.pop('latex', None)
        # 若 vision propose 階段有裁切的 PNG（crop_path），轉成 image_url 給前端 <img> 渲染
        crop_path = str(a.get('crop_path', '') or '').strip()
        if crop_path:
            a['image_url'] = _asset_image_url_from_path(crop_path)
            a['render_type'] = 'image'
            a['needs_review'] = False
            if not description:
                a['description'] = '此視覺資產原為 TikZ/pgfplots，已改用 PDF 裁切的原始圖片顯示。'
        else:
            a['render_type'] = 'description'
            a['needs_review'] = True
            if not description:
                a['description'] = '此視覺資產使用 TikZ/pgfplots 等語法，目前前端不支援直接渲染，已改以文字說明顯示。'
        return a

    # Code asset：沒 wrapper 或 latex 為空都會走這 — 強制還原成 verbatim
    if asset_type == 'code':
        # 來源候選：latex（純 raw code）→ description（可能含「C 語言…程式碼…int n=0; do { … }」這種混雜文）→ code_text
        existing_code_text = str(a.get('code_text', '') or '').strip()
        candidate = latex or existing_code_text
        # 若 candidate 仍空白，從 description 抽 code 樣態
        if not candidate and description:
            candidate = _extract_code_from_text(description)
        # 仍空白：把整段 description 當 code 字面（即便是描述，總比空白好讓人看）
        if not candidate and description:
            candidate = description
        if candidate:
            a['code_text'] = candidate
            a['language'] = a.get('language') or 'c'
            # 包成 verbatim 留在 latex，前端 stripCodeWrapper 會去掉 wrapper 後渲染
            a['latex'] = f'\\begin{{verbatim}}\n{candidate}\n\\end{{verbatim}}'
            a['render_type'] = 'code'
        else:
            # 完全沒內容：留個 placeholder 提示
            a['latex'] = ''
            a['render_type'] = 'description'
            if not description:
                a['description'] = '此程式碼資產內容未取得，請重新抽取。'
        return a

    # 空 latex 卻標成 latex，會讓前端出現空白框；移除 latex 欄位。
    if not latex and render_type == 'latex':
        a.pop('latex', None)
        a['render_type'] = 'description'

    return a

def _ensure_renderable_assets(question_doc: dict) -> List[dict]:
    """
    回傳前端可渲染的 assets。

    若 JSON 有錯誤的 P2-TABLE1（被辨識成頁首而非 OLS 表格），直接用
    OLS fallback 取代；若缺少 P2-TABLE1 但 layout_blocks 有引用，也補上。
    """
    raw_assets = [
        asset for asset in (question_doc.get('latex_assets', []) or [])
        if isinstance(asset, dict)
    ]

    assets = []
    ols_replaced = False

    for asset in raw_assets:
        if _is_bad_ols_asset(asset):
            assets.append(_normalize_asset_for_frontend(_fallback_ols_asset()))
            ols_replaced = True
            continue
        assets.append(_normalize_asset_for_frontend(asset))

    if _needs_ols_fallback(question_doc) and not ols_replaced:
        # 避免重複補 P2-TABLE1
        has_p2_table1 = any(
            str(a.get('asset_ref', '') or a.get('asset_id', '')) == 'P2-TABLE1'
            for a in assets
            if isinstance(a, dict)
        )
        if not has_p2_table1:
            assets = [_normalize_asset_for_frontend(_fallback_ols_asset())] + assets

    return assets

def _build_asset_lookup(question_doc: dict) -> dict:
    """把 latex_assets 轉成 {asset_ref: asset}，方便 layout_blocks 參照。"""
    assets = _ensure_renderable_assets(question_doc)
    lookup = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        for key in ('asset_ref', 'asset_id'):
            ref = asset.get(key)
            if ref:
                lookup[str(ref)] = asset
    return lookup


def _asset_to_display_text(asset: dict, block_text: str = '') -> str:
    """
    只把「真正應該出現在題幹內」的資產轉成文字。

    重點：表格、圖形、表格說明(description)不要塞進 question_text，
    否則前端主題目區會出現一大串：
    [OLS regression results table: ...]
    [F-distribution critical values lookup table: ...]

    表格/圖形本體仍會透過 latex_assets 傳給前端，在「題目附加內容」渲染。
    """
    if not isinstance(asset, dict):
        return ''

    asset_type = str(asset.get('asset_type', '') or '').lower()
    render_type = str(asset.get('render_type', '') or '').lower()
    latex = str(asset.get('latex', '') or '').strip()

    # 表格與圖形不要出現在 question_text，避免題幹混入說明文字。
    if (
        'table' in asset_type
        or 'plot' in asset_type
        or 'figure' in asset_type
        or render_type == 'description'
    ):
        return ''

    # 公式可以放在題幹內，讓前端 KaTeX/MathJax 渲染。
    if latex and ('formula' in asset_type or render_type == 'latex') and len(latex) <= 3000:
        return latex

    return ''

def build_full_question_text(question_doc: dict) -> str:
    """
    優先使用 layout_blocks 組出完整題目文字。

    題幹與子題會顯示在 question_text。
    表格/圖片/圖形類 asset_ref 不塞進 question_text，避免主畫面出現
    [OLS regression results table: ...] 這種描述文字；它們會保留在 latex_assets
    由前端「題目附加內容」區塊渲染。
    """
    blocks = question_doc.get('layout_blocks', []) or []
    if not blocks:
        return question_doc.get('question_text', '') or question_doc.get('group_question_text', '') or ''

    asset_lookup = _build_asset_lookup(question_doc)
    lines = []

    for block in blocks:
        if not isinstance(block, dict):
            continue

        block_type = str(block.get('block_type', '') or '').lower()
        label = str(block.get('label', '') or '').strip()
        text = str(block.get('text', '') or '').strip()
        asset_ref = block.get('asset_ref')

        # *_ref 類 block（code_ref / table_ref / figure_ref / formula_ref）：
        # block.text 通常只是 asset 的 placeholder 描述（如「C code snippet for
        # do-while loop with break」），絕對不能當題幹文字塞進 question_text。
        # 即使 asset_ref 為空（vision 沒抽到 asset_id）也要整塊跳過，
        # 否則前端就會出現「給定一段程式碼如下，請問螢幕上會印出? C code snippet for ...」
        # 這種把描述跟題幹接在一起的混淆畫面。
        # 實際 asset 仍透過 latex_assets 由前端「題目附加內容」區塊渲染。
        is_ref_block = block_type.endswith('_ref')

        if is_ref_block:
            if not asset_ref:
                continue
            asset = asset_lookup.get(str(asset_ref))
            asset_display = _asset_to_display_text(asset, text)
            if not asset_display:
                continue
            display_text = asset_display
        elif asset_ref:
            asset = asset_lookup.get(str(asset_ref))
            asset_display = _asset_to_display_text(asset, text)
            if not asset_display:
                continue
            display_text = asset_display
        else:
            display_text = text

        if not display_text:
            continue

        # 子題、假設 H0/H1、(1)(2)(3) 等保留 label。
        if label:
            lines.append(f"{label} {display_text}".strip())
        else:
            lines.append(display_text)

    full_text = "\n\n".join(lines).strip()
    return full_text or question_doc.get('question_text', '') or question_doc.get('group_question_text', '') or ''


# ==================== 工具函數 ====================

def get_quiz_from_database(quiz_ids: List[str]) -> dict:
    """從資料庫獲取考卷數據"""
    try:
        # quiz_ids 現在是 template_id 列表，取第一個作為 template_id
        template_id = quiz_ids[0] if quiz_ids else None
        question_source = DEFAULT_QUESTION_SOURCE
        if not template_id:
            return {
                'success': False,
                'message': '沒有提供有效的template_id'
            }

        # 先查詢SQL template獲取所有題目ID
        try:
            from accessories import sqldb
            from sqlalchemy import text
            import json

            # 查詢SQL template獲取question_ids
            template_query = text("""
                SELECT question_ids, question_source FROM quiz_templates
                WHERE id = :template_id
            """)

            with sqldb.engine.connect() as conn:
                _ensure_quiz_templates_question_source_column(conn)
                result = conn.execute(template_query, {'template_id': template_id})
                template_row = result.fetchone()

                if not template_row:
                    return {
                        'success': False,
                        'message': '找不到測驗模板'
                    }

                question_ids_json = template_row[0]
                question_source = template_row[1] or DEFAULT_QUESTION_SOURCE
                question_collection, question_source = _get_question_collection(question_source)
                question_ids = json.loads(question_ids_json)

                # 查詢所有題目
                questions = []
                for q_id in question_ids:
                    try:
                        object_id = ObjectId(q_id)
                        question_doc = question_collection.find_one({"_id": object_id})
                        if not question_doc:
                            question_doc = question_collection.find_one({"_id": q_id})
                        if question_doc:
                            questions.append(question_doc)
                    except Exception as e:
                        continue

                if not questions:
                    return {
                        'success': False,
                        'message': '沒有找到任何題目數據'
                    }

        except Exception as e:
            return {
                'success': False,
                'message': f'查詢題目時發生錯誤: {str(e)}'
            }

        # 轉換題目格式為前端需要的格式
        formatted_questions = []
        for i, question in enumerate(questions):
            formatted_type = _resolve_question_type(question)
            options = _normalize_options(question.get('options', []))

            formatted_question = {
                'id': i + 1,  # 使用數字ID，從1開始
                'question_text': build_full_question_text(question),
                'type': formatted_type,
                'answer_type': formatted_type,
                'options': options,
                'correct_answer': question.get('answer', question.get('correct_answer', '')),
                'original_exam_id': str(question.get('_id', question.get('id', ''))),
                'image_file': question.get('image_file', ''),
                'key_points': _normalize_key_points(question.get('key-points', question.get('key_points', ''))),
                'detail_answer': question.get('detail-answer', question.get('detail_answer', question.get('explanation', ''))),
                'explanation': question.get('detail-answer', question.get('detail_answer', question.get('explanation', ''))),
                'topic': question.get('topic', ''),
                'difficulty': question.get('difficulty_level', question.get('difficulty', 'medium')),
                'micro_concepts': question.get('micro_concepts', []),
                'concept_mapping': question.get('concept_mapping'),
                'primary_concept': question.get('primary_concept'),
                'concept_names': question.get('concept_names', []),
                'difficulty_level': question.get('difficulty_level', '中等'),
                'error_reason': question.get('error_reason', ''),
                'latex_assets': _ensure_renderable_assets(question),
                'shared_asset_refs': _get_shared_asset_refs(question),
                'grouped_subquestions': question.get('grouped_subquestions', []),
                'continuation_note': question.get('continuation_note', ''),
                'layout_blocks': question.get('layout_blocks', []),
                'source_pages': question.get('source_pages', []),
                'question_number': question.get('question_number', ''),
                'school': question.get('school', ''),
                'department': question.get('department', ''),
                'year': question.get('year', ''),
                'question_source': question_source,
                'created_at': str(question.get('created_at', '')) if question.get('created_at') else ''
            }
            formatted_questions.append(formatted_question)

        # 從SQL模板獲取測驗信息
        template_info = {}
        try:
            with sqldb.engine.connect() as conn:
                template_query = text("""
                    SELECT template_type, school, department, year, created_at
                    FROM quiz_templates
                    WHERE id = :template_id
                """)

                result = conn.execute(template_query, {'template_id': template_id})
                template_row = result.fetchone()

                if template_row:
                    template_type = template_row[0]
                    school = template_row[1] or ''
                    department = template_row[2] or ''
                    year = template_row[3] or ''
                    created_at = template_row[4]

                    # 根據測驗類型生成標題
                    if template_type == 'pastexam':
                        quiz_title = f"{school} - {year}年 - {department}"
                    else:  # knowledge
                        topic = questions[0].get('key-points', '計算機概論') if questions else '計算機概論'
                        quiz_title = f"{topic} - 知識測驗"

                    template_info = {
                        'title': quiz_title,
                        'exam_type': template_type,
                        'school': school,
                        'department': department,
                        'year': year,
                        'question_source': question_source,
                        'topic': questions[0].get('key-points', '計算機概論') if questions else '計算機概論',
                        'difficulty': questions[0].get('difficulty_level', 'medium') if questions else 'medium',
                        'question_count': len(formatted_questions),
                        'time_limit': 60,
                        'total_score': len(formatted_questions) * 5,
                        'created_at': created_at.isoformat() if created_at else datetime.now().isoformat()
                    }
        except Exception as e:
            print(f"⚠️ 獲取模板信息失敗: {e}")
            # 使用默認信息
            template_info = {
                'title': f"測驗 ({template_id})",
                'exam_type': 'knowledge',
                'question_source': question_source,
                'topic': questions[0].get('key-points', '計算機概論') if questions else '計算機概論',
                'difficulty': questions[0].get('difficulty_level', 'medium') if questions else 'medium',
                'question_count': len(formatted_questions),
                'time_limit': 60,
                'total_score': len(formatted_questions) * 5,
                'created_at': datetime.now().isoformat()
            }

        # 構建考卷數據 (從單個題目中提取信息)
        quiz_data = {
            'quiz_id': str(template_id), # 使用template_id作為quiz_id
            'template_id': str(template_id),
            'title': template_info.get('title', f"測驗 ({template_id})"),
            'questions': formatted_questions,
            'question_source': question_source,
            'time_limit': 60, # Default time limit for single question
            'quiz_info': template_info,
            'database_ids': [str(q_id) for q_id in question_ids] # 儲存所有題目ID，確保是字串
        }

        return {
            'success': True,
            'data': quiz_data
        }

    except Exception as e:
        return {
            'success': False,
            'message': f'獲取考卷數據失敗: {str(e)}'
        }


def init_quiz_tables():
    """初始化測驗相關的SQL表格 - 最終優化版本"""
    try:
        with current_app.app_context():
            # 創建quiz_templates表 - 存儲考卷模板
            with sqldb.engine.connect() as conn:
                conn.execute(sqldb.text("""
                    CREATE TABLE IF NOT EXISTS quiz_templates (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        user_email VARCHAR(255) NOT NULL,
                        template_type ENUM('knowledge', 'pastexam') NOT NULL,
                        question_ids JSON NOT NULL,
                        question_source VARCHAR(255) DEFAULT 'test5',
                        school VARCHAR(100) DEFAULT '',
                        department VARCHAR(100) DEFAULT '',
                        year VARCHAR(20) DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_user_email (user_email),
                        INDEX idx_template_type (template_type),
                        INDEX idx_question_source (question_source),
                        INDEX idx_school (school),
                        INDEX idx_department (department),
                        INDEX idx_year (year),
                        INDEX idx_created_at (created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """))

                conn.commit()
                _ensure_quiz_templates_question_source_column(conn)

            # 創建quiz_history表 - 存儲測驗歷史記錄（最終簡化版）
            with sqldb.engine.connect() as conn:
                conn.execute(sqldb.text("""
                    CREATE TABLE IF NOT EXISTS quiz_history (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        quiz_template_id INT NULL,
                        user_email VARCHAR(255) NOT NULL,
                        quiz_type ENUM('knowledge', 'pastexam') NOT NULL,
                        total_questions INT DEFAULT 0,
                        answered_questions INT DEFAULT 0,
                        correct_count INT DEFAULT 0,
                        wrong_count INT DEFAULT 0,
                        accuracy_rate DECIMAL(5,2) DEFAULT 0,
                        average_score DECIMAL(5,2) DEFAULT 0,
                        total_time_taken INT DEFAULT 0,
                        submit_time DATETIME NOT NULL,
                        status ENUM('incomplete', 'completed', 'abandoned') DEFAULT 'incomplete',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (quiz_template_id) REFERENCES quiz_templates(id) ON DELETE SET NULL,
                        INDEX idx_user_email (user_email),
                        INDEX idx_quiz_template_id (quiz_template_id),
                        INDEX idx_submit_time (submit_time)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """))
                conn.commit()

            # 創建quiz_errors表 - 存儲考生錯題（最終簡化版）
            with sqldb.engine.connect() as conn:
                conn.execute(sqldb.text("""
                    CREATE TABLE IF NOT EXISTS quiz_errors (
                        error_id INT AUTO_INCREMENT PRIMARY KEY,
                        quiz_history_id INT NOT NULL,
                        user_email VARCHAR(255) NOT NULL,
                        mongodb_question_id VARCHAR(50) NOT NULL,
                        user_answer TEXT,
                        score DECIMAL(5,2) DEFAULT 0,
                        time_taken INT DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (quiz_history_id) REFERENCES quiz_history(id) ON DELETE CASCADE,
                        INDEX idx_user_email (user_email),
                        INDEX idx_mongodb_question_id (mongodb_question_id),
                        INDEX idx_created_at (created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """))
                conn.commit()

            # 創建quiz_answers表 - 存儲所有題目的用戶答案
            with sqldb.engine.connect() as conn:
                conn.execute(sqldb.text("""
                    CREATE TABLE IF NOT EXISTS quiz_answers (
                        answer_id INT AUTO_INCREMENT PRIMARY KEY,
                        quiz_history_id INT NOT NULL,
                        user_email VARCHAR(255) NOT NULL,
                        mongodb_question_id VARCHAR(50) NOT NULL,
                        user_answer TEXT NOT NULL,
                        is_correct BOOLEAN NOT NULL DEFAULT FALSE,
                        score DECIMAL(5,2) DEFAULT 0,
                        feedback JSON,
                        answer_time_seconds INT DEFAULT 0,  -- 新增：每題作答時間（秒）
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (quiz_history_id) REFERENCES quiz_history(id) ON DELETE CASCADE,
                        INDEX idx_quiz_history_id (quiz_history_id),
                        INDEX idx_user_email (user_email),
                        INDEX idx_mongodb_question_id (mongodb_question_id),
                        INDEX idx_is_correct (is_correct),
                        INDEX idx_created_at (created_at),
                        INDEX idx_answer_time (answer_time_seconds)  -- 新增：作答時間索引
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """))
                conn.commit()

            # 創建長答案存儲表
            with sqldb.engine.connect() as conn:
                conn.execute(sqldb.text("""
                    CREATE TABLE IF NOT EXISTS long_answers (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        quiz_history_id INT NOT NULL,
                        question_id VARCHAR(255) NOT NULL,
                        user_email VARCHAR(255) NOT NULL,
                        question_type VARCHAR(50) NOT NULL,
                        full_answer LONGTEXT NOT NULL,
                        answer_hash VARCHAR(64) NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_quiz_question (quiz_history_id, question_id),
                        INDEX idx_user (user_email)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """))
                conn.commit()


            return True
    except Exception as e:
        print(f"❌ Failed to initialize quiz tables: {e}")
        return False



@quiz_bp.route('/submit-quiz', methods=['POST', 'OPTIONS'])
def submit_quiz():
    """提交測驗 API - 全AI評分版本"""
    if request.method == 'OPTIONS':
        return jsonify({'token': None, 'message': 'CORS preflight'}), 200

    # 驗證用戶身份
    token = request.headers.get('Authorization').split(" ")[1]
    user_email = verify_token(token)
    if not user_email:
        return jsonify({'token': None, 'message': '無效的token'}), 401

    # 獲取請求數據
    data = request.get_json()
    template_id = data.get('template_id')
    answers = data.get('answers', {})
    time_taken = data.get('time_taken', 0)
    question_answer_times = data.get('question_answer_times', {})  # 新增：提取每題作答時間
    frontend_questions = data.get('questions', [])  # 新增：提取前端發送的題目數據
    enable_rag_three_way_comparison = bool(
        data.get('enable_rag_three_way_comparison')
        or data.get('rag_three_way_enabled')
    )
    try:
        rag_compare_top_k = max(1, min(30, int(data.get('rag_compare_top_k', 5))))
    except (TypeError, ValueError):
        rag_compare_top_k = 5
    try:
        rag_compare_max_items = max(0, int(data.get('rag_compare_max_items', 0)))
    except (TypeError, ValueError):
        rag_compare_max_items = 0

    # 調試日誌

    if not template_id:
        return jsonify({
            'token': None,
            'message': '缺少考卷模板ID'
        }), 400



    # 生成唯一的進度追蹤ID
    progress_id = f"progress_{user_email}_{int(time.time())}"

    # 階段1: 試卷批改 - 獲取題目數據


    # 更新進度狀態為第1階段
    update_progress_status(progress_id, False, 1, "正在獲取題目數據...")

    # 這裡可以發送進度更新到前端（如果使用WebSocket或Server-Sent Events）
    # 目前先打印進度，後續可以實現即時通訊

    # 從SQL獲取模板信息
    with sqldb.engine.connect() as conn:
        template_id_int = int(template_id)
        _ensure_quiz_templates_question_source_column(conn)
        template = conn.execute(text("""
            SELECT * FROM quiz_templates WHERE id = :template_id
        """), {'template_id': template_id_int}).fetchone()

        if not template:
            return jsonify({
                'token': None,
                'message': '考卷模板不存在'
            }), 404

        # 從模板獲取題目ID列表
        question_ids = json.loads(template.question_ids)
        total_questions = len(question_ids)
        quiz_type = template.template_type
        question_source = getattr(template, 'question_source', DEFAULT_QUESTION_SOURCE) or DEFAULT_QUESTION_SOURCE
        question_collection, question_source = _get_question_collection(question_source)

        # 從模板獲取題目數量

        # 優先使用前端發送的題目數據，如果沒有則從MongoDB獲取
        if frontend_questions and len(frontend_questions) > 0:
            questions = frontend_questions
        else:
            # 從MongoDB exam集合獲取題目詳情
            questions = []
            for i, question_id in enumerate(question_ids):
                # 嘗試使用ObjectId查詢
                exam_question = question_collection.find_one({"_id": ObjectId(question_id)})
                if not exam_question:
                    # 如果ObjectId查詢失敗，嘗試直接查詢
                    exam_question = question_collection.find_one({"_id": question_id})

                if exam_question:
                    # 使用與 create-quiz 相同的題目處理邏輯
                    exam_type = exam_question.get('type', 'single')
                    if exam_type == 'group':
                        # 題組：保留群組題外層資訊，展開子題但一併回傳
                        group_question_text = build_full_question_text(exam_question) or exam_question.get('group_question_text') or exam_question.get('question_text', '')
                        if not group_question_text:
                            print(f"⚠️ 警告：題組 {i+1} (ID: {exam_question.get('_id')}) 的 group_question_text 和 question_text 都為空")
                            group_question_text = f"題組 {i+1} (無題目文字)"

                        micro_concepts = exam_question.get('micro_concepts', [])
                        key_points = exam_question.get('key-points', '')
                        # 處理 key-points 可能是陣列或字串的情況
                        if isinstance(key_points, list):
                            key_points = ', '.join(key_points) if key_points else ''
                        parent_id = str(exam_question.get('_id', ''))

                        # 構建子題清單
                        sub_qs_raw = exam_question.get('sub_questions', []) or []
                        sub_qs = []
                        for sub in sub_qs_raw:
                            sub_options = sub.get('options', [])
                            if isinstance(sub_options, str):
                                sub_options = [opt.strip() for opt in sub_options.split(',') if opt.strip()]
                            elif not isinstance(sub_options, list):
                                sub_options = []

                            sub_image = sub.get('image_file', '')

                            sub_qs.append({
                                'question_number': sub.get('question_number', ''),
                                'question_text': sub.get('question_text', ''),
                                'options': sub_options,
                                'answer': sub.get('answer', ''),
                                'answer_type': sub.get('answer_type', 'single-choice'),
                                'image_file': sub_image,
                                'detail_answer': sub.get('detail-answer', ''),
                                'key_points': ', '.join(sub.get('key-points', [])) if isinstance(sub.get('key-points', []), list) else sub.get('key-points', ''),
                                 'difficulty_level': sub.get('difficulty level', sub.get('difficulty_level', '')),
                                 'micro_concepts': sub.get('micro_concepts', []),
                                 'concept_mapping': sub.get('concept_mapping'),
                                 'original_exam_id': parent_id
                            })

                        group_question = {
                            'id': i + 1,
                            'type': 'group',
                            'group_question_text': group_question_text,
                             'micro_concepts': micro_concepts,
                             'concept_mapping': exam_question.get('concept_mapping'),
                             'primary_concept': exam_question.get('primary_concept'),
                             'concept_names': exam_question.get('concept_names', []),
                            'key_points': key_points,
                            'original_exam_id': parent_id,
                            'layout_blocks': exam_question.get('layout_blocks', []),
                            'latex_assets': _ensure_renderable_assets(exam_question),
                            'shared_asset_refs': _get_shared_asset_refs(exam_question),
                            'source_pages': exam_question.get('source_pages', []),
                            'sub_questions': sub_qs
                        }

                        # 題組外層若也有圖片，轉換為 base64
                        group_image_file = exam_question.get('image_file', '')
                        group_image_data_list = []
                        negative_values = ['沒有圖片', '不需要圖片', '不須圖片', '不須照片', '沒有考卷', '']
                        if group_image_file and group_image_file not in negative_values:
                            group_image_filenames = []
                            if isinstance(group_image_file, list):
                                group_image_filenames = [img for img in group_image_file if img and img not in negative_values]
                            elif isinstance(group_image_file, str):
                                group_image_filenames = [group_image_file]

                            # 將每個圖片檔案轉換為 base64
                            for group_image_filename in group_image_filenames:
                                group_image_base64 = get_image_base64(group_image_filename)
                                if group_image_base64:
                                    # 判斷圖片格式
                                    image_ext = os.path.splitext(group_image_filename)[1].lower()
                                    mime_type = 'image/jpeg'
                                    if image_ext in ['.png']:
                                        mime_type = 'image/png'
                                    elif image_ext in ['.gif']:
                                        mime_type = 'image/gif'
                                    elif image_ext in ['.webp']:
                                        mime_type = 'image/webp'

                                    group_image_data_list.append(f"data:{mime_type};base64,{group_image_base64}")

                            # 如果只有一張圖片，直接返回字串；多張圖片返回陣列
                            if len(group_image_data_list) == 1:
                                group_question['image_file'] = group_image_data_list[0]
                            elif len(group_image_data_list) > 1:
                                group_question['image_file'] = group_image_data_list
                            else:
                                group_question['image_file'] = ''
                        else:
                            group_question['image_file'] = ''

                        questions.append(group_question)
                    else:
                        # 單題：保留原始 type / answer_type，避免 coding-answer 被誤判
                        question_type = _resolve_question_type(exam_question)

                        # 調試信息：檢查題目文字
                        question_text = exam_question.get('question_text', '')
                        if not question_text:
                            print(f"⚠️ 警告：題目 {i+1} (ID: {exam_question.get('_id')}) 的 question_text 為空")
                            print(f"   學校: {exam_question.get('school')}, 科系: {exam_question.get('department')}, 年份: {exam_question.get('year')}")
                            print(f"   答案: {exam_question.get('answer', '')[:100]}...")
                            # 嘗試從其他欄位獲取題目文字
                            if exam_question.get('answer'):
                                question_text = f"題目 {i+1}: {exam_question.get('answer', '')[:200]}..."
                            else:
                                question_text = f"題目 {i+1} (無題目文字)"

                        question = {
                            'id': i + 1,
                            'question_text': build_full_question_text(exam_question) or question_text,
                            'type': question_type,
                            'options': exam_question.get('options'),
                            'correct_answer': exam_question.get('answer', ''),
                            'original_exam_id': str(exam_question.get('_id', '')),
                            'image_file': exam_question.get('image_file'),
                             'key_points': _normalize_key_points(exam_question.get('key-points', exam_question.get('key_points', ''))),
                             'micro_concepts': exam_question.get('micro_concepts', []),
                             'concept_mapping': exam_question.get('concept_mapping'),
                             'primary_concept': exam_question.get('primary_concept'),
                             'concept_names': exam_question.get('concept_names', []),
                            'answer_type': question_type,
                            'detail_answer': exam_question.get('detail-answer', exam_question.get('detail_answer', '')),
                            'latex_assets': _ensure_renderable_assets(exam_question),
                            'shared_asset_refs': _get_shared_asset_refs(exam_question),
                            'grouped_subquestions': exam_question.get('grouped_subquestions', []),
                            'continuation_note': exam_question.get('continuation_note', ''),
                            'layout_blocks': exam_question.get('layout_blocks', []),
                            'source_pages': exam_question.get('source_pages', []),
                            'question_number': exam_question.get('question_number', ''),
                            'school': exam_question.get('school', ''),
                            'department': exam_question.get('department', ''),
                            'year': exam_question.get('year', '')
                        }

                        # 處理選項格式
                        question['options'] = _normalize_options(question['options'])

                        # 處理圖片檔案（單題）- 轉換為 base64
                        image_file = exam_question.get('image_file', '')
                        negative_values = ['沒有圖片', '不需要圖片', '不須圖片', '不須照片', '沒有考卷', '']
                        image_data_list = []
                        if image_file and image_file not in negative_values:
                            image_filenames = []
                            if isinstance(image_file, list):
                                image_filenames = [img for img in image_file if img and img not in negative_values]
                            elif isinstance(image_file, str):
                                image_filenames = [image_file]

                            # 將每個圖片檔案轉換為 base64
                            for image_filename in image_filenames:
                                image_base64 = get_image_base64(image_filename)
                                if image_base64:
                                    # 判斷圖片格式
                                    image_ext = os.path.splitext(image_filename)[1].lower()
                                    mime_type = 'image/jpeg'
                                    if image_ext in ['.png']:
                                        mime_type = 'image/png'
                                    elif image_ext in ['.gif']:
                                        mime_type = 'image/gif'
                                    elif image_ext in ['.webp']:
                                        mime_type = 'image/webp'

                                    image_data_list.append(f"data:{mime_type};base64,{image_base64}")

                            # 如果只有一張圖片，直接返回字串；多張圖片返回陣列
                            if len(image_data_list) == 1:
                                question['image_file'] = image_data_list[0]
                            elif len(image_data_list) > 1:
                                question['image_file'] = image_data_list
                            else:
                                question['image_file'] = ''
                        else:
                            question['image_file'] = ''

                        questions.append(question)
                else:
                    print(f"⚠️ 找不到題目ID: {question_id}")
                    # 創建一個空的題目記錄
                    question = {
                        'id': i + 1,
                        'question_text': f'題目 {i + 1} (ID: {question_id})',
                        'type': 'single-choice',
                        'options': [],
                        'correct_answer': '',
                        'original_exam_id': question_id,
                        'image_file': '',
                        'key_points': ''
                    }
                    questions.append(question)

        # 成功獲取題目詳情

    # 階段2: 計算分數 - 分類題目

    # 更新進度狀態為第2階段
    update_progress_status(progress_id, False, 2, "正在分類題目...")

    # 評分和分析 - 全AI評分邏輯
    correct_count = 0
    wrong_count = 0
    total_score = 0
    wrong_questions = []
    unanswered_count = 0

    # 分類題目：已作答題目和未作答題目（所有已作答題目都使用AI評分）
    answered_questions = []  # 已作答題目（所有類型都使用AI評分）
    unanswered_questions = []    # 未作答題目

    # 處理已作答題目
    for i, question in enumerate(questions):
        question_id = question.get('original_exam_id', '')
        user_answer = answers.get(str(i), '')
        question_type = question.get('type', '')

        if question_type == 'group':
            # GROUP 題特殊處理：處理子題答案
            sub_questions = question.get('sub_questions', [])
            group_answered = False
            group_sub_answers = []  # 收集所有子題答案

            for sub_idx, sub_question in enumerate(sub_questions):
                sub_answer_key = f"{i}_sub_{sub_idx}"  # 子題答案鍵值格式：主題索引_sub_子題索引
                sub_user_answer = answers.get(sub_answer_key, '')

                if sub_user_answer:  # 子題有答案
                    group_answered = True
                    answer_time_seconds = question_answer_times.get(sub_answer_key, 0)
                    group_sub_answers.append(sub_user_answer)

                    # 為每個子題創建獨立的評分資料
                    sub_q_data = {
                        'index': i,
                        'sub_index': sub_idx,
                        'question': {
                            'id': f"{i}_{sub_idx}",
                            'question_text': sub_question.get('question_text', ''),
                            'type': sub_question.get('answer_type', 'single-choice'),
                            'options': sub_question.get('options', []),
                            'correct_answer': sub_question.get('answer', ''),
                            'original_exam_id': sub_question.get('original_exam_id', question_id),
                            'image_file': sub_question.get('image_file', ''),
                            'key_points': sub_question.get('key_points', ''),
                            'question_number': sub_question.get('question_number', ''),
                            'is_sub_question': True,
                            'parent_question_id': question_id,
                            'parent_question_text': question.get('group_question_text', '')
                        },
                        'user_answer': sub_user_answer,
                        'answer_time_seconds': answer_time_seconds
                    }

                    answered_questions.append(sub_q_data)

            # 如果沒有找到標準格式的子題答案，檢查是否有其他格式的答案
            if not group_answered:
                # 檢查是否有以主題索引為鍵的答案（可能是子題答案陣列）
                main_answer = answers.get(str(i), '')
                if isinstance(main_answer, list) and len(main_answer) > 0:
                    # 如果主答案是一個陣列，可能是子題答案
                    group_answered = True
                    group_sub_answers = main_answer
                    for sub_idx, sub_question in enumerate(sub_questions):
                        if sub_idx < len(main_answer):
                            sub_user_answer = main_answer[sub_idx]
                            answer_time_seconds = question_answer_times.get(str(i), 0) // len(sub_questions)  # 平均分配時間

                            # 為每個子題創建獨立的評分資料
                            sub_q_data = {
                                'index': i,
                                'sub_index': sub_idx,
                                'question': {
                                    'id': f"{i}_{sub_idx}",
                                    'question_text': sub_question.get('question_text', ''),
                                    'type': sub_question.get('answer_type', 'single-choice'),
                                    'options': sub_question.get('options', []),
                                    'correct_answer': sub_question.get('answer', ''),
                                    'original_exam_id': sub_question.get('original_exam_id', question_id),
                                    'image_file': sub_question.get('image_file', ''),
                                    'key_points': sub_question.get('key_points', ''),
                                    'question_number': sub_question.get('question_number', ''),
                                    'is_sub_question': True,
                                    'parent_question_id': question_id,
                                    'parent_question_text': question.get('group_question_text', '')
                                },
                                'user_answer': sub_user_answer,
                                'answer_time_seconds': answer_time_seconds
                            }

                            answered_questions.append(sub_q_data)

                # 如果還是沒有找到，檢查所有答案中是否有陣列格式的答案
                if not group_answered:
                    for answer_key, answer_value in answers.items():
                        if isinstance(answer_value, list) and len(answer_value) > 0:
                            # 假設這個陣列答案對應當前 Group 題目
                            group_answered = True
                            group_sub_answers = answer_value
                            # 找到陣列格式答案，處理 Group 題目

                            for sub_idx, sub_question in enumerate(sub_questions):
                                if sub_idx < len(answer_value):
                                    sub_user_answer = answer_value[sub_idx]
                                    answer_time_seconds = question_answer_times.get(answer_key, 0) // len(sub_questions)

                                    # 為每個子題創建獨立的評分資料
                                    sub_q_data = {
                                        'index': i,
                                        'sub_index': sub_idx,
                                        'question': {
                                            'id': f"{i}_{sub_idx}",
                                            'question_text': sub_question.get('question_text', ''),
                                            'type': sub_question.get('answer_type', 'single-choice'),
                                            'options': sub_question.get('options', []),
                                            'correct_answer': sub_question.get('answer', ''),
                                            'original_exam_id': sub_question.get('original_exam_id', question_id),
                                            'image_file': sub_question.get('image_file', ''),
                                            'key_points': sub_question.get('key_points', ''),
                                            'question_number': sub_question.get('question_number', ''),
                                            'is_sub_question': True,
                                            'parent_question_id': question_id,
                                            'parent_question_text': question.get('group_question_text', '')
                                        },
                                        'user_answer': sub_user_answer,
                                        'answer_time_seconds': answer_time_seconds
                                    }

                                    answered_questions.append(sub_q_data)
                            break  # 只處理第一個找到的陣列答案

            # 為 Group 題目本身創建一個整體的答案記錄
            if group_answered:
                # 計算 Group 題目的整體作答時間
                group_answer_time = question_answer_times.get(str(i), 0)
                if not group_answer_time:
                    # 如果沒有找到主題索引的時間，嘗試從子題時間計算
                    group_answer_time = sum(q_data.get('answer_time_seconds', 0) for q_data in answered_questions
                                          if q_data.get('index') == i and q_data.get('question', {}).get('is_sub_question'))

                # 創建 Group 題目的整體答案資料
                group_q_data = {
                    'index': i,
                    'question': {
                        'id': str(i),
                        'question_text': question.get('group_question_text', ''),
                        'type': 'group',
                        'options': [],
                        'correct_answer': '',  # Group 題目沒有單一正確答案
                        'original_exam_id': question_id,
                        'image_file': question.get('image_file', ''),
                        'key_points': question.get('key_points', ''),
                        'is_sub_question': False,
                        'parent_question_id': None,
                        'parent_question_text': None,
                        'sub_questions': sub_questions
                    },
                    'user_answer': group_sub_answers,  # 子題答案陣列
                    'answer_time_seconds': group_answer_time
                }

                answered_questions.append(group_q_data)
            else:
                # 整個題組都沒有答案
                unanswered_count += 1
                unanswered_questions.append({
                    'index': i,
                    'question': question,
                    'user_answer': '',
                    'question_type': 'group'
                })
        else:
            # 單題處理
            if user_answer:  # 只處理有答案的題目
                # 獲取作答時間（秒數）
                answer_time_seconds = question_answer_times.get(str(i), 0)

                # 調試日誌

                # 構建題目資料
                q_data = {
                    'index': i,
                    'question': question,
                    'user_answer': user_answer,
                    'answer_time_seconds': answer_time_seconds  # 每題作答時間（秒）
                }

                answered_questions.append(q_data)
            else:
                # 未作答題目：收集到未作答列表
                unanswered_count += 1
                unanswered_questions.append({
                    'index': i,
                    'question': question,
                    'user_answer': '',
                    'question_type': question_type
                })


    # 更新進度狀態為第3階段
    update_progress_status(progress_id, False, 3, "AI正在進行智能評分...")

    # 批量AI評分所有已作答題目
    if answered_questions:
        # 準備AI評分數據
        ai_questions_data = []
        for q_data in answered_questions:
            question = q_data['question']
            user_answer = q_data['user_answer']
            question_type = question.get('type', '')

            # Map the assessed item before grading. For sub-questions the immutable
            # attempt snapshot is authoritative because they share a parent Mongo id.
            concept_mapping = ensure_question_concept_mapping(
                question,
                question_id=question.get('original_exam_id', ''),
                persist=not question.get('is_sub_question', False),
            )
            q_data['concept_mapping'] = concept_mapping

            # 對於AI評分，使用原始完整答案，不進行截斷
            # 這樣AI能看到完整的圖片內容，評分更準確
            ai_question_data = {
                'question_id': question.get('original_exam_id', ''),
                'user_answer': user_answer,  # 使用原始完整答案
                'question_type': question_type,
                'question_text': question.get('question_text', ''),
                'options': question.get('options', []),
                'correct_answer': question.get('correct_answer', ''),
                'key_points': question.get('key_points', ''),
                'concept_mapping': concept_mapping,
            }

            # 如果是子題，添加額外信息
            if question.get('is_sub_question', False):
                ai_question_data.update({
                    'is_sub_question': True,
                    'question_number': question.get('question_number', ''),
                    'parent_question_id': question.get('parent_question_id', ''),
                    'parent_question_text': question.get('parent_question_text', ''),
                    'sub_index': q_data.get('sub_index', 0)
                })

            ai_questions_data.append(ai_question_data)

        # 使用AI批改模組進行批量評分
        ai_results = batch_grade_ai_questions(ai_questions_data)

        # 處理AI評分結果
        for i, result in enumerate(ai_results):
            q_data = answered_questions[i]
            question = q_data['question']
            question_id = question.get('original_exam_id', '')

            is_correct = result.get('is_correct', False)
            score = result.get('score', 0)
            feedback = result.get('feedback', {})
            if not isinstance(feedback, dict):
                feedback = {'explanation': str(feedback)}
            feedback['concept_mapping'] = q_data.get('concept_mapping', {})

            # 統計正確和錯誤題數
            if is_correct:
                correct_count += 1
                total_score += score
            else:
                wrong_count += 1
                # 收集錯題信息
                wrong_question_info = {
                    'question_id': question.get('id', q_data['index'] + 1),
                    'question_text': question.get('question_text', ''),
                    'question_type': question.get('type', ''),  # 從question對象獲取type
                    'user_answer': q_data['user_answer'],
                    'correct_answer': question.get('correct_answer', ''),
                    'options': question.get('options', []),
                    'image_file': question.get('image_file', ''),
                    'original_exam_id': question.get('original_exam_id', ''),
                    'question_index': q_data['index'],
                    'score': score,
                    'feedback': feedback
                }

                # 如果是子題，添加額外信息
                if question.get('is_sub_question', False):
                    wrong_question_info.update({
                        'is_sub_question': True,
                        'question_number': question.get('question_number', ''),
                        'parent_question_id': question.get('parent_question_id', ''),
                        'parent_question_text': question.get('parent_question_text', ''),
                        'sub_index': q_data.get('sub_index', 0)
                    })

                wrong_questions.append(wrong_question_info)

            # 保存AI評分結果到 answered_questions 中，供後續使用
            q_data['ai_result'] = {
                'is_correct': is_correct,
                'score': score,
                'feedback': feedback
            }

        # AI批量評分完成
    else:
        pass

    # 更新進度狀態為第4階段
    if progress_id:
        update_progress_status(progress_id, False, 4, "正在統計結果...")

    # 計算統計數據
    answered_count = len(answered_questions)
    unanswered_count = len(unanswered_questions)

    # 計算統計數據
    accuracy_rate = (correct_count / total_questions * 100) if total_questions > 0 else 0
    average_score = (total_score / answered_count) if answered_count > 0 else 0

    # 更新或創建SQL記錄
    with sqldb.engine.connect() as conn:
        # 使用從測驗數據獲取的類型
        quiz_template_id = template_id_int  # 使用實際的模板ID

        # 查找現有的quiz_history記錄
        existing_record = conn.execute(text("""
            SELECT id FROM quiz_history
            WHERE user_email = :user_email AND quiz_type = :quiz_type
            ORDER BY created_at DESC LIMIT 1
        """), {
            'user_email': user_email,
            'quiz_type': quiz_type
        }).fetchone()

        if existing_record:
            # 更新現有記錄
            quiz_history_id = existing_record[0]
            conn.execute(text("""
                UPDATE quiz_history
                SET answered_questions = :answered_questions,
                    correct_count = :correct_count,
                    wrong_count = :wrong_count,
                    accuracy_rate = :accuracy_rate,
                    average_score = :average_score,
                    total_time_taken = :time_taken,
                    submit_time = :submit_time,
                    status = 'completed'
                WHERE id = :quiz_history_id
            """), {
                'answered_questions': answered_count,
                'correct_count': correct_count,
                'wrong_count': wrong_count,
                'accuracy_rate': round(accuracy_rate, 2),
                'average_score': round(average_score, 2),
                'time_taken': time_taken,
                                'submit_time': datetime.now(),
                'quiz_history_id': quiz_history_id
            })
        else:
            # 創建新記錄
            # 對於AI生成的考卷，quiz_template_id設為NULL（資料庫允許NULL）
            # 對於傳統考卷，使用整數template_id
            db_quiz_template_id = None if quiz_template_id is None else quiz_template_id

            result = conn.execute(text("""
                INSERT INTO quiz_history
                (quiz_template_id, user_email, quiz_type, total_questions, answered_questions,
                 correct_count, wrong_count, accuracy_rate, average_score, total_time_taken, submit_time, status)
                VALUES (:quiz_template_id, :user_email, :quiz_type, :total_questions, :answered_questions,
                       :correct_count, :wrong_count, :accuracy_rate, :average_score, :total_time_taken, :submit_time, :status)
            """), {
                'quiz_template_id': db_quiz_template_id,
                'user_email': user_email,
                'quiz_type': quiz_type,
                'total_questions': total_questions,
                'answered_questions': answered_count,
                'correct_count': correct_count,
                'wrong_count': wrong_count,
                'accuracy_rate': round(accuracy_rate, 2),
                'average_score': round(average_score, 2),
                'total_time_taken': time_taken,
                'submit_time': datetime.now(),
                'status': 'completed'
            })
            quiz_history_id = result.lastrowid

        # 儲存所有題目的用戶答案到 quiz_answers 表
        # 1. 儲存已作答題目（AI評分結果）
        for i, q_data in enumerate(answered_questions):
            question = q_data['question']
            user_answer = q_data['user_answer']
            question_id = question.get('original_exam_id', '')
            question_type = question.get('type', '')

            # 獲取AI評分結果
            ai_result = q_data.get('ai_result', {})
            is_correct = ai_result.get('is_correct', False)
            score = ai_result.get('score', 0)
            feedback = ai_result.get('feedback', {})

            # 獲取作答時間（秒數）
            answer_time_seconds = q_data.get('answer_time_seconds', 0)

            # 處理 Group 題目的答案格式
            if question_type == 'group':
                # Group 題目的答案可能是陣列，需要轉換為字串
                if isinstance(user_answer, list):
                    user_answer_str = json.dumps(user_answer, ensure_ascii=False)
                else:
                    user_answer_str = str(user_answer)
            else:
                user_answer_str = str(user_answer)

            # 構建用戶答案資料
            answer_data = {
                'answer': user_answer_str,
                'feedback': feedback  # 使用AI批改的feedback
            }

            # 使用新的長答案存儲方法，保持數據完整性
            stored_answer = _store_long_answer(user_answer_str, 'unknown', quiz_history_id, question_id, user_email)

            # 插入到 quiz_answers 表，包含feedback和作答時間
            conn.execute(text("""
                INSERT INTO quiz_answers
                (quiz_history_id, user_email, mongodb_question_id, user_answer, is_correct, score, feedback, answer_time_seconds)
                VALUES (:quiz_history_id, :user_email, :mongodb_question_id, :user_answer, :is_correct, :score, :feedback, :answer_time_seconds)
            """), {
                'quiz_history_id': quiz_history_id,
                'user_email': user_email,
                'mongodb_question_id': question_id,
                'user_answer': stored_answer,  # 使用存儲後的答案引用
                'is_correct': is_correct,
                'score': score,
                'feedback': json.dumps(feedback),  # 將feedback轉換為JSON字符串
                'answer_time_seconds': answer_time_seconds  # 每題作答時間（秒）
            })

        # 2. 儲存未作答題目
        for q_data in unanswered_questions:
            i = q_data['index']
            question = q_data['question']
            question_id = question.get('original_exam_id', '')
            concept_mapping = ensure_question_concept_mapping(
                question,
                question_id=question_id,
                persist=not question.get('is_sub_question', False),
            )

            # 未作答題目：is_correct = False, score = 0
            answer_data = {
                'answer': '',
                'feedback': {}
            }

            # 插入到 quiz_answers 表
            conn.execute(text("""
                INSERT INTO quiz_answers
                (quiz_history_id, user_email, mongodb_question_id, user_answer, is_correct, score, feedback, answer_time_seconds)
                VALUES (:quiz_history_id, :user_email, :mongodb_question_id, :user_answer, :is_correct, :score, :feedback, :answer_time_seconds)
            """), {
                'quiz_history_id': quiz_history_id,
                'user_email': user_email,
                'mongodb_question_id': question_id,
                'user_answer': '',  # 未作答題目答案為空
                'is_correct': False,  # 未作答題目標記為錯誤
                'score': 0,
                'feedback': json.dumps(
                    {'concept_mapping': concept_mapping}, ensure_ascii=False
                ),
                'answer_time_seconds': 0
            })

        # 保留原有的錯題儲存邏輯（向後兼容）
        if wrong_questions:
            for wrong_q in wrong_questions:
                # 使用新的長答案存儲方法，保持數據完整性
                stored_answer = _store_long_answer(wrong_q['user_answer'], 'unknown', quiz_history_id,
                                                wrong_q.get('original_exam_id', ''), user_email)

                conn.execute(text("""
                    INSERT INTO quiz_errors
                    (quiz_history_id, user_email, mongodb_question_id, user_answer,
                     score, time_taken)
                    VALUES (:quiz_history_id, :user_email, :mongodb_question_id,
                           :user_answer, :score, :time_taken)
                """), {
                    'quiz_history_id': quiz_history_id,
                    'user_email': user_email,
                    'mongodb_question_id': wrong_q.get('original_exam_id', ''),
                    'user_answer': stored_answer,  # 使用存儲後的答案引用
                    'score': wrong_q.get('score', 0),
                    'time_taken': 0  # 簡化時間處理
                })

        conn.commit()

    rag_three_way_comparison = {
        "enabled": enable_rag_three_way_comparison,
        "status": "disabled",
        "count": 0,
        "succeeded": 0,
        "failed": 0,
        "records": [],
    }
    if enable_rag_three_way_comparison:
        update_progress_status(progress_id, False, 4, "正在建立 GraphRAG / ChromaDB / 純 LLM 三方比較紀錄...")
        rag_three_way_comparison = _run_submit_rag_three_way_comparisons(
            answered_questions,
            template_id=template_id,
            quiz_history_id=quiz_history_id,
            quiz_type=quiz_type,
            question_source=question_source,
            top_k=rag_compare_top_k,
            max_items=rag_compare_max_items,
        )

    # 更新進度追蹤狀態為完成
    update_progress_status(progress_id, True, 4, "AI批改完成！")

    return jsonify({
        'token': refresh_token(token),
        'message': '測驗提交成功',
        'data': {
            'template_id': template_id,  # 返回模板ID
            'quiz_history_id': quiz_history_id,  # 返回測驗歷史記錄ID
            'result_id': f'result_{quiz_history_id}',  # 返回結果ID（用於前端跳轉）
            'progress_id': progress_id,  # 返回進度追蹤ID
            'total_questions': total_questions,
            'answered_questions': answered_count,
            'unanswered_questions': unanswered_count,
            'correct_count': correct_count,
            'wrong_count': wrong_count,
            'marked_count': 0,  # 暫時設為0，後續可擴展
            'accuracy_rate': round(accuracy_rate, 2),
            'average_score': round(average_score, 2),
            'time_taken': time_taken,
            'rag_three_way_comparison': rag_three_way_comparison,
            'total_time': time_taken,  # 添加總時間字段
            'grading_stages': [
                {'stage': 1, 'name': '試卷批改', 'status': 'completed', 'description': '獲取題目數據完成'},
                {'stage': 2, 'name': '計算分數', 'status': 'completed', 'description': '題目分類完成'},
                {'stage': 3, 'name': '評判知識點', 'status': 'completed', 'description': f'AI評分完成，共評分{answered_count}題'},
                {'stage': 4, 'name': '生成學習計畫', 'status': 'completed', 'description': f'統計完成，正確率{accuracy_rate:.1f}%'}
            ],
            'detailed_results': [
                {
                    'question_index': q_data['index'],
                    'question_text': q_data['question'].get('question_text', ''),
                    'user_answer': q_data['user_answer'],
                    'correct_answer': q_data['question'].get('correct_answer', ''),
                    'is_correct': q_data.get('ai_result', {}).get('is_correct', False),
                    'score': q_data.get('ai_result', {}).get('score', 0),
                    'feedback': q_data.get('ai_result', {}).get('feedback', {}),
                    'concept_mapping': q_data.get('concept_mapping', {}),
                    'primary_concept': (q_data.get('concept_mapping') or {}).get('primary_concept'),
                    'concept_names': (q_data.get('concept_mapping') or {}).get('concept_names', []),
                }
                for q_data in answered_questions
            ]
        }
    })


# 舊的答案截斷方法已移除，現在使用長答案存儲方法保持數據完整性


# 進度追蹤存儲（簡單的內存存儲，生產環境建議使用 Redis）
progress_storage = {}

def update_progress_status(progress_id: str, is_completed: bool, current_stage: int, description: str):
    """更新進度追蹤狀態"""
    progress_storage[progress_id] = {
        'is_completed': is_completed,
        'current_stage': current_stage,
        'stage_description': description,
        'updated_at': time.time()
    }

def get_progress_status(progress_id: str) -> dict:
    """獲取進度追蹤狀態"""
    try:
        # 從進度追蹤存儲中獲取狀態
        return progress_storage.get(progress_id, {
            'current_stage': 1,  # 默認從第一階段開始
            'is_completed': False,
            'stage_description': '正在初始化...'
        })
    except Exception as e:
        print(f"❌ 獲取進度狀態失敗: {e}")
        return None

def _parse_user_answer(user_answer):
    """解析用戶答案，支援多種格式，包括 LONG_ANSWER_ 引用"""
    if isinstance(user_answer, dict):
        return user_answer.get('answer', '')
    elif isinstance(user_answer, str):
        # 處理 LONG_ANSWER_ 引用
        if user_answer.startswith('LONG_ANSWER_'):
            try:
                long_answer_id = int(user_answer.replace('LONG_ANSWER_', ''))
                # 從 long_answers 表查詢完整答案
                with sqldb.engine.connect() as conn:
                    result = conn.execute(text("""
                        SELECT full_answer FROM long_answers
                        WHERE id = :long_answer_id
                    """), {
                        'long_answer_id': long_answer_id
                    }).fetchone()

                    if result:
                        return result[0]  # 返回完整的答案內容
                    else:
                        return f"[長答案載入失敗: {user_answer}]"
            except (ValueError, Exception) as e:
                print(f"❌ 解析長答案引用失敗: {e}")
                return f"[長答案解析錯誤: {user_answer}]"

        # 處理 JSON 格式
        elif user_answer.startswith('['):
            try:
                return json.loads(user_answer)
            except json.JSONDecodeError:
                return user_answer
    return user_answer

def _store_long_answer(user_answer: any, question_type: str, quiz_history_id: int, question_id: str, user_email: str) -> str:
    """
    存儲長答案到專門的表中，保持數據完整性

    參數：
    - user_answer: 原始用戶答案
    - question_type: 題目類型
    - quiz_history_id: 測驗歷史ID
    - question_id: 題目ID
    - user_email: 用戶郵箱

    返回：
    - 存儲引用ID或標識符
    """
    try:
        answer_str = str(user_answer)

        # 如果答案不長，直接返回
        if len(answer_str) <= 10000:
            return answer_str

        # 對於長答案，存儲到專門的表中
        with sqldb.engine.connect() as conn:
            # 創建長答案存儲表（如果不存在）
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS long_answers (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    quiz_history_id INT NOT NULL,
                    question_id VARCHAR(255) NOT NULL,
                    user_email VARCHAR(255) NOT NULL,
                    question_type VARCHAR(50) NOT NULL,
                    full_answer LONGTEXT NOT NULL,
                    answer_hash VARCHAR(64) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_quiz_question (quiz_history_id, question_id),
                    INDEX idx_user (user_email)
                )
            """))

            # 計算答案的哈希值作為唯一標識
            answer_hash = hashlib.md5(answer_str.encode()).hexdigest()

            # 檢查是否已經存儲過相同的答案
            existing = conn.execute(text("""
                SELECT id FROM long_answers
                WHERE quiz_history_id = :quiz_history_id AND question_id = :question_id
            """), {
                'quiz_history_id': quiz_history_id,
                'question_id': question_id
            }).fetchone()

            if existing:
                # 如果已存在，返回引用標識
                return f"LONG_ANSWER_{existing[0]}"
            else:
                # 存儲新的長答案
                result = conn.execute(text("""
                    INSERT INTO long_answers
                    (quiz_history_id, question_id, user_email, question_type, full_answer, answer_hash)
                    VALUES (:quiz_history_id, :question_id, :user_email, :question_type, :full_answer, :answer_hash)
                """), {
                    'quiz_history_id': quiz_history_id,
                    'question_id': question_id,
                    'user_email': user_email,
                    'question_type': question_type,
                    'full_answer': answer_str,
                    'answer_hash': answer_hash
                })

                long_answer_id = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
                conn.commit()
                return f"LONG_ANSWER_{long_answer_id}"

    except Exception as e:
        print(f"❌ 存儲長答案失敗: {e}")
        # 如果存儲失敗，返回截斷的答案（但保持數據完整性）
        answer_str = str(user_answer)
        if len(answer_str) > 10000:
            # 返回截斷的答案，但添加錯誤標記
            truncated_answer = answer_str[:9000] + "...[存儲失敗，答案已截斷]"
            print(f"⚠️ 長答案存儲失敗，使用截斷方式: {len(answer_str)} -> {len(truncated_answer)} 字符")
            return truncated_answer
        else:
            # 如果答案不長，直接返回
            return answer_str


@quiz_bp.route('/get-quiz-result/<result_id>', methods=['GET', 'OPTIONS'])
def get_quiz_result(result_id):
    """根據結果ID獲取測驗結果 API - 優化版本"""
    if request.method == 'OPTIONS':
        return jsonify({'token': None, 'success': True}), 204

    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return jsonify({'token': None, 'message': '未提供token'}), 401

    token = auth_header.split(" ")[1]

    # 從result_id中提取quiz_history_id
    # result_id格式: result_123
    if not result_id.startswith('result_'):
        return jsonify({'token': None, 'message': '無效的結果ID格式'}), 400

    try:
        quiz_history_id = int(result_id.split('_')[1])
    except (ValueError, IndexError):
        return jsonify({'token': None, 'message': '無效的結果ID格式'}), 400

    # 從SQL獲取測驗結果
    with sqldb.engine.connect() as conn:
        # 獲取測驗歷史記錄
        _ensure_quiz_templates_question_source_column(conn)
        history_result = conn.execute(text("""
            SELECT qh.id, qh.quiz_template_id, qh.user_email, qh.quiz_type,
                   qh.total_questions, qh.answered_questions, qh.correct_count, qh.wrong_count,
                   qh.accuracy_rate, qh.average_score, qh.total_time_taken,
                   qh.submit_time, qh.status, qh.created_at,
                   qt.question_ids, qt.school, qt.department, qt.year, qt.question_source
            FROM quiz_history qh
            LEFT JOIN quiz_templates qt ON qh.quiz_template_id = qt.id
            WHERE qh.id = :quiz_history_id
        """), {
            'quiz_history_id': quiz_history_id
        }).fetchone()

        if not history_result:
            return jsonify({'token': None, 'message': '測驗結果不存在'}), 404


        # 獲取所有題目的用戶答案（從quiz_answers表）
        answers_result = conn.execute(text("""
            SELECT mongodb_question_id, user_answer, is_correct, score, feedback, answer_time_seconds, created_at
            FROM quiz_answers
            WHERE quiz_history_id = :quiz_history_id
            ORDER BY created_at
        """), {
            'quiz_history_id': quiz_history_id
        }).fetchall()


        # 獲取錯題詳情（從quiz_errors表，向後兼容）
        error_result = conn.execute(text("""
            SELECT mongodb_question_id, user_answer, score, time_taken, created_at
            FROM quiz_errors
            WHERE quiz_history_id = :quiz_history_id
            ORDER BY created_at
        """), {
            'quiz_history_id': quiz_history_id
        }).fetchall()


        # 構建答案字典，方便查詢
        answers_dict = {}
        for answer in answers_result:
            answers_dict[str(answer[0])] = {
                'user_answer': json.loads(answer[1]) if answer[1] else '',
                'is_correct': bool(answer[2]),
                'score': float(answer[3]) if answer[3] else 0,
                'feedback': json.loads(answer[4]) if answer[4] else {}, # 將JSON字符串轉換回Python字典
                'answer_time_seconds': answer[5] if answer[5] else 0,
                'answer_time': answer[6].isoformat() if answer[6] else None
            }

        # 獲取題目ID列表
        question_ids_raw = history_result[14]
        question_ids = []
        if question_ids_raw:
            try:
                question_ids = json.loads(question_ids_raw)
            except json.JSONDecodeError as e:
                print(f"❌ JSON解析失敗: {e}")
                question_ids = []

        question_source = history_result[18] if len(history_result) > 18 and history_result[18] else DEFAULT_QUESTION_SOURCE
        question_collection, question_source = _get_question_collection(question_source)

        if not question_ids:
            result_data = {
                'quiz_history_id': history_result[0],
                'quiz_template_id': history_result[1],
                'user_email': history_result[2],
                'quiz_type': history_result[3],
                'total_questions': history_result[4],
                'answered_questions': history_result[5],
                'unanswered_questions': history_result[4] - history_result[5],
                'correct_count': history_result[6],
                'wrong_count': history_result[7],
                'accuracy_rate': float(history_result[8]) if history_result[8] else 0,
                'average_score': float(history_result[9]) if history_result[9] else 0,
                'total_time_taken': history_result[10] if history_result[10] else 0,
                'submit_time': history_result[11].isoformat() if history_result[11] else None,
                'status': history_result[12],
                'created_at': history_result[13].isoformat() if history_result[13] else None,
                'school': history_result[15] if history_result[15] else '',
                'department': history_result[16] if history_result[16] else '',
                'year': history_result[17] if history_result[17] else '',
                'question_source': question_source,
                'questions': [],
                'errors': []
            }

            return jsonify({
                'token': refresh_token(token),
                'success': True,
                'message': '獲取測驗結果成功（僅基本統計）',
                'data': result_data
            }), 200

        # 獲取所有題目的詳細資訊
        all_questions = []
        errors = []

        for i, question_id in enumerate(question_ids):

            # 從MongoDB獲取題目詳情
            question_detail = {}
            try:
                # 安全地處理 ObjectId 查詢
                if isinstance(question_id, str) and len(question_id) == 24:
                    exam_question = question_collection.find_one({"_id": ObjectId(question_id)})
                else:
                    exam_question = question_collection.find_one({"_id": question_id})

                if exam_question:
                    # 使用與 create-quiz 相同的題目處理邏輯
                    exam_type = exam_question.get('type', 'single')
                    if exam_type == 'group':
                        # 題組：保留群組題外層資訊，展開子題但一併回傳
                        group_question_text = build_full_question_text(exam_question) or exam_question.get('group_question_text') or exam_question.get('question_text', '')
                        if not group_question_text:
                            group_question_text = f"題組 {i+1} (無題目文字)"

                        micro_concepts = exam_question.get('micro_concepts', [])
                        key_points = exam_question.get('key-points', '')
                        # 處理 key-points 可能是陣列或字串的情況
                        if isinstance(key_points, list):
                            key_points = ', '.join(key_points) if key_points else ''
                        parent_id = str(exam_question.get('_id', ''))

                        # 構建子題清單
                        sub_qs_raw = exam_question.get('sub_questions', []) or []
                        sub_qs = []
                        for sub in sub_qs_raw:
                            sub_options = sub.get('options', [])
                            if isinstance(sub_options, str):
                                sub_options = [opt.strip() for opt in sub_options.split(',') if opt.strip()]
                            elif not isinstance(sub_options, list):
                                sub_options = []

                            sub_image = sub.get('image_file', '')

                            sub_qs.append({
                                'question_number': sub.get('question_number', ''),
                                'question_text': sub.get('question_text', ''),
                                'options': sub_options,
                                'answer': sub.get('answer', ''),
                                'answer_type': sub.get('answer_type', 'single-choice'),
                                'image_file': sub_image,
                                'detail_answer': sub.get('detail-answer', ''),
                                'key_points': ', '.join(sub.get('key-points', [])) if isinstance(sub.get('key-points', []), list) else sub.get('key-points', ''),
                                'difficulty_level': sub.get('difficulty level', sub.get('difficulty_level', '')),
                                'original_exam_id': parent_id
                            })

                        question_detail = {
                            'type': 'group',
                            'group_question_text': group_question_text,
                            'micro_concepts': micro_concepts,
                            'key_points': key_points,
                            'original_exam_id': parent_id,
                            'layout_blocks': exam_question.get('layout_blocks', []),
                            'latex_assets': _ensure_renderable_assets(exam_question),
                            'shared_asset_refs': _get_shared_asset_refs(exam_question),
                            'source_pages': exam_question.get('source_pages', []),
                            'sub_questions': sub_qs,
                            'image_file': exam_question.get('image_file', '')
                        }
                    else:
                        # 單題處理
                        question_text = exam_question.get('question_text', '')
                        if not question_text:
                            if exam_question.get('answer'):
                                question_text = f"題目 {i+1}: {exam_question.get('answer', '')[:200]}..."
                            else:
                                question_text = f"題目 {i+1} (無題目文字)"

                        question_detail = {
                            'type': _resolve_question_type(exam_question),
                            'question_text': build_full_question_text(exam_question) or question_text,
                            'options': _normalize_options(exam_question.get('options', [])),
                            'correct_answer': exam_question.get('answer', ''),
                            'image_file': exam_question.get('image_file', ''),
                            'key_points': _normalize_key_points(exam_question.get('key-points', exam_question.get('key_points', ''))),
                            'original_exam_id': str(exam_question.get('_id', '')),
                            'detail_answer': exam_question.get('detail-answer', exam_question.get('detail_answer', '')),
                            'latex_assets': _ensure_renderable_assets(exam_question),
                            'shared_asset_refs': _get_shared_asset_refs(exam_question),
                            'grouped_subquestions': exam_question.get('grouped_subquestions', []),
                            'continuation_note': exam_question.get('continuation_note', ''),
                            'layout_blocks': exam_question.get('layout_blocks', []),
                            'source_pages': exam_question.get('source_pages', []),
                            'question_number': exam_question.get('question_number', ''),
                            'school': exam_question.get('school', ''),
                            'department': exam_question.get('department', ''),
                            'year': exam_question.get('year', '')
                        }
                else:
                    question_detail = {
                        'question_text': f'題目 {i + 1}',
                        'options': [],
                        'correct_answer': '',
                        'image_file': '',
                        'key_points': ''
                    }
            except Exception as e:
                print(f"⚠️ 獲取題目詳情失敗: {e}")
                question_detail = {
                    'question_text': f'題目 {i + 1}',
                    'options': [],
                    'correct_answer': '',
                    'image_file': '',
                    'key_points': ''
                }

            # 獲取用戶答案信息
            question_id_str = str(question_id)
            answer_info = answers_dict.get(question_id_str, {})

            # 構建題目資訊
            if question_detail.get('type') == 'group':
                # GROUP 題特殊處理
                sub_questions = question_detail.get('sub_questions', [])

                # 處理子題答案
                processed_sub_questions = []
                for sub_idx, sub_question in enumerate(sub_questions):
                    # 查找子題的答案（格式：主題ID_sub_子題索引）
                    sub_question_id = f"{question_id_str}_{sub_idx}"
                    sub_answer_info = answers_dict.get(sub_question_id, {})

                    # 構建子題資訊
                    processed_sub_question = {
                        'question_number': sub_question.get('question_number', ''),
                        'question_text': sub_question.get('question_text', ''),
                        'answer_type': sub_question.get('answer_type', 'short-answer'),
                        'options': sub_question.get('options', []),
                        'correct_answer': sub_question.get('answer', ''),
                        'image_file': sub_question.get('image_file', ''),
                        'key_points': sub_question.get('key_points', ''),
                        'difficulty_level': sub_question.get('difficulty_level', ''),
                        'is_correct': sub_answer_info.get('is_correct', False),
                        'user_answer': sub_answer_info.get('user_answer', ''),
                        'score': sub_answer_info.get('score', 0),
                        'answer_time_seconds': sub_answer_info.get('answer_time_seconds', 0),
                        'feedback': sub_answer_info.get('feedback', {})
                    }
                    processed_sub_questions.append(processed_sub_question)

                # 處理 Group 題目的用戶答案
                group_user_answer = _parse_user_answer(answer_info.get('user_answer', ''))

                question_info = {
                    'question_id': question_id_str,
                    'question_index': i,
                    'type': 'group',
                    'question_text': question_detail.get('group_question_text', ''),  # 添加 question_text 欄位
                    'group_question_text': question_detail.get('group_question_text', ''),
                    'micro_concepts': question_detail.get('micro_concepts', []),
                    'key_points': question_detail.get('key_points', ''),
                    'image_file': question_detail.get('image_file', ''),
                    'sub_questions': processed_sub_questions,
                    'is_correct': answer_info.get('is_correct', False),
                    'is_marked': False,  # 目前沒有標記功能
                    'user_answer': group_user_answer,
                    'score': answer_info.get('score', 0),
                    'answer_time_seconds': answer_info.get('answer_time_seconds', 0),
                    'answer_time': answer_info.get('answer_time')
                }
            else:
                # 單題處理
                single_user_answer = _parse_user_answer(answer_info.get('user_answer', ''))

                question_info = {
                    'question_id': question_id_str,
                    'question_index': i,
                    'type': question_detail.get('type', 'single-choice'),
                    'answer_type': question_detail.get('type', 'single-choice'),
                    'question_text': question_detail.get('question_text', ''),
                    'options': question_detail.get('options', []),
                    'correct_answer': question_detail.get('correct_answer', ''),
                    'image_file': question_detail.get('image_file', ''),
                    'key_points': question_detail.get('key_points', ''),
                    'detail_answer': question_detail.get('detail_answer', ''),
                    'latex_assets': _ensure_renderable_assets(question_detail),
                    'shared_asset_refs': _get_shared_asset_refs(question_detail),
                    'grouped_subquestions': question_detail.get('grouped_subquestions', []),
                    'continuation_note': question_detail.get('continuation_note', ''),
                    'layout_blocks': question_detail.get('layout_blocks', []),
                    'source_pages': question_detail.get('source_pages', []),
                    'question_number': question_detail.get('question_number', ''),
                    'school': question_detail.get('school', ''),
                    'department': question_detail.get('department', ''),
                    'year': question_detail.get('year', ''),
                    'is_correct': answer_info.get('is_correct', False),
                    'is_marked': False,  # 目前沒有標記功能
                    'user_answer': single_user_answer,
                    'score': answer_info.get('score', 0),
                    'answer_time_seconds': answer_info.get('answer_time_seconds', 0),
                    'answer_time': answer_info.get('answer_time')
                }

            # 檢查是否為錯題
            if not answer_info.get('is_correct', False):
                errors.append(question_info)

            all_questions.append(question_info)

        # 計算統計數據
        total_questions = history_result[4]
        answered_questions = history_result[5]
        correct_count = history_result[6]
        wrong_count = history_result[7]
        unanswered_count = total_questions - answered_questions

        result_data = {
            'quiz_history_id': history_result[0],
            'quiz_template_id': history_result[1],
            'user_email': history_result[2],
            'quiz_type': history_result[3],
            'total_questions': total_questions,
            'answered_questions': answered_questions,
            'unanswered_questions': unanswered_count,
            'correct_count': correct_count,
            'wrong_count': wrong_count,
            'accuracy_rate': float(history_result[8]) if history_result[8] else 0,
            'average_score': float(history_result[9]) if history_result[9] else 0,
            'total_time_taken': history_result[10] if history_result[10] else 0,
            'submit_time': history_result[11].isoformat() if history_result[11] else None,
            'status': history_result[12],
            'created_at': history_result[13].isoformat() if history_result[13] else None,
            'school': history_result[15] if history_result[15] else '',
            'department': history_result[16] if history_result[16] else '',
            'year': history_result[17] if history_result[17] else '',
            'question_source': question_source,
            'questions': all_questions,  # 所有題目的詳細資訊
            'errors': errors  # 錯題列表
        }

        return jsonify({
            'token': refresh_token(token),
            'success': True,
            'message': '獲取測驗結果成功',
            'data': result_data
        }), 200


# 删除 /test-quiz-result API - 与 /get-quiz-result 功能重复

@quiz_bp.route('/create-quiz', methods=['POST', 'OPTIONS'])
def create_quiz():
    """創建測驗 API - 支持用戶填寫學校、科系、年份"""
    if request.method == 'OPTIONS':
        return jsonify({'token': None, 'message': 'CORS preflight'}), 200
    token = request.headers.get('Authorization')
    token = token.split(" ")[1]
    try:
        # 驗證token
        user_email = verify_token(token)

        # 獲取請求參數
        data = request.get_json() or {}
        question_collection, question_source = _get_question_collection(data)
        quiz_type = data.get('type')  # 'knowledge' 或 'pastexam'


        # 獲取用戶填寫的學校、科系、年份信息
        school = data.get('school', '')
        department = data.get('department', '')
        year = data.get('year', '')

        if quiz_type == 'knowledge':
            # 知識點測驗
            topic = data.get('topic')
            difficulty = data.get('difficulty', 'medium')
            count = int(data.get('count', 20))

            if not topic:
                return jsonify({'token': None, 'message': '缺少知識點參數'}), 400

            # 從MongoDB獲取符合條件的考題
            # 使用正確的欄位名稱：key-points
            query = {"key-points": topic}
            available_exams = list(question_collection.find(query).limit(count * 2))

            if len(available_exams) < count:
                available_exams = list(question_collection.find({}).limit(count))

            selected_exams = random.sample(available_exams, min(count, len(available_exams)))
            quiz_title = f"{topic} - {difficulty} - {count}題"

            # 知識點測驗的學校、科系、年份
            if not school:
                school = '知識點測驗'
            if not department:
                department = topic or '通用'
            if not year:
                year = '不限年份'

        elif quiz_type == 'pastexam':
            # 考古題測驗
            if not all([school, year, department]):
                return jsonify({'token': None, 'message': '考古題測驗必須填寫學校、年份、系所'}), 400

            # 特殊處理 demo 選項
            if school == 'demo' and year == '114' and department == 'demo':
                # 返回固定的 7 題 demo 題目
                demo_question_ids = [
                    "6905deac7292fdbd94102c01",
                    "6905deac7292fdbd94102c02",
                    "6905deac7292fdbd94102c03",
                    "6905deac7292fdbd94102c04",
                    "6905deac7292fdbd94102c05",
                    "6905deac7292fdbd94102c06",
                    "6905deac7292fdbd94102c07"
                ]
                selected_exams = []
                for q_id in demo_question_ids:
                    try:
                        object_id = ObjectId(q_id)
                        question_doc = question_collection.find_one({"_id": object_id})
                        if question_doc:
                            selected_exams.append(question_doc)
                    except Exception as e:
                        print(f"⚠️ 載入 demo 題目失敗 (ID: {q_id}): {e}")
                        continue

                if not selected_exams:
                    return jsonify({'token': None, 'message': '找不到 demo 題目'}), 404

                quiz_title = f"Demo - 114年 - Demo"
            else:
                # 從MongoDB獲取符合條件的考古題
                query = {
                    "school": school,
                    "year": year,
                    "department": department
                }
                selected_exams = list(question_collection.find(query))

                if not selected_exams:
                    print(f"❌ 找不到符合條件的考題: {query}")
                    return jsonify({'token': None, 'message': '找不到符合條件的考題'}), 404

                quiz_title = f"{school} - {year}年 - {department}"

        else:
            return jsonify({'token': None, 'message': '無效的測驗類型'}), 400

        # 轉換為標準化的題目格式
        questions = []
        for i, exam in enumerate(selected_exams):
            # 正確讀取題目類型 - type用來判斷單一/題組，answer_type用來判斷單選/多選
            exam_type = exam.get('type', 'single')  # type: single/group
            answer_type = exam.get('answer_type', 'single-choice')  # answer_type: single-choice/multiple-choice等
            if exam_type == 'group':  # 使用type欄位判斷是否為題組
                # 題組：保留群組題外層資訊，展開子題但一併回傳
                group_question_text = build_full_question_text(exam) or exam.get('group_question_text') or exam.get('question_text', '')
                if not group_question_text:
                    print(f"⚠️ 警告：題組 {i+1} (ID: {exam.get('_id')}) 的 group_question_text 和 question_text 都為空")
                    group_question_text = f"題組 {i+1} (無題目文字)"

                micro_concepts = exam.get('micro_concepts', [])
                key_points = exam.get('key-points', '')
                # 處理 key-points 可能是陣列或字串的情況
                if isinstance(key_points, list):
                    key_points = ', '.join(key_points) if key_points else ''
                parent_id = str(exam.get('_id', ''))

                # 構建子題清單
                sub_qs_raw = exam.get('sub_questions', []) or []
                sub_qs = []
                for sub in sub_qs_raw:
                    sub_options = sub.get('options', [])
                    if isinstance(sub_options, str):
                        sub_options = [opt.strip() for opt in sub_options.split(',') if opt.strip()]
                    elif not isinstance(sub_options, list):
                        sub_options = []

                    sub_image = sub.get('image_file', '')
                    # 處理子題圖片 - 轉換為 base64
                    sub_image_data_list = []
                    if sub_image and sub_image not in ['沒有圖片', '不需要圖片', '不須圖片', '不須照片', '沒有考卷', '']:
                        sub_image_filenames = []
                        if isinstance(sub_image, list):
                            sub_image_filenames = [img for img in sub_image if img and img not in ['沒有圖片', '不需要圖片', '不須圖片', '不須照片', '沒有考卷', '']]
                        elif isinstance(sub_image, str):
                            sub_image_filenames = [sub_image]

                        # 將每個圖片檔案轉換為 base64
                        for sub_image_filename in sub_image_filenames:
                            sub_image_base64 = get_image_base64(sub_image_filename)
                            if sub_image_base64:
                                # 判斷圖片格式
                                image_ext = os.path.splitext(sub_image_filename)[1].lower()
                                mime_type = 'image/jpeg'
                                if image_ext in ['.png']:
                                    mime_type = 'image/png'
                                elif image_ext in ['.gif']:
                                    mime_type = 'image/gif'
                                elif image_ext in ['.webp']:
                                    mime_type = 'image/webp'

                                sub_image_data_list.append(f"data:{mime_type};base64,{sub_image_base64}")

                    # 如果只有一張圖片，直接返回字串；多張圖片返回陣列
                    if len(sub_image_data_list) == 1:
                        sub_image_final = sub_image_data_list[0]
                    elif len(sub_image_data_list) > 1:
                        sub_image_final = sub_image_data_list
                    else:
                        sub_image_final = ''

                    # 處理子題的 key-points
                    sub_key_points = sub.get('key-points', '')
                    if isinstance(sub_key_points, list):
                        sub_key_points = ', '.join(sub_key_points) if sub_key_points else ''

                    sub_qs.append({
                        'question_number': sub.get('question_number', ''),
                        'question_text': sub.get('question_text', ''),
                        'options': sub_options,
                        'answer': sub.get('answer', ''),
                        'answer_type': sub.get('answer_type', 'single-choice'),
                        'image_file': sub_image_final,
                        'detail_answer': sub.get('detail-answer', ''),
                        'key_points': sub_key_points,
                        'difficulty_level': sub.get('difficulty level', sub.get('difficulty_level', '')),
                        'original_exam_id': parent_id,
                        'question_source': question_source
                    })

                group_question = {
                    'id': i + 1,
                    'type': 'group',
                    'group_question_text': group_question_text,
                    'micro_concepts': micro_concepts,
                    'key_points': key_points,
                    'original_exam_id': parent_id,
                    'layout_blocks': exam.get('layout_blocks', []),
                    'latex_assets': _ensure_renderable_assets(exam),
                    'shared_asset_refs': _get_shared_asset_refs(exam),
                    'source_pages': exam.get('source_pages', []),
                    'sub_questions': sub_qs,
                    'question_source': question_source
                }

                # 題組外層若也有圖片，轉換為 base64
                group_image_file = exam.get('image_file', '')
                group_image_data_list = []
                if group_image_file and group_image_file not in ['沒有圖片', '不需要圖片', '不須圖片', '不須照片', '沒有考卷', '']:
                    group_image_filenames = []
                    if isinstance(group_image_file, list):
                        group_image_filenames = [img for img in group_image_file if img and img not in ['沒有圖片', '不需要圖片', '不須圖片', '不須照片', '沒有考卷', '']]
                    elif isinstance(group_image_file, str):
                        group_image_filenames = [group_image_file]

                    # 將每個圖片檔案轉換為 base64
                    for group_image_filename in group_image_filenames:
                        group_image_base64 = get_image_base64(group_image_filename)
                        if group_image_base64:
                            # 判斷圖片格式
                            image_ext = os.path.splitext(group_image_filename)[1].lower()
                            mime_type = 'image/jpeg'
                            if image_ext in ['.png']:
                                mime_type = 'image/png'
                            elif image_ext in ['.gif']:
                                mime_type = 'image/gif'
                            elif image_ext in ['.webp']:
                                mime_type = 'image/webp'

                            group_image_data_list.append(f"data:{mime_type};base64,{group_image_base64}")

                    # 如果只有一張圖片，直接返回字串；多張圖片返回陣列
                    if len(group_image_data_list) == 1:
                        group_question['image_file'] = group_image_data_list[0]
                    elif len(group_image_data_list) > 1:
                        group_question['image_file'] = group_image_data_list
                    else:
                        group_question['image_file'] = ''
                else:
                    group_question['image_file'] = ''

                questions.append(group_question)
            else:
                # 單題：保留原始 type / answer_type，避免 coding-answer 被誤判
                question_type = _resolve_question_type(exam)

                # 調試信息：檢查題目文字
                question_text = exam.get('question_text', '')
                if not question_text:
                    print(f"⚠️ 警告：題目 {i+1} (ID: {exam.get('_id')}) 的 question_text 為空")
                    print(f"   學校: {exam.get('school')}, 科系: {exam.get('department')}, 年份: {exam.get('year')}")
                    print(f"   答案: {exam.get('answer', '')[:100]}...")
                    # 嘗試從其他欄位獲取題目文字
                    if exam.get('answer'):
                        question_text = f"題目 {i+1}: {exam.get('answer', '')[:200]}..."
                    else:
                        question_text = f"題目 {i+1} (無題目文字)"

                question = {
                    'id': i + 1,
                    'question_text': build_full_question_text(exam) or question_text,
                    'type': question_type,
                    'options': _normalize_options(exam.get('options')),
                    'correct_answer': exam.get('answer', ''),
                    'original_exam_id': str(exam.get('_id', '')),
                    'image_file': exam.get('image_file'),
                    'key_points': _normalize_key_points(exam.get('key-points', exam.get('key_points', ''))),
                    'answer_type': question_type,
                    'detail_answer': exam.get('detail-answer', exam.get('detail_answer', '')),
                    'latex_assets': _ensure_renderable_assets(exam),
                    'shared_asset_refs': _get_shared_asset_refs(exam),
                    'grouped_subquestions': exam.get('grouped_subquestions', []),
                    'continuation_note': exam.get('continuation_note', ''),
                    'layout_blocks': exam.get('layout_blocks', []),
                    'source_pages': exam.get('source_pages', []),
                    'question_number': exam.get('question_number', ''),
                    'school': exam.get('school', ''),
                    'department': exam.get('department', ''),
                    'year': exam.get('year', ''),
                    'question_source': question_source
                }

                # 處理選項格式
                question['options'] = _normalize_options(question['options'])

                # 處理圖片檔案（單題）- 轉換為 base64
                image_file = exam.get('image_file', '')
                image_data_list = []
                if image_file and image_file not in ['沒有圖片', '不需要圖片', '不須圖片', '不須照片', '沒有考卷', '']:
                    image_filenames = []
                    if isinstance(image_file, list):
                        image_filenames = [img for img in image_file if img and img not in ['沒有圖片', '不需要圖片', '不須圖片', '不須照片', '沒有考卷', '']]
                    elif isinstance(image_file, str):
                        image_filenames = [image_file]

                    # 將每個圖片檔案轉換為 base64
                    for image_filename in image_filenames:
                        image_base64 = get_image_base64(image_filename)
                        if image_base64:
                            # 判斷圖片格式
                            image_ext = os.path.splitext(image_filename)[1].lower()
                            mime_type = 'image/jpeg'
                            if image_ext in ['.png']:
                                mime_type = 'image/png'
                            elif image_ext in ['.gif']:
                                mime_type = 'image/gif'
                            elif image_ext in ['.webp']:
                                mime_type = 'image/webp'

                            image_data_list.append(f"data:{mime_type};base64,{image_base64}")

                    # 如果只有一張圖片，直接返回字串；多張圖片返回陣列
                    if len(image_data_list) == 1:
                        question['image_file'] = image_data_list[0]
                    elif len(image_data_list) > 1:
                        question['image_file'] = image_data_list
                    else:
                        question['image_file'] = ''
                else:
                    question['image_file'] = ''

                questions.append(question)

        # 生成測驗ID
        quiz_id = str(uuid.uuid4())


        # 在SQL中創建quiz_history初始記錄
        try:
            with sqldb.engine.connect() as conn:
                # 檢查並創建 quiz_templates 表
                try:
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS quiz_templates (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            user_email VARCHAR(255) NOT NULL,
                            template_type ENUM('knowledge', 'pastexam') NOT NULL,
                            question_ids JSON NOT NULL,
                            question_source VARCHAR(255) DEFAULT 'test5',
                            school VARCHAR(100) DEFAULT '',
                            department VARCHAR(100) DEFAULT '',
                            year VARCHAR(20) DEFAULT '',
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            INDEX idx_user_email (user_email),
                            INDEX idx_template_type (template_type),
                            INDEX idx_question_source (question_source),
                            INDEX idx_school (school),
                            INDEX idx_department (department),
                            INDEX idx_year (year),
                            INDEX idx_created_at (created_at)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    """))
                    conn.commit()

                except Exception as e:
                    print(f"⚠️ 創建 quiz_templates 表失敗: {e}")

                _ensure_quiz_templates_question_source_column(conn)

                # 檢查並創建 quiz_history 表
                try:
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS quiz_history (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            quiz_template_id INT NULL,
                            user_email VARCHAR(255) NOT NULL,
                            quiz_type ENUM('knowledge', 'pastexam') NOT NULL,
                            total_questions INT DEFAULT 0,
                            answered_questions INT DEFAULT 0,
                            correct_count INT DEFAULT 0,
                            wrong_count INT DEFAULT 0,
                            accuracy_rate DECIMAL(5,2) DEFAULT 0,
                            average_score DECIMAL(5,2) DEFAULT 0,
                            total_time_taken INT DEFAULT 0,
                            submit_time DATETIME NOT NULL,
                            status ENUM('incomplete', 'completed', 'abandoned') DEFAULT 'incomplete',
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (quiz_template_id) REFERENCES quiz_templates(id) ON DELETE SET NULL,
                            INDEX idx_user_email (user_email),
                            INDEX idx_quiz_template_id (quiz_template_id),
                            INDEX idx_submit_time (submit_time)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    """))
                    conn.commit()

                except Exception as e:
                    print(f"⚠️ 創建 quiz_history 表失敗: {e}")

                # 檢查並創建 quiz_errors 表
                try:
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS quiz_errors (
                            error_id INT AUTO_INCREMENT PRIMARY KEY,
                            quiz_history_id INT NOT NULL,
                            user_email VARCHAR(255) NOT NULL,
                            mongodb_question_id VARCHAR(50) NOT NULL,
                            user_answer TEXT,
                            score DECIMAL(5,2) DEFAULT 0,
                            time_taken INT DEFAULT 0,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (quiz_history_id) REFERENCES quiz_history(id) ON DELETE CASCADE,
                            INDEX idx_user_email (user_email),
                            INDEX idx_mongodb_question_id (mongodb_question_id),
                            INDEX idx_created_at (created_at)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    """))
                    conn.commit()

                except Exception as e:
                    print(f"⚠️ 創建 quiz_errors 表失敗: {e}")

                # 創建考卷模板
                question_ids = [str(q.get('original_exam_id', '')) for q in questions if q.get('original_exam_id')]

                template_result = conn.execute(text("""
                    INSERT INTO quiz_templates
                    (user_email, template_type, question_ids, question_source, school, department, year)
                    VALUES (:user_email, :template_type, :question_ids, :question_source, :school, :department, :year)
                """), {
                    'user_email': user_email,
                    'template_type': quiz_type,
                    'question_ids': json.dumps(question_ids),
                    'question_source': question_source,
                    'school': school,
                    'department': department,
                    'year': year
                })
                conn.commit()

                template_id = template_result.lastrowid


                # 創建初始的quiz_history記錄
                conn.execute(text("""
                    INSERT INTO quiz_history
                    (quiz_template_id, user_email, quiz_type, total_questions, answered_questions,
                     correct_count, wrong_count, accuracy_rate, average_score, submit_time, status)
                    VALUES (:quiz_template_id, :user_email, :quiz_type, :total_questions, :answered_questions,
                           :correct_count, :wrong_count, :accuracy_rate, :average_score, :submit_time, :status)
                """), {
                    'quiz_template_id': template_id,
                    'user_email': user_email,
                    'quiz_type': quiz_type,
                    'total_questions': len(questions),
                    'answered_questions': 0,
                    'correct_count': 0,
                    'wrong_count': 0,
                    'accuracy_rate': 0,
                    'average_score': 0,
                    'submit_time': datetime.now(),
                    'status': 'incomplete'
                })
                conn.commit()


        except Exception as sql_error:
            print(f"⚠️ SQL初始記錄創建失敗: {sql_error}")
            # SQL創建失敗不影響主要功能

        return jsonify({
            'token': refresh_token(token),
            'message': '測驗創建成功',
            'quiz_id': quiz_id,
            'template_id': template_id,  # 返回模板ID
            'title': quiz_title,
            'school': school,
            'department': department,
            'year': year,
            'question_source': question_source,
            'question_count': len(questions),
            'time_limit': 60,
            'questions': questions  # 直接返回题目数据
        }), 200

    except Exception as e:
        print(f"❌ 創建測驗時發生錯誤: {str(e)}")
        return jsonify({'token': None, 'message': f'創建測驗失敗: {str(e)}'}), 500

def get_image_base64(image_filename):
    """讀取圖片檔案並轉換為 base64 編碼"""
    try:
        # 檢查檔案名稱是否為空
        if not image_filename or not image_filename.strip():
            return None

        # 取得當前檔案所在目錄，圖片在同層的 picture 資料夾
        current_dir = os.path.dirname(os.path.abspath(__file__))
        image_path = os.path.join(current_dir, 'picture', image_filename)

        if os.path.exists(image_path):
            with open(image_path, 'rb') as image_file:
                image_data = image_file.read()
                base64_encoded = base64.b64encode(image_data).decode('utf-8')
                return base64_encoded
        else:
            # print(f"圖片檔案不存在: {image_path}")
            return None
    except Exception as e:
        print(f"讀取圖片時發生錯誤: {str(e)}")
        return None

# 删除 /get-quiz API - 前端不再使用，功能已被 create-quiz 替代

@quiz_bp.route('/question-sources', methods=['GET', 'OPTIONS'])
def get_question_sources():
    """Return MongoDB collections that can be selected as question sources."""
    if request.method == 'OPTIONS':
        return jsonify({'token': None, 'message': 'CORS preflight'}), 200

    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return jsonify({'token': None, 'message': '缺少 token', 'code': 'NO_TOKEN'}), 401

    try:
        token = auth_header.split(" ")[1]
        user_email = verify_token(token)
        if not user_email:
            return jsonify({'token': None, 'message': '無效 token', 'code': 'TOKEN_INVALID'}), 401

        candidates = _configured_question_sources()
        try:
            for collection_name in mongo.db.list_collection_names():
                if collection_name.startswith("system."):
                    continue
                try:
                    normalized = _normalize_question_source(collection_name)
                except ValueError:
                    continue
                if normalized not in candidates:
                    candidates.append(normalized)
        except Exception as e:
            print(f"⚠️ 自動列出 MongoDB collections 失敗: {e}")

        try:
            default_db_name = getattr(mongo.db, "name", "")
            for db_name in mongo.cx.list_database_names():
                if db_name in {"admin", "config", "local"}:
                    continue
                for collection_name in mongo.cx[db_name].list_collection_names():
                    if collection_name.startswith("system."):
                        continue
                    source_name = collection_name if db_name == default_db_name else f"{db_name}.{collection_name}"
                    try:
                        normalized = _normalize_question_source(source_name)
                    except ValueError:
                        continue
                    if normalized not in candidates:
                        candidates.append(normalized)
        except Exception as e:
            print(f"⚠️ 自動列出 MongoDB databases/collections 失敗: {e}")

        sources = []
        for source in candidates:
            try:
                collection, normalized_source = _get_question_collection(source)
                count = collection.estimated_document_count()
                sample = collection.find_one(
                    {},
                    {
                        'school': 1,
                        'department': 1,
                        'year': 1,
                        'question_text': 1,
                        'key-points': 1
                    }
                )
                sources.append({
                    'value': normalized_source,
                    'label': normalized_source,
                    'count': int(count or 0),
                    'has_sample': bool(sample)
                })
            except Exception as e:
                print(f"⚠️ 題庫來源 {source} 無法讀取: {e}")

        return jsonify({
            'token': refresh_token(token),
            'default_source': _normalize_question_source(DEFAULT_QUESTION_SOURCE),
            'sources': sources
        }), 200

    except Exception as e:
        logger.error(f"讀取題庫來源失敗: {str(e)}")
        return jsonify({'token': None, 'message': '讀取題庫來源失敗', 'error': str(e)}), 500


@quiz_bp.route('/get-exam', methods=['POST', 'OPTIONS'])
def get_exam():
    """獲取所有考題數據"""
    if request.method == 'OPTIONS':
        return '', 204
    auth_header = request.headers.get('Authorization')

    if not auth_header:
        return jsonify({'token': None, 'message': '未提供token', 'code': 'NO_TOKEN'}), 401

    try:
        token = auth_header.split(" ")[1]
        decoded_token = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])
        user_email = decoded_token.get('user')

        if not user_email:
            return jsonify({'token': None, 'message': '無效的token', 'code': 'TOKEN_INVALID'}), 401
    except jwt.ExpiredSignatureError:
        return jsonify({'token': None, 'message': 'Token已過期，請重新登錄', 'code': 'TOKEN_EXPIRED'}), 401
    except jwt.InvalidTokenError:
        return jsonify({'token': None, 'message': '無效的token', 'code': 'TOKEN_INVALID'}), 401
    except Exception as e:
        print(f"驗證token時發生錯誤: {str(e)}")
        return jsonify({'token': None, 'message': '認證失敗', 'code': 'AUTH_FAILED'}), 401

    data = request.get_json(silent=True) or {}
    question_collection, question_source = _get_question_collection(data)
    examdata = question_collection.find()
    exam_list = []
    for exam in examdata:
        exam_dict = {
             'type': exam.get('type'),
                    'school': exam.get('school'),
                    'department': exam.get('department'),
                    'year': exam.get('year'),
                    'question_number': exam.get('question_number'),
                    'question_text': build_full_question_text(exam),
                    'options': exam.get('options'),
                    'answer': exam.get('answer'),
                    'answer_type': exam.get('answer_type'),
                    'image_file': exam.get('image_file'),
                    'detail-answer': exam.get('detail-answer'),
                    'key_points': _normalize_key_points(exam.get('key-points', exam.get('key_points', ''))),
                    'difficulty level': exam.get('difficulty level'),
                    'latex_assets': _ensure_renderable_assets(exam),
                    'shared_asset_refs': _get_shared_asset_refs(exam),
                    'grouped_subquestions': exam.get('grouped_subquestions', []),
                    'continuation_note': exam.get('continuation_note', ''),
                    'layout_blocks': exam.get('layout_blocks', []),
                    'source_pages': exam.get('source_pages', []),
                    'question_source': question_source,
        }

        # 處理圖片檔案
        if exam_dict['image_file']:
            image_data_list = []
            for image_filename in exam_dict['image_file']:
                image_base64 = get_image_base64(image_filename)
                if image_base64:
                    image_data_list.append({
                        'filename': image_filename,
                        'data': image_base64
                    })
            exam_dict['images'] = image_data_list

        exam_list.append(exam_dict)

    return jsonify({'token': refresh_token(token), 'question_source': question_source, 'exams': exam_list}), 200


@quiz_bp.route('/get-exam-filters', methods=['POST', 'OPTIONS'])
def get_exam_filters():
    """獲取考題篩選選項（輕量級，不包含題目內容和圖片）"""
    if request.method == 'OPTIONS':
        return '', 204
    auth_header = request.headers.get('Authorization')

    if not auth_header:
        return jsonify({'token': None, 'message': '未提供token', 'code': 'NO_TOKEN'}), 401

    try:
        token = auth_header.split(" ")[1]
        decoded_token = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])
        user_email = decoded_token.get('user')

        if not user_email:
            return jsonify({'token': None, 'message': '無效的token', 'code': 'TOKEN_INVALID'}), 401
    except jwt.ExpiredSignatureError:
        return jsonify({'token': None, 'message': 'Token已過期，請重新登錄', 'code': 'TOKEN_EXPIRED'}), 401
    except jwt.InvalidTokenError:
        return jsonify({'token': None, 'message': '無效的token', 'code': 'TOKEN_INVALID'}), 401
    except Exception as e:
        print(f"驗證token時發生錯誤: {str(e)}")
        return jsonify({'token': None, 'message': '認證失敗', 'code': 'AUTH_FAILED'}), 401

    try:
        # 只查詢需要的欄位，不包含題目內容和圖片
        data = request.get_json(silent=True) or {}
        question_collection, question_source = _get_question_collection(data)
        examdata = question_collection.find(
            {},
            {
                'school': 1,
                'department': 1,
                'year': 1,
                'key-points': 1,
                '_id': 0
            }
        )

        # 使用集合來去重並收集資料
        schools = set()
        departments = set()
        years = set()
        subjects = set()
        subject_count_map = {}

        # 統計每個學校-年度-系所組合的題目數量
        school_year_dept_count = {}

        for exam in examdata:
            # 收集學校
            if exam.get('school'):
                schools.add(exam.get('school'))

            # 收集系所
            if exam.get('department'):
                departments.add(exam.get('department'))

            # 收集年度
            if exam.get('year'):
                years.add(str(exam.get('year')))

            # 收集知識點/科目
            key_points = exam.get('key-points', [])
            if isinstance(key_points, list):
                for subject in key_points:
                    if subject and subject != '其他':
                        subjects.add(subject)
                        subject_count_map[subject] = subject_count_map.get(subject, 0) + 1
            elif key_points and key_points != '其他':
                subjects.add(key_points)
                subject_count_map[key_points] = subject_count_map.get(key_points, 0) + 1

            # 統計學校-年度-系所組合的題目數量
            school = exam.get('school', '')
            year = str(exam.get('year', ''))
            dept = exam.get('department', '')
            if school and year and dept:
                key = f"{school}|{year}|{dept}"
                school_year_dept_count[key] = school_year_dept_count.get(key, 0) + 1

        # 轉換為排序後的列表
        result = {
            'token': refresh_token(token),
            'question_source': question_source,
            'filters': {
                'question_source': question_source,
                'schools': sorted(list(schools)),
                'departments': sorted(list(departments)),
                'years': sorted(list(years)),
                'subjects': sorted(list(subjects)),
                'subject_counts': subject_count_map,
                'school_year_dept_counts': school_year_dept_count
            }
        }

        return jsonify(result), 200

    except Exception as e:
        logger.error(f"獲取篩選選項時發生錯誤: {str(e)}")
        return jsonify({'token': refresh_token(token), 'message': '獲取篩選選項失敗', 'error': str(e)}), 500


@quiz_bp.route('/grading-progress/<template_id>', methods=['GET', 'OPTIONS'])
def get_grading_progress(template_id):
    """獲取測驗批改進度 API"""
    if request.method == 'OPTIONS':
        return jsonify({'token': None, 'message': 'CORS preflight'}), 200

    try:
        # 驗證用戶身份
        token = request.headers.get('Authorization').split(" ")[1]
        user_email = verify_token(token)
        if not user_email:
            return jsonify({'message': '無效的token'}), 401

        # 檢查測驗狀態
        with sqldb.engine.connect() as conn:
            template_id_int = int(template_id)

            # 檢查是否有完成的測驗記錄
            history_result = conn.execute(text("""
                SELECT id, status, correct_count, wrong_count, accuracy_rate, average_score, total_questions, answered_questions
                FROM quiz_history
                WHERE quiz_template_id = :template_id AND user_email = :user_email
                ORDER BY created_at DESC LIMIT 1
            """), {
                'template_id': template_id_int,
                'user_email': user_email
            }).fetchone()

            if history_result and history_result[1] == 'completed':
                # 測驗已完成，返回完整結果
                total_questions = history_result[6]
                answered_questions = history_result[7]
                unanswered_questions = total_questions - answered_questions

                return jsonify({
                    'token': refresh_token(token),
                    'success': True,
                        'status': 'completed',
                        'data': {
                            'quiz_history_id': history_result[0],
                            'correct_count': history_result[2],
                            'wrong_count': history_result[3],
                            'unanswered_count': unanswered_questions,
                            'accuracy_rate': float(history_result[4]) if history_result[4] else 0,
                            'average_score': float(history_result[5]) if history_result[5] else 0,
                            'grading_stages': [
                                {'stage': 1, 'name': '試卷批改', 'status': 'completed', 'description': '獲取題目數據完成'},
                                {'stage': 2, 'name': '計算分數', 'status': 'completed', 'description': '題目分類完成'},
                                {'stage': 3, 'name': '評判知識點', 'status': 'completed', 'description': 'AI評分完成'},
                                {'stage': 4, 'name': '生成學習計畫', 'status': 'completed', 'description': '統計完成'}
                            ]
                        }
                    })
            else:
                # 測驗進行中，返回進度狀態
                return jsonify({
                    'token': refresh_token(token),
                    'success': True,
                    'status': 'in_progress',
                    'data': {
                        'grading_stages': [
                            {'stage': 1, 'name': '試卷批改', 'status': 'in_progress', 'description': '正在獲取題目數據...'},
                            {'stage': 2, 'name': '計算分數', 'status': 'pending', 'description': '等待開始'},
                            {'stage': 3, 'name': '評判知識點', 'status': 'pending', 'description': '等待開始'},
                            {'stage': 4, 'name': '生成學習計畫', 'status': 'pending', 'description': '等待開始'}
                        ]
                    }
                })

    except Exception as e:
        print(f"❌ 獲取批改進度時發生錯誤: {str(e)}")
        return jsonify({'message': f'獲取批改進度失敗: {str(e)}'}), 500


@quiz_bp.route('/quiz-progress/<progress_id>', methods=['GET'])
def get_quiz_progress(progress_id):
    """獲取測驗進度 API - 用於前端實時查詢進度"""
    try:

        # 解析progress_id獲取用戶信息
        if not progress_id.startswith('progress_'):
            return jsonify({'error': '無效的進度ID'}), 400

        # 模擬進度狀態（實際應該從數據庫獲取）
        progress_data = {
            'progress_id': progress_id,
            'current_stage': 3,  # 當前階段：1=試卷批改, 2=計算分數, 3=評判知識點, 4=生成學習計畫
            'total_stages': 4,
            'stage_name': '評判知識點',
            'stage_description': 'AI正在進行智能評分...',
            'progress_percentage': 75,  # 75%完成
            'is_completed': False,
            'estimated_time_remaining': 30,  # 預計剩餘時間（秒）
            'last_updated': time.time()
        }

        return jsonify({
            'success': True,
            'data': progress_data
        })

    except Exception as e:
        print(f"❌ 獲取進度失敗: {e}")
        return jsonify({
            'success': False,
            'error': f'獲取進度失敗: {str(e)}'
        }), 500


@quiz_bp.route('/quiz-progress-sse/<progress_id>', methods=['GET'])
def quiz_progress_sse(progress_id):
    """測驗進度 Server-Sent Events API - 實時推送進度更新"""
    def generate_progress_events():
        # 設置SSE headers
        yield 'data: {"type": "connected", "message": "進度追蹤已連接"}\n\n'

        # 檢查進度追蹤狀態
        progress_status = get_progress_status(progress_id)

        if progress_status and progress_status.get('is_completed'):
            # 如果AI批改已經完成，直接發送完成消息
            completion_data = {
                'type': 'completion',
                'message': 'AI批改完成！',
                'progress_percentage': 100,
                'is_completed': True,
                'timestamp': time.time()
            }
            yield f'data: {json.dumps(completion_data, ensure_ascii=False)}\n\n'
            return

        # 如果還沒完成，發送當前進度
        current_stage = progress_status.get('current_stage', 1) if progress_status else 1
        stage_descriptions = {
            1: '正在獲取題目數據...',
            2: '正在分類題目...',
            3: 'AI正在進行智能評分...',
            4: '正在統計結果...'
        }

        progress_data = {
            'type': 'progress_update',
            'current_stage': current_stage,
            'stage_description': stage_descriptions.get(current_stage, '處理中...'),
            'progress_percentage': (current_stage / 4) * 100,
            'is_completed': False,
            'timestamp': time.time()
        }

        yield f'data: {json.dumps(progress_data, ensure_ascii=False)}\n\n'

        # 等待一下，然後檢查是否完成
        time.sleep(1)

        # 再次檢查完成狀態
        progress_status = get_progress_status(progress_id)
        if progress_status and progress_status.get('is_completed'):
            completion_data = {
                'type': 'completion',
                'message': 'AI批改完成！',
                'progress_percentage': 100,
                'is_completed': True,
                'timestamp': time.time()
            }
            yield f'data: {json.dumps(completion_data, ensure_ascii=False)}\n\n'
    # 設置SSE響應headers
    response = Response(
        generate_progress_events(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Cache-Control'
        }
    )

    return response

@quiz_bp.route('/get-long-answer/<answer_id>', methods=['GET'])
def get_long_answer(answer_id: str):

        # 驗證用戶身份
    token = request.headers.get('Authorization')
    if not token:
        return jsonify({'error': '缺少授權token'}), 401

    user_email = verify_token(token.split(" ")[1])
    if not user_email:
        return jsonify({'error': '無效的token'}), 401

    # 解析答案ID
    if not answer_id.startswith('LONG_ANSWER_'):
        return jsonify({'error': '無效的答案ID格式'}), 400

    long_answer_id = int(answer_id.replace('LONG_ANSWER_', ''))

    # 從數據庫獲取長答案
    with sqldb.engine.connect() as conn:
        result = conn.execute(text("""
            SELECT la.full_answer, la.question_type, la.created_at,
                    qh.template_id, qh.user_email
            FROM long_answers la
            JOIN quiz_history qh ON la.quiz_history_id = qh.id
            WHERE la.id = :long_answer_id
        """), {
            'long_answer_id': long_answer_id
        }).fetchone()

        if not result:
            return jsonify({'error': '答案不存在'}), 404

        # 驗證用戶權限（只能查看自己的答案）
        if result.user_email != user_email:
            return jsonify({'error': '無權限查看此答案'}), 403

        return jsonify({
            'token': refresh_token(token),
            'success': True,
            'data': {
                'full_answer': result.full_answer,
                'question_type': result.question_type,
                'created_at': result.created_at.isoformat() if result.created_at else None,
                'template_id': result.template_id
            }
        })

@quiz_bp.route('/get-quiz-from-database', methods=['POST', 'OPTIONS'])
def get_quiz_from_database_endpoint():

    if request.method == 'OPTIONS':
        return jsonify({'token': None, 'success': True}), 204

    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return jsonify({'token': None, 'message': '未提供token'}), 401

    token = auth_header.split(" ")[1]
    data = request.get_json()
    quiz_ids = data.get('quiz_ids', [])

    if not quiz_ids:
        return jsonify({
            'success': False,
            'message': '缺少考卷ID'
        }), 400

    # 調用獲取考卷數據函數
    result = get_quiz_from_database(quiz_ids)

    return jsonify({'token': refresh_token(token), 'data': result})

@quiz_bp.route('/get-quiz/<quiz_id>', methods=['GET', 'OPTIONS'])
def get_quiz(quiz_id):
    """獲取單個測驗數據"""
    if request.method == 'OPTIONS':
        return jsonify({'success': True}), 204

    try:
        logger.info(f"收到獲取測驗請求: {quiz_id}")

        # 直接調用get_quiz_from_database函數
        result = get_quiz_from_database([quiz_id])

        logger.info(f"get_quiz_from_database結果: {result.get('success', False)}")

        if result.get('success'):
            data = result.get('data', {})
            questions = data.get('questions', [])
            logger.info(f"找到測驗，題目數量: {len(questions)}")

            return jsonify({
                'success': True,
                'data': {
                    'quiz_id': quiz_id,
                    'template_id': quiz_id,
                    'title': data.get('quiz_info', {}).get('title', ''),
                    'questions': questions,
                    'time_limit': data.get('time_limit', 60),
                    'total_questions': len(questions),
                    'quiz_info': data.get('quiz_info', {})
                }
            })
        else:
            logger.warning(f"找不到測驗: {result.get('message', '未知錯誤')}")
            return jsonify({
                'success': False,
                'error': result.get('message', '找不到測驗')
            }), 404

    except Exception as e:
        logger.error(f"獲取測驗失敗: {e}")
        return jsonify({
            'success': False,
            'error': f'獲取測驗失敗: {str(e)}'
        }), 500
