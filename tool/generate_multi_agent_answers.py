#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""以主代理、覆核代理與仲裁代理批次建立結構化考題解答。

此工具用於 final12/new_exam_output.json 的輸出格式。它刻意把「可供
系統使用的最終答案」與「三代理決策軌跡」分開儲存：

* answers.json：保留原題欄位，加入 answer、detail-answer、key-points、
  difficulty level、error reason，可作為後續匯入資料。
* agent_traces/qNNN.json：每題主代理、覆核代理、仲裁代理的完整 JSON 回應。
* agent_decision_report.{json,md}：仲裁代理明示的採納來源與統計。

不會讀取既有 answer/detail-answer，避免舊答案影響本次答案生成。
"""

from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import os
import re
import sys
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


MAIN_PROMPT = """你是考題標準答案生成流程中的主代理人。
請依題幹、選項、子題與附圖（如有）提出一份可供覆核的初步答案。不得使用
外部未提供的參考答案，也不得修改題目原文。英文題幹、程式碼、選項可保留原文；
解析請使用繁體中文。

請只輸出 JSON 物件：
{{
  "proposed": {{
    "answer": "明確、可直接批改的答案",
    "detail-answer": "繁體中文詳細解析，包含必要推理、步驟或圖形/公式判定依據",
    "key-points": ["知識點1", "知識點2"],
    "difficulty level": "簡單|中等|困難",
    "error reason": "若題目資訊不足、選項矛盾或無法唯一作答，說明原因；否則空字串"
  }},
  "reasoning_summary": "主代理人採用此答案的簡短理由"
}}

題目資料：
{question}
"""

REVIEW_PROMPT = """你是考題標準答案生成流程中的覆核代理人。請根據題目資料，
嚴格檢查主代理人的答案、解析、知識點與難度；不要因主代理人的說法而放寬標準。
如發現答案、步驟、題型理解、圖片判讀或題意有問題，請提出可直接採用的修正。

請只輸出 JSON 物件：
{{
  "verdict": "main_supported|changes_required|insufficient_question_information",
  "answer_assessment": "正確|需修正|無法判定",
  "corrections": {{
    "answer": "若需修正，填修正後答案；否則空字串",
    "detail-answer": "若需修正，填修正後解析；否則空字串",
    "key-points": ["若需修正才填寫"],
    "difficulty level": "簡單|中等|困難|",
    "error reason": "題目不完整或主代理錯誤時的說明；否則空字串"
  }},
  "review_reason": "覆核理由"
}}

題目資料：
{question}

主代理結果：
{main_result}
"""

ARBITER_PROMPT = """你是考題標準答案生成流程中的仲裁代理人。請依題目、主代理
結果與覆核結果，輸出最後可供批改使用的答案。你必須自行核對題意，不可只是機械
複製其中一方。若題目資料本身不足以唯一作答，必須把限制寫入 error reason。

decision_source 必須且只能是下列之一：
- main_adopted：最終答案實質採用主代理的答案，覆核沒有造成實質更改。
- reviewer_correction：最終答案採用覆核代理提出的明確修正。
- arbiter_synthesis：仲裁代理整合雙方或自行校正，不能歸為前兩者。
- insufficient_question_information：題目或圖片資料不足，無法建立可靠標準答案。

field_sources 要對每個欄位標示來源，值只能是 main、reviewer、arbiter 或 unavailable。
key-points 請輸出陣列；最終工具會保存為相容的字串欄位。

請只輸出 JSON 物件：
{{
  "final": {{
    "answer": "明確、可直接批改的答案",
    "detail-answer": "繁體中文詳細解析",
    "key-points": ["知識點1", "知識點2"],
    "difficulty level": "簡單|中等|困難",
    "error reason": "無問題時空字串"
  }},
  "decision_source": "main_adopted|reviewer_correction|arbiter_synthesis|insufficient_question_information",
  "field_sources": {{
    "answer": "main|reviewer|arbiter|unavailable",
    "detail-answer": "main|reviewer|arbiter|unavailable",
    "key-points": "main|reviewer|arbiter|unavailable",
    "difficulty level": "main|reviewer|arbiter|unavailable",
    "error reason": "main|reviewer|arbiter|unavailable"
  }},
  "decision_reason": "說明為何採用或修正主代理結果"
}}

