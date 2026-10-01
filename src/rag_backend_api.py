"""HTTP API for selecting, comparing, and tracing the available RAG backends."""
from __future__ import annotations

import json
import hashlib
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from flask import Blueprint, jsonify, request

from src import rag_backend


rag_backend_bp = Blueprint("rag_backend", __name__, url_prefix="/api/rag")

BACKEND_ROOT = Path(__file__).resolve().parents[1]
COMPARISON_DIR = Path(
    os.getenv(
        "RAG_COMPARISON_DIR",
        str(BACKEND_ROOT / "logs" / "rag_comparisons"),
    )
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _new_comparison_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{uuid.uuid4().hex[:8]}"


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _expand_chromadb_hits_for_display(data: Dict[str, Any]) -> Dict[str, Any]:
    """Make ChromaDB trace/comparison payloads show full chunks, not previews.

    Older trace files may contain both:
      - content_preview: clipped text ending with "...[truncated 800 chars]"
      - content_full: the real full chunk

    Several UI panels historically read content_preview only.  For thesis
    inspection we need the displayed/reported text to be the actual chunk, so
    this normalizes payloads at API boundaries while keeping a short excerpt.
    """
    if not isinstance(data, dict):
        return data

    def normalize_hit(hit: Dict[str, Any]) -> None:
        if not isinstance(hit, dict):
            return
        full = hit.get("content_full")
        preview = hit.get("content_preview")
        if full:
            if preview and "content_excerpt" not in hit and preview != full:
                hit["content_excerpt"] = preview
            hit["content_preview"] = full
            hit["content_full"] = full
            hit["content_chars"] = hit.get("content_chars") or len(str(full))
            hit["content_truncated_in_trace"] = False

    chroma = data.get("chromadb")
    if isinstance(chroma, dict):
        for hit in chroma.get("hits") or []:
            normalize_hit(hit)

    # 三方比較檔有 chromadb 與 sides.chromadb 兩份摘要，兩邊都正規化。
    for container in (data.get("chromadb"), (data.get("sides") or {}).get("chromadb")):
        if isinstance(container, dict):
            for hit in container.get("hits") or []:
                normalize_hit(hit)

    return data


def _backfill_chromadb_comparison_hits(data: Dict[str, Any]) -> Dict[str, Any]:
    """Backfill old comparison records from their ChromaDB trace file when possible."""
    data = _expand_chromadb_hits_for_display(data)
    chroma_side = (data.get("sides") or {}).get("chromadb")
    if not isinstance(chroma_side, dict):
        chroma_side = data.get("chromadb")
    if not isinstance(chroma_side, dict):
        return data

    # If the comparison already has full chunks, no need to touch it.
    existing_hits = chroma_side.get("hits") or []
    if existing_hits and any((h or {}).get("content_full") for h in existing_hits if isinstance(h, dict)):
        return data

    trace_id = chroma_side.get("trace_id") or (data.get("chromadb") or {}).get("trace_id")
    if not trace_id:
        return data
    try:
        from src.chromadb_trace import read_trace
        trace = read_trace(str(trace_id))
    except Exception:
        trace = None
    if not trace:
        return data

    trace_hits = ((trace.get("chromadb") or {}).get("hits") or [])
    if not trace_hits:
        return data
    normalized_trace = _expand_chromadb_hits_for_display({"chromadb": {"hits": trace_hits}})
    full_hits = (normalized_trace.get("chromadb") or {}).get("hits") or []

    # Copy full text into both top-level chromadb and sides.chromadb if present.
    for container in (data.get("chromadb"), (data.get("sides") or {}).get("chromadb")):
        if isinstance(container, dict):
            container["hits"] = full_hits[: len(container.get("hits") or full_hits)]
            container["hit_count"] = len(container["hits"])
    return data


def _comparison_side_summary(side: Dict[str, Any]) -> Dict[str, Any]:
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
        "selected_chunk_count": chunks.get("selected", chunks.get("retrieved", 0)),
        "rejected_chunk_count": chunks.get("rejected", 0),
        "duplicate_chunk_count": chunks.get("duplicates", 0),
        "noise_filtered_chunk_count": chunks.get("noise_filtered", 0),
        "injected_chunk_count": chunks.get("injected", 0),
        "complete_injected_chunk_count": chunks.get("complete_injected", 0),
        "shortened_chunk_count": chunks.get("shortened", 0),
        "omitted_chunk_count": chunks.get("omitted", 0),
        "budget_omitted_chunk_count": chunks.get("budget_omitted", 0),
        "valid_for_complete_context_comparison": chunks.get(
            "valid_for_complete_context_comparison", False
        ),
        "answer_chars": len(str(side.get("answer") or "")),
        "error": side.get("error"),
    }