題目資料：
{question}

主代理結果：
{main_result}

覆核結果：
{review_result}
"""

FINAL_FIELDS = (
    "answer",
    "detail-answer",
    "key-points",
    "difficulty level",
    "error reason",
)
DECISION_SOURCES = {
    "main_adopted",
    "reviewer_correction",
    "arbiter_synthesis",
    "insufficient_question_information",
}
FIELD_SOURCES = {"main", "reviewer", "arbiter", "unavailable"}


def load_runtime_backend_config() -> None:
    """載入 CLI 所需的非機密 Vertex 設定。

    Flask 啟動時會載入 api.env；這支獨立批次工具不會經過 app.py，若未先
    載入設定，accessories.init_gemini() 會誤回退到 AI Studio API key。
    金鑰群組仍由既有 tool.api_keys 管理，此處不讀取或覆寫任何 API key。
    已在 shell 明確設定的環境變數優先。
    """
    config_path = PROJECT_ROOT / "api.env"
    allowed = {
        "AI_PROVIDER",
        "GEMINI_BACKEND",
        "VERTEX_PROJECT",
        "VERTEX_LOCATION",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
    }
    if not config_path.is_file():
        return
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in allowed and not os.environ.get(key):
            os.environ[key] = value.strip().strip('"').strip("'")


def json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_json(text: str) -> Dict[str, Any]:
    """容忍 Gemini 偶爾加入的 code fence，但拒絕非 JSON 內容。"""
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw).strip()
    if not raw.startswith("{"):
        first = raw.find("{")
        last = raw.rfind("}")
        if first >= 0 and last > first:
            raw = raw[first:last + 1]
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("agent response must be a JSON object")
    return result


def normalize_key_points(value: Any) -> str:
    if isinstance(value, list):
        values = [str(x).strip() for x in value if str(x).strip()]
        return "、".join(dict.fromkeys(values))
    return str(value or "").strip()


def normalized_final(arbiter: Dict[str, Any]) -> Dict[str, str]:
    raw_final = arbiter.get("final")
    if not isinstance(raw_final, dict):
        raise ValueError("arbiter response has no final object")
    final = {field: "" for field in FINAL_FIELDS}
    for field in FINAL_FIELDS:
        value = raw_final.get(field, "")
        final[field] = normalize_key_points(value) if field == "key-points" else str(value or "").strip()
    if not final["answer"]:
        raise ValueError("arbiter final.answer is empty")
    if final["difficulty level"] not in {"簡單", "中等", "困難"}:
        final["difficulty level"] = ""
    return final


def question_id(question: Dict[str, Any], index: int) -> str:
    explicit = str(question.get("question_id") or question.get("id") or "").strip()
    if explicit:
        return explicit
    document_id = str(question.get("document_id") or "question").strip()
    number = str(question.get("question_number") or index).strip()
    return f"{document_id}__Q{number}"


def question_text(question: Dict[str, Any]) -> str:
    text = str(question.get("question_text") or question.get("group_question_text") or "").strip()
    if not text:
        raise ValueError("question_text is empty")
    options = question.get("options")
    option_text = ""
    if isinstance(options, list) and options:
        option_text = "\n選項：\n" + "\n".join(str(x) for x in options)
    grouped = question.get("grouped_subquestions") or question.get("sub_questions")
    if isinstance(grouped, list) and grouped:
        items = []
        for pos, child in enumerate(grouped, 1):
            if isinstance(child, dict):
                items.append(f"子題 {pos}: {child.get('question_text') or child.get('text') or child}")
            else:
                items.append(f"子題 {pos}: {child}")
        option_text += "\n" + "\n".join(items)
    return text + option_text


def resolve_visual_assets(question: Dict[str, Any], input_path: Path, maximum: int) -> List[Dict[str, str]]:
    """從 final12 的 latex_assets / shared_asset_refs 找到可傳給視覺模型的檔案。"""
    source_assets: List[Any] = []
    for field in ("latex_assets", "assets", "shared_asset_refs"):
        value = question.get(field)
        if isinstance(value, list):
            source_assets.extend(value)

    seen: set[str] = set()
    resolved: List[Dict[str, str]] = []
    for asset in source_assets:
        if not isinstance(asset, dict):
            continue
        crop = str(asset.get("crop_path") or asset.get("image_path") or "").strip()
        if not crop:
            continue
        candidate = Path(crop)
        if not candidate.is_absolute():
            candidate = input_path.parent / crop.replace("/", "\\")
        candidate = candidate.resolve()
        key = str(candidate).lower()
        if key in seen or not candidate.is_file():
            continue
        seen.add(key)
        mime_type = mimetypes.guess_type(candidate.name)[0] or "image/png"
        resolved.append(
            {
                "path": str(candidate),
                "mime_type": mime_type,
                "description": str(asset.get("description") or "").strip(),
                "asset_id": str(asset.get("asset_id") or candidate.stem),
            }
        )
        if len(resolved) >= maximum:
            break
    return resolved


def build_question_payload(question: Dict[str, Any], image_assets: List[Dict[str, str]]) -> str:
    payload = {
        "question_id": question.get("question_id") or question.get("id"),
        "question_number": question.get("question_number"),
        "type": question.get("type"),
        "question_text_with_options": question_text(question),
        "source_pages": question.get("source_pages"),
        "visual_assets": [
            {"asset_id": a["asset_id"], "description": a["description"], "mime_type": a["mime_type"]}
            for a in image_assets
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def make_model(model_name: str):
    load_runtime_backend_config()
    from accessories import init_gemini

    model = init_gemini(model_name)
    if not model:
        raise RuntimeError("Gemini model initialization failed")
    return model


def call_agent(model: Any, prompt: str, image_assets: Iterable[Dict[str, str]], temperature: float, max_output_tokens: int) -> Dict[str, Any]:
    contents: List[Any] = [prompt]
    asset_list = list(image_assets)
    if asset_list:
        try:
            from google.genai import types
        except ImportError:
            from google import genai as _genai
            types = _genai.types
        for asset in asset_list:
            contents.append(
                types.Part.from_bytes(
                    data=Path(asset["path"]).read_bytes(),
                    mime_type=asset["mime_type"],
                )
            )
    response = model.generate_content(
        contents if len(contents) > 1 else prompt,
        generation_config={
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
            "response_mime_type": "application/json",
        },
    )
    raw = str(getattr(response, "text", "") or "").strip()
    if not raw:
        raise RuntimeError("empty model response")
    return {"raw": raw, "json": parse_json(raw)}


def process_one(
    question: Dict[str, Any],
    *,
    index: int,
    input_path: Path,
    model: Any,
    temperature: float,
    max_output_tokens: int,
    max_images: int,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    identifier = question_id(question, index)
    images = resolve_visual_assets(question, input_path, max_images)
    payload = build_question_payload(question, images)
    main = call_agent(model, MAIN_PROMPT.format(question=payload), images, temperature, max_output_tokens)
    review = call_agent(
        model,
        REVIEW_PROMPT.format(question=payload, main_result=json.dumps(main["json"], ensure_ascii=False)),
        images,
        temperature,
        max_output_tokens,
    )
    arbiter = call_agent(
        model,
        ARBITER_PROMPT.format(
            question=payload,
            main_result=json.dumps(main["json"], ensure_ascii=False),
            review_result=json.dumps(review["json"], ensure_ascii=False),
        ),
        images,
        temperature,
        max_output_tokens,
    )
    final = normalized_final(arbiter["json"])
    decision_source = str(arbiter["json"].get("decision_source") or "arbiter_synthesis").strip()
    if decision_source not in DECISION_SOURCES:
        decision_source = "arbiter_synthesis"
    field_sources_raw = arbiter["json"].get("field_sources")
    field_sources = {
        field: str(field_sources_raw.get(field) if isinstance(field_sources_raw, dict) else "arbiter").strip()
        for field in FINAL_FIELDS
    }
    for field, source in field_sources.items():
        if source not in FIELD_SOURCES:
            field_sources[field] = "arbiter"

    answer_record = copy.deepcopy(question)
    answer_record.update(final)
    answer_record["answer_generation"] = {
        "process": "primary_review_arbiter_v1",
        "decision_source": decision_source,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    trace = {
        "question_id": identifier,
        "question_index": index,
        "status": "success",
        "source_question_number": question.get("question_number"),
        "image_assets_used": images,
        "primary_agent": main["json"],
        "primary_agent_raw": main["raw"],
        "review_agent": review["json"],
        "review_agent_raw": review["raw"],
        "arbiter_agent": arbiter["json"],
        "arbiter_agent_raw": arbiter["raw"],
        "decision_source": decision_source,
        "field_sources": field_sources,
        "decision_reason": str(arbiter["json"].get("decision_reason") or "").strip(),
        "final": final,
    }
    return answer_record, trace


def selected_questions(data: Any, indices: str, limit: int) -> List[Tuple[int, Dict[str, Any]]]:
    if isinstance(data, dict):
        questions = data.get("questions") or data.get("data") or []
    else:
        questions = data
    if not isinstance(questions, list):
        raise ValueError("input JSON must be a question list or an object containing questions")
    parsed_indices: Optional[set[int]] = None
    if indices:
        parsed_indices = set()
        for part in indices.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start, end = (int(x.strip()) for x in part.split("-", 1))
                parsed_indices.update(range(start, end + 1))
            else:
                parsed_indices.add(int(part))
    chosen = [(i, q) for i, q in enumerate(questions, 1) if isinstance(q, dict) and (parsed_indices is None or i in parsed_indices)]
    if limit > 0:
        chosen = chosen[:limit]
    if not chosen:
        raise ValueError("no questions selected")
    return chosen


def report_rows(traces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for trace in traces:
        review = trace.get("review_agent") if isinstance(trace.get("review_agent"), dict) else {}
        rows.append(
            {
                "index": trace.get("question_index"),
                "question_id": trace.get("question_id"),
                "question_number": trace.get("source_question_number"),
                "status": trace.get("status"),
                "review_verdict": review.get("verdict", "") if trace.get("status") == "success" else "",
                "decision_source": trace.get("decision_source", "") if trace.get("status") == "success" else "",
                "field_sources": trace.get("field_sources", {}) if trace.get("status") == "success" else {},
                "images_used": len(trace.get("image_assets_used") or []),
                "error": trace.get("error", ""),
            }
        )
    return rows


def write_report(output_dir: Path, *, input_path: Path, args: argparse.Namespace, traces: List[Dict[str, Any]]) -> None:
    rows = report_rows(traces)
    successful = [row for row in rows if row["status"] == "success"]
    decision_counts = Counter(row["decision_source"] for row in successful)
    review_counts = Counter(row["review_verdict"] for row in successful)
    report = {
        "schema_version": "multi_agent_answer_decision_report_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_json": str(input_path),
        "model": args.model,
        "temperature": args.temperature,
        "selected_questions": len(rows),
        "successful_questions": len(successful),
        "failed_questions": len(rows) - len(successful),
        "decision_source_counts": dict(sorted(decision_counts.items())),
        "review_verdict_counts": dict(sorted(review_counts.items())),
        "questions": rows,
        "interpretation": (
            "decision_source 由仲裁代理明示最終答案的主要採納來源；它記錄模型決策軌跡，"
            "不是人工正確性標記。"
        ),
    }
    json_dump(output_dir / "agent_decision_report.json", report)
    lines = [
        "# 多代理標準答案決策報告",
        "",
        f"- 輸入題庫：`{input_path}`",
        f"- 模型：`{args.model}`；temperature：`{args.temperature}`",
        f"- 題目：{len(rows)}；成功：{len(successful)}；失敗：{len(rows) - len(successful)}",
        "",
        "## 仲裁採納來源",
        "",
    ]
    for source, count in sorted(decision_counts.items()):
        lines.append(f"- `{source}`：{count} 題")
    lines += ["", "## 逐題紀錄", "", "| 題次 | 題號 | 覆核結論 | 仲裁採納來源 | 圖片數 | 狀態 |", "|---:|---|---|---|---:|---|"]
    for row in rows:
        lines.append(
            f"| {row['index']} | {row['question_number'] or row['question_id']} | "
            f"{row['review_verdict'] or '-'} | {row['decision_source'] or '-'} | "
            f"{row['images_used']} | {row['status']} |"
        )
    if any(row["error"] for row in rows):
        lines += ["", "## 失敗原因", ""]
        lines += [f"- 題次 {row['index']}：{row['error']}" for row in rows if row["error"]]
    (output_dir / "agent_decision_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="以主代理、覆核代理、仲裁代理產生考題解答與決策報告")
    parser.add_argument("--input", required=True, help="new_exam_output.json 的絕對或相對路徑")
    parser.add_argument("--output-dir", default="", help="輸出資料夾；省略時寫入 logs/multi_agent_answers/<timestamp>")
    parser.add_argument("--model", default="gemini-2.5-flash", help="三個代理共用的 Gemini 模型")
    parser.add_argument("--temperature", type=float, default=0.0, help="生成溫度；正式批次建議 0")
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--max-images", type=int, default=4, help="每題最多傳入幾張圖片")
    parser.add_argument("--indices", default="", help="只處理指定題次，例如 1,3-5")
    parser.add_argument("--limit", type=int, default=0, help="最多處理幾題，0 代表全部")
    parser.add_argument("--dry-run", action="store_true", help="只驗證題目與圖片路徑，不呼叫付費模型")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"input not found: {input_path}")
    data = json.loads(input_path.read_text(encoding="utf-8"))
    selected = selected_questions(data, args.indices, args.limit)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else PROJECT_ROOT / "logs" / "multi_agent_answers" / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "agent_traces").mkdir(exist_ok=True)

    manifest = {
        "schema_version": "multi_agent_answer_generation_v1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "input_json": str(input_path),
        "model": args.model,
        "temperature": args.temperature,
        "selected_indices": [index for index, _ in selected],
        "dry_run": args.dry_run,
    }
    json_dump(output_dir / "manifest.json", manifest)
    print(f"[input] {input_path}")
    print(f"[output] {output_dir}")
    print(f"[questions] {len(selected)}")

    if args.dry_run:
        preflight = []
        for index, question in selected:
            images = resolve_visual_assets(question, input_path, args.max_images)
            preflight.append(
                {
                    "index": index,
                    "question_id": question_id(question, index),
                    "question_number": question.get("question_number"),
                    "image_assets_found": images,
                    "question_text_present": bool(question_text(question)),
                }
            )
        json_dump(output_dir / "preflight.json", preflight)
        print(f"[dry-run] OK. {output_dir / 'preflight.json'}")
        return 0

    model = make_model(args.model)
    answers: List[Dict[str, Any]] = []
    traces: List[Dict[str, Any]] = []
    for ordinal, (index, question) in enumerate(selected, 1):
        identifier = question_id(question, index)
        print(f"[{ordinal}/{len(selected)}] {identifier}")
        try:
            answer, trace = process_one(
                question,
                index=index,
                input_path=input_path,
                model=model,
                temperature=args.temperature,
                max_output_tokens=args.max_output_tokens,
                max_images=args.max_images,
            )
            answers.append(answer)
            print(f"    decision={trace['decision_source']}")
        except Exception as exc:
            trace = {
                "question_id": identifier,
                "question_index": index,
                "source_question_number": question.get("question_number"),
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
                "image_assets_used": resolve_visual_assets(question, input_path, args.max_images),
            }
            print(f"    FAILED: {trace['error']}")
        traces.append(trace)
        json_dump(output_dir / "agent_traces" / f"q{index:03d}.json", trace)
        json_dump(output_dir / "answers.partial.json", answers)
        write_report(output_dir, input_path=input_path, args=args, traces=traces)

    json_dump(output_dir / "answers.json", answers)
    write_report(output_dir, input_path=input_path, args=args, traces=traces)
    print("\n[done]")
    print(f"  answers: {output_dir / 'answers.json'}")
    print(f"  traces:  {output_dir / 'agent_traces'}")
    print(f"  report:  {output_dir / 'agent_decision_report.md'}")
    return 0 if all(trace.get("status") == "success" for trace in traces) else 2


if __name__ == "__main__":
    raise SystemExit(main())