def _save_comparison_record(record: Dict[str, Any]) -> Dict[str, str]:
    COMPARISON_DIR.mkdir(parents=True, exist_ok=True)
    path = COMPARISON_DIR / f"{record['comparison_id']}.json"
    record["comparison_json_path"] = str(path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(_json_safe(record), f, ensure_ascii=False, indent=2)
    return {"comparison_json_path": str(path)}


def _comparison_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    sides = record.get("sides") or {}
    return {
        "comparison_id": record.get("comparison_id"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "status": record.get("status"),
        "question_preview": str(record.get("question") or "")[:220],
        "comparison_mode": record.get("comparison_mode"),
        "top_k": record.get("top_k"),
        "latency_ms": record.get("latency_ms"),
        "graphrag": _comparison_side_summary(sides.get("graphrag") or {}),
        "chromadb": _comparison_side_summary(sides.get("chromadb") or {}),
        "llm_only": _comparison_side_summary(sides.get("llm_only") or {}),
        "comparison_json_path": record.get("comparison_json_path"),
    }


def _list_comparison_records(limit: int = 50) -> list[Dict[str, Any]]:
    if not COMPARISON_DIR.exists():
        return []
    records = []
    for path in sorted(COMPARISON_DIR.glob("*.json"), reverse=True)[:limit]:
        try:
            with path.open(encoding="utf-8") as f:
                record = json.load(f)
            records.append(_comparison_summary(record))
        except Exception:
            continue
    return records


def _read_comparison_record(comparison_id: str) -> Dict[str, Any] | None:
    if not COMPARISON_DIR.exists():
        return None
    exact = COMPARISON_DIR / f"{comparison_id}.json"
    if exact.exists():
        with exact.open(encoding="utf-8") as f:
            return json.load(f)
    for path in sorted(COMPARISON_DIR.glob("*.json"), reverse=True):
        if path.stem.startswith(comparison_id):
            with path.open(encoding="utf-8") as f:
                return json.load(f)
    return None


@rag_backend_bp.route("/backend", methods=["GET"])
def get_backend():
    return jsonify(rag_backend.status())


@rag_backend_bp.route("/backend", methods=["POST"])
def set_backend():
    data = request.get_json(silent=True) or {}
    name = str(data.get("backend") or request.args.get("backend") or "").strip().lower()
    if not name:
        return jsonify({
            "error": "missing 'backend' in body/query",
            "valid": rag_backend.status()["valid"],
        }), 400
    try:
        active = rag_backend.set_backend(name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"message": f"RAG backend switched to {active}", **rag_backend.status()})


@rag_backend_bp.route("/backend/reset", methods=["POST"])
def reset_backend():
    rag_backend.reset_backend()
    return jsonify({"message": "runtime override cleared", **rag_backend.status()})


@rag_backend_bp.route("/traces", methods=["GET"])
def list_traces_route():
    backend = str(request.args.get("backend") or "all").strip().lower()
    try:
        limit = max(1, min(200, int(request.args.get("limit", 30))))
    except (ValueError, TypeError):
        limit = 30

    output = []
    if backend in ("all", "graphrag"):
        try:
            from src.graphrag_trace import list_traces
            for item in list_traces(limit=limit):
                item["backend"] = "graphrag"
                output.append(item)
        except Exception:
            pass
    if backend in ("all", "chromadb"):
        try:
            from src.chromadb_trace import list_traces
            for item in list_traces(limit=limit):
                item["backend"] = "chromadb"
                output.append(item)
        except Exception:
            pass
    output.sort(key=lambda item: item.get("started_at") or "", reverse=True)
    return jsonify({"traces": output[:limit]})


@rag_backend_bp.route("/traces/<backend>/<trace_id>", methods=["GET"])
def get_trace_route(backend: str, trace_id: str):
    backend = backend.lower()
    data = None
    try:
        if backend == "graphrag":
            from src.graphrag_trace import read_trace
            data = read_trace(trace_id)
        elif backend == "chromadb":
            from src.chromadb_trace import read_trace
            data = read_trace(trace_id)
            data = _expand_chromadb_hits_for_display(data or {})
        else:
            return jsonify({"error": f"unknown backend: {backend}"}), 400
    except Exception:
        data = None
    if not data:
        return jsonify({"error": "trace not found"}), 404
    return jsonify(data)


@rag_backend_bp.route("/comparisons", methods=["GET"])
def list_comparisons_route():
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except (ValueError, TypeError):
        limit = 50
    return jsonify({"comparisons": _list_comparison_records(limit)})


@rag_backend_bp.route("/comparisons/<comparison_id>", methods=["GET"])
def get_comparison_route(comparison_id: str):
    data = _read_comparison_record(comparison_id)
    if not data:
        return jsonify({"error": "comparison record not found"}), 404
    data = _backfill_chromadb_comparison_hits(data)
    return jsonify(data)


# ============================================================
# 🆕 檔案瀏覽 & 下載：logs/graphrag_traces/ + logs/chromadb_traces/
# ============================================================
_TRACE_DIRS = {
    "graphrag": BACKEND_ROOT / "logs" / "graphrag_traces",
    "chromadb": BACKEND_ROOT / "logs" / "chromadb_traces",
}


def _safe_trace_file(backend: str, filename: str) -> Path:
    """驗證檔名不含 path traversal，回實際 Path 物件。找不到丟 ValueError。"""
    if backend not in _TRACE_DIRS:
        raise ValueError(f"unknown backend: {backend}")
    # 只允許 [a-zA-Z0-9_-.] 檔名，防 path traversal
    import re as _re
    if not _re.match(r"^[A-Za-z0-9_.\-]+$", filename):
        raise ValueError("invalid filename")
    base_dir = _TRACE_DIRS[backend]
    p = (base_dir / filename).resolve()
    # 確認 resolved 後仍在 base_dir 底下
    if not str(p).startswith(str(base_dir.resolve())):
        raise ValueError("path escape")
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(filename)
    return p


@rag_backend_bp.route("/trace-files/<backend>", methods=["GET"])
def list_trace_files_route(backend: str):
    """列出 logs/<backend>_traces/ 底下所有檔案（含 .json + .md）。

    query: ?limit=100
    回傳:
      {"backend": "graphrag", "dir": ".../logs/graphrag_traces",
       "files": [{"name": "20260706_120000_xxx.json", "size": 12345, "mtime": "..."}, ...]}
    """
    if backend not in _TRACE_DIRS:
        return jsonify({"error": f"unknown backend: {backend}"}), 400
    try:
        limit = max(1, min(500, int(request.args.get("limit", 200))))
    except (ValueError, TypeError):
        limit = 200

    base_dir = _TRACE_DIRS[backend]
    if not base_dir.exists():
        return jsonify({"backend": backend, "dir": str(base_dir), "files": []})

    files = []
    for p in sorted(base_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file():
            continue
        try:
            stat = p.stat()
            files.append({
                "name": p.name,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "ext": p.suffix.lower(),
            })
        except OSError:
            continue
        if len(files) >= limit:
            break

    return jsonify({
        "backend": backend,
        "dir": str(base_dir),
        "total": len(files),
        "files": files,
    })


@rag_backend_bp.route("/trace-files/<backend>/<path:filename>", methods=["GET"])
def download_trace_file_route(backend: str, filename: str):
    """下載/開啟單一 trace 檔案（.json 直接顯示，.md 下載）"""
    from flask import send_file, Response
    try:
        p = _safe_trace_file(backend, filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except FileNotFoundError:
        return jsonify({"error": "file not found"}), 404

    # JSON 直接以 application/json 回，方便瀏覽器直接看
    if p.suffix.lower() == ".json":
        try:
            text = p.read_text(encoding="utf-8")
            if backend == "chromadb":
                try:
                    data = json.loads(text)
                    data = _expand_chromadb_hits_for_display(data)
                    text = json.dumps(_json_safe(data), ensure_ascii=False, indent=2)
                except Exception:
                    pass
            return Response(text, mimetype="application/json; charset=utf-8")
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # .md 或其他：以 text/markdown 回（也能下載）
    if p.suffix.lower() == ".md":
        try:
            return Response(p.read_text(encoding="utf-8"),
                            mimetype="text/markdown; charset=utf-8")
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # 其他附件下載
    return send_file(str(p), as_attachment=True, download_name=filename)


def _comparison_prompt(data: Dict[str, Any]) -> tuple[str, str, str]:
    """Build one identical grading prompt for every comparison mode."""
    question = str(data.get("question") or "").strip()
    requested_mode = str(data.get("comparison_mode") or "").strip().lower()
    knowledge_policy = str(
        data.get("knowledge_policy") or "strict"
    ).strip().lower()
    if knowledge_policy not in {"strict", "hybrid"}:
        knowledge_policy = "strict"
    # 論文盲測：作答模型完全看不到標準答案、學生答案或批改回饋。
    # 三方拿到相同的基礎指令；strict 限制 RAG 端只能依檢索內容作答，
    # hybrid 則允許三方皆使用模型既有知識，檢索內容作為額外證據。
    if requested_mode == "blind_qa":
        if knowledge_policy == "hybrid":
            base_prompt = f"""你正在參加一項資訊科學問答盲測。請直接、完整且聚焦地回答下列題目。

題目：{question}

作答知識規則（混合知識模式）：
1. 不論本 Prompt 後方是否附有檢索內容，均可使用你自身既有的資訊科學知識回答。
2. 若附有「檢索內容」或「教材 Chunk」，請把它們當作額外證據與補充資訊；優先採用其中正確且與題目相關的內容，但不得因教材未涵蓋全部答案就拒絕作答。
3. 若檢索內容與你的既有知識衝突，請指出衝突並採用可合理驗證的答案，不可盲目服從錯誤或離題 Chunk。
4. 回答應涵蓋必要定義、推理步驟與有助理解的概念關係；不要用無關旁支或重複文字刻意增加篇幅。
5. 不要評論實驗、資料來源或評分規則；只輸出答案與必要說明。"""
        else:
            base_prompt = f"""你正在參加一項資訊科學問答盲測。請直接回答下列題目。

題目：{question}

作答資料規則：
1. 如果本 Prompt 後方附有「檢索內容」或「教材 Chunk」，只能依據其中與題目相關的資訊和必要的邏輯推理作答，不可用未出現在檢索內容中的外部事實補洞。
2. 如果本 Prompt 後方完全沒有檢索內容，請使用你自身既有的理解能力作答。
3. 如果現有資料不足以回答，請明確寫出「提供的資訊不足」，不要猜測。
4. 不要評論資料來源或評分規則，只輸出答案與必要推理。"""
        return base_prompt, question[:3000], "blind_qa"

    student_answer = str(data.get("student_answer") or data.get("user_answer") or "").strip()
    correct_answer = str(data.get("correct_answer") or "").strip()
    feedback = data.get("grading_feedback") or data.get("feedback") or ""
    if isinstance(feedback, (dict, list)):
        feedback_text = json.dumps(feedback, ensure_ascii=False, indent=2)
    else:
        feedback_text = str(feedback).strip()

    has_grading_context = bool(student_answer or correct_answer or feedback_text)
    if has_grading_context:
        base_prompt = f"""你是一位資訊科學教師。請針對學生的錯題提供清楚、可驗證的補救教學。

題目：{question}
學生答案：{student_answer or '（未提供）'}
參考答案：{correct_answer or '（未提供）'}
原批改回饋：{feedback_text or '（未提供）'}

請依序說明：
1. 學生答案錯誤或不足之處。
2. 正確觀念與推理。
3. 學生可能混淆的概念。
4. 一個簡短的複習建議。

只能把後續注入的檢索內容當作參考；若參考內容不足，請明確說明，不要捏造教材內容。"""
        mode = "wrong_answer_feedback"
    else:
        base_prompt = f"""你是一位資訊科學教師。請回答下列問題，並以後續注入的檢索內容作為參考；若參考內容不足，請明確說明。

問題：{question}"""
        mode = "question_answer"

    # Retrieval quality must be measured from the original question only.
    # Student/correct answers still belong in the grading prompt, but using
    # them for retrieval leaks the expected answer into both RAG backends and
    # makes chunk-relevance comparisons invalid (image answers can also inject
    # thousands of Base64 characters into the query).
    return base_prompt, question[:3000], mode


def _context_payload(
    base_prompt: str,
    enhanced_prompt: str,
    usage: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    text = (
        enhanced_prompt[len(base_prompt):]
        if enhanced_prompt.startswith(base_prompt)
        else enhanced_prompt
    )
    chars = len(text)
    return {
        "text": text,
        "chars": chars,
        "utf8_bytes": len(text.encode("utf-8")),
        # Approximation only: exact tokens depend on the generation tokenizer.
        "estimated_tokens": max(0, round(chars / 4)),
        "prompt_before_chars": len(base_prompt),
        "prompt_after_chars": len(enhanced_prompt),
        "truncated": bool((usage or {}).get("context_truncated", False)),
        "retrieved_chunk_count": int((usage or {}).get("retrieved_chunk_count", 0) or 0),
        "selected_chunk_count": int((usage or {}).get("selected_chunk_count", 0) or 0),
        "rejected_chunk_count": int((usage or {}).get("rejected_chunk_count", 0) or 0),
        "injected_chunk_count": int((usage or {}).get("injected_chunk_count", 0) or 0),
        "complete_injected_chunk_count": int(
            (usage or {}).get("complete_injected_chunk_count", 0) or 0
        ),
        "shortened_chunk_count": int((usage or {}).get("shortened_chunk_count", 0) or 0),
        "omitted_chunk_count": int((usage or {}).get("omitted_chunk_count", 0) or 0),
        "budget_omitted_chunk_count": int(
            (usage or {}).get("budget_omitted_chunk_count", 0) or 0
        ),
    }


def _run_comparison_side(
    backend: str,
    base_prompt: str,
    search_query: str,
    top_k: int,
    generation_config: Dict[str, Any] | None = None,
    graph_context_mode: str = "seed_only",
    max_prereq_concepts: int = 5,
) -> Dict[str, Any]:
    """Retrieve context and generate one answer inside thread-local state."""
    from src.rag_sys.rag_ai_role import (
        call_gemini_api,
        enhance_prompt_with_knowledge,
        get_last_graphrag_usage,
        reset_graphrag_usage,
    )

    started = time.perf_counter()
    trace_module = None
    try:
        if backend == "graphrag":
            from src import graphrag_trace as trace_module
            trace_module.start_trace(
                question=search_query,
                platform="grading_comparison",
                route="rag/compare",
            )
        elif backend == "chromadb":
            from src import chromadb_trace as trace_module
            trace_module.start_trace(
                question=search_query,
                platform="grading_comparison",
                route="rag/compare",
                top_k=top_k,
            )
        elif backend == "llm_only":
            # Baseline: do not create a retrieval trace because no RAG context is injected.
            trace_module = None
        else:
            raise ValueError(f"unknown comparison backend: {backend}")

        reset_graphrag_usage()
        with rag_backend.with_backend(backend):
            retrieval_prompt_started = time.perf_counter()
            enhanced_prompt = enhance_prompt_with_knowledge(
                base_prompt,
                search_query,
                top_k=top_k,
                graph_context_mode=graph_context_mode,
                max_prereq_concepts=max_prereq_concepts,
            )
            retrieval_prompt_ms = round(
                (time.perf_counter() - retrieval_prompt_started) * 1000, 2
            )
            usage = get_last_graphrag_usage()
            generation_started = time.perf_counter()
            answer = call_gemini_api(
                enhanced_prompt,
                generation_config_override=generation_config,
            )
            generation_ms = round(
                (time.perf_counter() - generation_started) * 1000, 2
            )

        current_trace = trace_module.get_current_trace() if trace_module is not None else {}
        current_trace = current_trace or {}
        chunk_records = []
        chunk_summary = {
            "retrieved": 0,
            "unique_retrieved": 0,
            "selected": 0,
            "rejected": 0,
            "duplicates": 0,
            "noise_filtered": 0,
            "injected": 0,
            "complete_injected": 0,
            "shortened": 0,
            "omitted": 0,
            "budget_omitted": 0,
            "valid_for_complete_context_comparison": backend == "llm_only",
        }
        if backend == "graphrag":
            graph_result = (current_trace.get("graphrag") or {}).get("result") or {}
            graph_chunks = current_trace.get("chunks") or {}
            chunk_records = graph_chunks.get("records") or []
            chunk_summary.update(graph_chunks.get("summary") or {})
            selected_count = int(
                chunk_summary.get("selected", chunk_summary.get("retrieved", 0)) or 0
            )
            chunk_summary["valid_for_complete_context_comparison"] = bool(
                usage.get("used")
                and selected_count > 0
                and selected_count == chunk_summary.get("injected")
                and chunk_summary.get("injected") == chunk_summary.get("complete_injected")
                and not chunk_summary.get("shortened")
                and not chunk_summary.get("budget_omitted")
                and not usage.get("context_truncated", False)
            )
            hits = [
                {
                    "content_preview": str(item.get("definition") or "")[:400],
                    "metadata": {
                        "concept": item.get("name"),
                        "category": "graphrag",
                        "prerequisites": item.get("prerequisites") or [],
                        "leads_to": item.get("leads_to") or [],
                    },
                    "distance": item.get("score"),
                }
                for item in (graph_result.get("expanded") or [])[:top_k]
            ]
            finalized = trace_module.finalize_trace(answer, usage)
            trace_id = finalized.get("trace_id")
        elif backend == "chromadb":
            chroma = current_trace.get("chromadb") or {}
            injected_context = str((current_trace.get("prompt") or {}).get("injected_context") or "")
            for rank, hit in enumerate(chroma.get("hits") or [], start=1):
                raw_content = str(hit.get("content_full") or hit.get("content_preview") or "")
                found_verbatim = bool(raw_content) and raw_content in injected_context
                digest = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
                source = hit.get("source") or ""
                chunk_index = hit.get("chunk_idx")
                chunk_records.append({
                    "section": "vector",
                    "retrieval_stage": "vector",
                    "concept": "",
                    "concepts": [],
                    "ordinal": rank,
                    "chunk_id": f"{source}:{chunk_index}" if source or chunk_index is not None else digest,
                    "content_sha256": digest,
                    "source": source,
                    "source_ids": [source] if source else [],
                    "chunk_seq_id": chunk_index,
                    "raw_content": raw_content,
                    "raw_chars": len(raw_content),
                    "candidate_index": rank,
                    "selected_for_injection": True,
                    "selection_status": "selected",
                    "selection_reason": "semantic_top_k",
                    "graph_path": [],
                    "graph_distance": None,
                    "relevance_rank": rank,
                    "relevance_score": (
                        round(1.0 - float(hit.get("distance")), 6)
                        if isinstance(hit.get("distance"), (int, float))
                        else None
                    ),
                    "semantic_score": (
                        round(1.0 - float(hit.get("distance")), 6)
                        if isinstance(hit.get("distance"), (int, float))
                        else None
                    ),
                    "lexical_score": None,
                    "concept_match_score": None,
                    "injected_content": raw_content if found_verbatim else "",
                    "injected_chars": len(raw_content) if found_verbatim else 0,
                    "is_complete": bool(
                        found_verbatim and not hit.get("content_truncated_in_trace", False)
                    ),
                    "omitted": not found_verbatim,
                    "omitted_reason": None if found_verbatim else "not_found_in_injected_context",
                    "distance": hit.get("distance"),
                })
            unique_hashes = {record["content_sha256"] for record in chunk_records}
            chunk_summary.update({
                "retrieved": len(chunk_records),
                "unique_retrieved": len(unique_hashes),
                "selected": len(chunk_records),
                "rejected": 0,
                "duplicates": max(0, len(chunk_records) - len(unique_hashes)),
                "noise_filtered": 0,
                "injected": sum(1 for record in chunk_records if not record["omitted"]),
                "complete_injected": sum(1 for record in chunk_records if record["is_complete"]),
                "shortened": sum(
                    1
                    for record in chunk_records
                    if not record["omitted"] and not record["is_complete"]
                ),
                "omitted": sum(1 for record in chunk_records if record["omitted"]),
                "budget_omitted": 0,
            })
            chunk_summary["valid_for_complete_context_comparison"] = bool(
                chunk_summary["retrieved"] == chunk_summary["injected"]
                and chunk_summary["injected"] == chunk_summary["complete_injected"]
                and not chunk_summary["shortened"]
                and not chunk_summary["omitted"]
            )
            usage.update({
                "retrieved_chunk_count": chunk_summary["retrieved"],
                "unique_retrieved_chunk_count": chunk_summary["unique_retrieved"],
                "selected_chunk_count": chunk_summary["selected"],
                "rejected_chunk_count": chunk_summary["rejected"],
                "injected_chunk_count": chunk_summary["injected"],
                "complete_injected_chunk_count": chunk_summary["complete_injected"],
                "shortened_chunk_count": chunk_summary["shortened"],
                "omitted_chunk_count": chunk_summary["omitted"],
            })
            hits = [
                {
                    # 論文比較需要看到「實際提供給 LLM 的完整教材 chunk」。
                    # 舊版只回傳 content_preview，前端會顯示 ...[truncated 800 chars]；
                    # 這裡改為優先回傳 content_full，保留欄位相容性。
                    "content_preview": hit.get("content_full") or hit.get("content_preview") or "",
                    "content_full": hit.get("content_full") or hit.get("content_preview") or "",
                    "content_chars": hit.get("content_chars"),
                    "content_truncated_in_trace": hit.get("content_truncated_in_trace", False),
                    "metadata": {
                        "source": hit.get("source"),
                        "chunk": hit.get("chunk_idx"),
                    },
                    "distance": hit.get("distance"),
                }
                for hit in (chroma.get("hits") or [])[:top_k]
            ]
            finalized = trace_module.finalize_trace(answer)
            trace_id = finalized.get("trace_id")
        else:
            hits = []
            trace_id = None

        prompt_search_cursor = 0
        for record in chunk_records:
            injected_text = str(record.get("injected_content") or "")
            if not injected_text:
                record["prompt_span"] = None
                continue
            start = enhanced_prompt.find(injected_text, prompt_search_cursor)
            if start < 0:
                start = enhanced_prompt.find(injected_text)
            if start < 0:
                record["prompt_span"] = None
                continue
            end = start + len(injected_text)
            record["prompt_span"] = {"char_start": start, "char_end": end}
            prompt_search_cursor = end

        injected_records = [
            record for record in chunk_records
            if record.get("selected_for_injection", True) and record.get("is_complete")
        ]
        for record in chunk_records:
            has_source = bool(record.get("source") or record.get("source_ids"))
            has_locator = bool(
                record.get("chunk_seq_id") is not None
                or record.get("page_start") is not None
                or record.get("char_start") is not None
            )
            record["source_traceable"] = bool(
                has_source and record.get("chunk_id") and record.get("content_sha256")
            )
            record["precise_source_locator"] = bool(
                record.get("source") and has_locator
            )
        source_traceable_count = sum(
            1 for record in injected_records if record.get("source_traceable")
        )
        precise_source_count = sum(
            1 for record in injected_records if record.get("precise_source_locator")
        )
        if backend != "llm_only":
            chunk_summary.update({
                "source_traceable_count": source_traceable_count,
                "source_traceability_rate": (
                    source_traceable_count / len(injected_records)
                    if injected_records else 0.0
                ),
                "precise_source_locator_count": precise_source_count,
                "precise_source_locator_rate": (
                    precise_source_count / len(injected_records)
                    if injected_records else 0.0
                ),
            })

        retrieval_ok = True
        if backend != "llm_only":
            retrieval_ok = bool(
                usage.get("used")
                and usage.get("reason") not in {
                    "upstream_offline", "no_hits", "chromadb_error",
                    "skipped_non_academic", "not_used",
                }
                and chunk_summary.get("valid_for_complete_context_comparison")
            )

        return {
            "ok": True,
            "generation_ok": bool(str(answer or "").strip()),
            "retrieval_ok": retrieval_ok,
            "answer": answer,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "timing": {
                "retrieval_and_prompt_ms": retrieval_prompt_ms,
                "generation_ms": generation_ms,
                "total_ms": round((time.perf_counter() - started) * 1000, 2),
            },
            "hit_count": len(hits),
            "hits": hits,
            "chunks": chunk_records,
            "chunk_summary": chunk_summary,
            "context": _context_payload(base_prompt, enhanced_prompt, usage),
            "prompt": {
                "base_text": base_prompt,
                "final_text": enhanced_prompt,
                "base_sha256": hashlib.sha256(base_prompt.encode("utf-8")).hexdigest(),
                "final_sha256": hashlib.sha256(enhanced_prompt.encode("utf-8")).hexdigest(),
                "graph_context_mode": (
                    graph_context_mode if backend == "graphrag" else None
                ),
            },
            "usage": usage,
            "trace_id": trace_id,
        }
    except Exception as exc:
        if trace_module is not None:
            try:
                trace_module.record_error("comparison_failed", exc)
                if backend == "graphrag":
                    trace_module.finalize_trace("", get_last_graphrag_usage())
                else:
                    trace_module.finalize_trace("")
            except Exception:
                pass
        return {
            "ok": False,
            "error": str(exc),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "timing": {
                "retrieval_and_prompt_ms": None,
                "generation_ms": None,
                "total_ms": round((time.perf_counter() - started) * 1000, 2),
            },
            "answer": "",
            "generation_ok": False,
            "retrieval_ok": False if backend != "llm_only" else True,
            "hit_count": 0,
            "hits": [],
            "context": _context_payload(base_prompt, base_prompt),
            "prompt": {
                "base_text": base_prompt,
                "final_text": base_prompt,
                "base_sha256": hashlib.sha256(base_prompt.encode("utf-8")).hexdigest(),
                "final_sha256": hashlib.sha256(base_prompt.encode("utf-8")).hexdigest(),
                "graph_context_mode": (
                    graph_context_mode if backend == "graphrag" else None
                ),
            },
        }


def run_three_way_comparison(
    data: Dict[str, Any],
    metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Generate and save one fair GraphRAG/ChromaDB/LLM-only comparison.

    This helper is shared by the RAG A/B page and by quiz submission
    experiment mode, so both paths produce the same JSON schema.
    """
    question = str(data.get("question") or "").strip()
    if not question:
        raise ValueError("question required")
    try:
        top_k = max(1, min(30, int(data.get("top_k", 5))))
    except (TypeError, ValueError):
        top_k = 5
    graph_context_mode = str(
        data.get("graph_context_mode") or "seed_only"
    ).strip().lower()
    if graph_context_mode not in {"seed_only", "prerequisite_d1"}:
        graph_context_mode = "seed_only"
    knowledge_policy = str(
        data.get("knowledge_policy") or "strict"
    ).strip().lower()
    if knowledge_policy not in {"strict", "hybrid"}:
        knowledge_policy = "strict"
    experiment_version = str(data.get("experiment_version") or (
        "thesis_v4_hybrid_knowledge"
        if knowledge_policy == "hybrid"
        else "thesis_v3_injected_scope"
    ))
    comparison_schema_version = (
        "explainable_rag_comparison_v4_hybrid"
        if knowledge_policy == "hybrid"
        else "explainable_rag_comparison_v3"
    )
    try:
        max_prereq_concepts = max(0, min(30, int(data.get("max_prereq_concepts", 5))))
    except (TypeError, ValueError):
        max_prereq_concepts = 5

    generation_config = None
    if data.get("temperature") is not None:
        try:
            temperature = max(0.0, min(2.0, float(data.get("temperature"))))
        except (TypeError, ValueError):
            temperature = 0.0
        generation_config = {"temperature": temperature}

    base_prompt, search_query, mode = _comparison_prompt(data)
    started_at = _now_iso()
    started = time.perf_counter()
    # Jobs run concurrently. rag_backend and both trace stores are
    # thread-local, so this does not alter the application's global backend.
    backends = ("graphrag", "chromadb", "llm_only")
    with ThreadPoolExecutor(max_workers=len(backends)) as pool:
        futures = {
            backend: pool.submit(
                _run_comparison_side,
                backend,
                base_prompt,
                search_query,
                top_k,
                generation_config,
                graph_context_mode,
                max_prereq_concepts,
            )
            for backend in backends
        }
        sides = {name: future.result() for name, future in futures.items()}

    comparison_id = _new_comparison_id()
    status = "done" if all((sides.get(name) or {}).get("ok") for name in backends) else "partial"
    graph_chunk_summary = (sides.get("graphrag") or {}).get("chunk_summary") or {}
    chroma_chunk_summary = (sides.get("chromadb") or {}).get("chunk_summary") or {}
    chunk_comparison_valid = bool(
        graph_chunk_summary.get("valid_for_complete_context_comparison")
        and chroma_chunk_summary.get("valid_for_complete_context_comparison")
    )
    response_payload = {
        "comparison_id": comparison_id,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "status": status,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "question": question,
        "search_query": search_query,
        "comparison_mode": mode,
        "knowledge_policy": knowledge_policy,
        "experiment_version": experiment_version,
        "schema_version": comparison_schema_version,
        "top_k": top_k,
        "graph_context_mode": graph_context_mode,
        "max_prereq_concepts": max_prereq_concepts,
        "generation_config": generation_config or {},
        "chunk_comparison": {
            "valid": chunk_comparison_valid,
            "rule": (
                "Both backends must inject every chunk selected for the model verbatim. "
                "GraphRAG candidates rejected by question-aware reranking remain complete "
                "in the audit log and do not invalidate selected-context completeness."
            ),
            "graphrag": graph_chunk_summary,
            "chromadb": chroma_chunk_summary,
        },
        "graphrag": sides["graphrag"],
        "chromadb": sides["chromadb"],
        "llm_only": sides["llm_only"],
    }
    record = {
        **response_payload,
        "schema_version": comparison_schema_version,
        "metadata": metadata or {},
        "request": {
            "question": question,
            "knowledge_policy": knowledge_policy,
            "student_answer": data.get("student_answer") or data.get("user_answer"),
            "correct_answer": data.get("correct_answer"),
            "grading_feedback": data.get("grading_feedback") or data.get("feedback"),
        },
        "generation_config": generation_config or {},
        "prompt": {
            "base_text": base_prompt,
            "base_chars": len(base_prompt),
            "knowledge_policy": knowledge_policy,
        },
        "sides": sides,
        "summary": {
            "graphrag": _comparison_side_summary(sides["graphrag"]),
            "chromadb": _comparison_side_summary(sides["chromadb"]),
            "llm_only": _comparison_side_summary(sides["llm_only"]),
        },
    }
    try:
        response_payload.update(_save_comparison_record(record))
    except Exception as exc:
        response_payload["comparison_save_error"] = str(exc)
    if metadata:
        response_payload["metadata"] = metadata
    return response_payload


@rag_backend_bp.route("/compare", methods=["POST"])
def compare_ab():
    """Generate fair GraphRAG/ChromaDB/LLM-only answers from one identical base prompt."""
    data = request.get_json(silent=True) or {}
    try:
        response_payload = run_three_way_comparison(
            data,
            metadata={"source": "rag_ab_api"},
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(response_payload)


@rag_backend_bp.route("/health", methods=["GET"])
def health():
    try:
        from src.graphrag_client import healthcheck as graphrag_healthcheck
        graphrag_info = graphrag_healthcheck()
    except Exception as exc:
        graphrag_info = {"ok": False, "reason": f"healthcheck error: {exc}"}

    try:
        from src.chromadb_rag import healthcheck as chromadb_healthcheck
        chromadb_info = chromadb_healthcheck()
    except Exception as exc:
        chromadb_info = {"ok": False, "reason": f"healthcheck error: {exc}"}

    return jsonify({
        "active": rag_backend.get_active_backend(),
        "graphrag": graphrag_info,
        "chromadb": chromadb_info,
        "llm_only": {"ok": True, "reason": "baseline: no retrieval dependency"},
    })
