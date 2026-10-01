#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Per-request GraphRAG tracing for web assistant conversations.

The trace is intentionally written as both JSON and Markdown:
- JSON is stable for tools and language models.
- Markdown is quick for humans to inspect while debugging.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


_TRACE_LOCAL = threading.local()

BACKEND_ROOT = Path(__file__).resolve().parents[1]
TRACE_DIR = Path(
    os.getenv(
        "GRAPHRAG_TRACE_DIR",
        str(BACKEND_ROOT / "logs" / "graphrag_traces"),
    )
)
MAX_TEXT_LENGTH = int(os.getenv("GRAPHRAG_TRACE_MAX_TEXT", "12000"))


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_trace_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{stamp}_{uuid.uuid4().hex[:8]}"


def _clip_text(value: str, limit: int = MAX_TEXT_LENGTH) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[truncated {len(value) - limit} chars]"


def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return repr(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clip_text(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, depth + 1) for v in list(value)]
    return repr(value)


def _base_usage() -> Dict[str, Any]:
    return {
        "used": False,
        "reason": "not_used",
        "seed_count": 0,
        "expanded_count": 0,
        "snippet_count": 0,
        "prereq_material_count": 0,
        "prereq_chunk_count": 0,
        "prereq_chain_count": 0,
        "prereq_node_count": 0,
        "descendant_chain_count": 0,
        "descendant_node_count": 0,
        "total_items": 0,
        "seed_concepts": [],
        "retrieved_seed_count": 0,
        "retrieved_expanded_count": 0,
        "retrieved_snippet_count": 0,
        "retrieved_prereq_material_count": 0,
        "retrieved_prereq_chunk_count": 0,
        "retrieved_prereq_chain_count": 0,
        "retrieved_prereq_node_count": 0,
        "retrieved_descendant_chain_count": 0,
        "retrieved_descendant_node_count": 0,
        "retrieved_chunk_count": 0,
        "unique_retrieved_chunk_count": 0,
        "selected_chunk_count": 0,
        "rejected_chunk_count": 0,
        "duplicate_chunk_count": 0,
        "noise_filtered_chunk_count": 0,
        "injected_chunk_count": 0,
        "complete_injected_chunk_count": 0,
        "shortened_chunk_count": 0,
        "omitted_chunk_count": 0,
        "budget_omitted_chunk_count": 0,
        "chunk_rerank": {},
        "prereq_chunk_depth": 1,
        "prereq_chunks_per_concept": 1,
        "prereq_chunk_concepts": [],
        "context_char_budget": 0,
        "context_chars": 0,
        "context_utf8_bytes": 0,
        "context_estimated_tokens": 0,
        "context_truncated": False,
    }


def start_trace(
    question: str,
    user_id: str = "default",
    platform: str = "web",
    route: str = "web-ai/chat",
) -> Dict[str, Any]:
    trace = {
        "trace_id": _new_trace_id(),
        "started_at": _now_iso(),
        "finished_at": None,
        "status": "started",
        "route": route,
        "platform": platform,
        "user_id": user_id,
        "question": question,
        "decision": {
            "should_search": None,
            "reason": None,
        },
        "graphrag": {
            "called": False,
            "api_base": None,
            "endpoint": None,
            "top_k": None,
            "latency_ms": None,
            "result": None,
        },
        "prompt": {
            "before_chars": None,
            "after_chars": None,
            "injected_chars": None,
            "injected_utf8_bytes": None,
            "injected_estimated_tokens": None,
            "before_text": None,
            "after_text": None,
            "injected_context": None,
        },
        "chunks": {
            "records": [],
            "summary": {
                "retrieved": 0,
                "unique_retrieved": 0,
                "injected": 0,
                "complete_injected": 0,
                "shortened": 0,
                "omitted": 0,
            },
        },
        "usage": _base_usage(),
        "answer": {
            "chars": None,
            "preview": None,
        },
        "events": [],
        "files": {},
    }
    _TRACE_LOCAL.current = trace
    record_event("trace_started", {"route": route, "platform": platform})
    return trace


def get_current_trace() -> Optional[Dict[str, Any]]:
    return getattr(_TRACE_LOCAL, "current", None)


def clear_current_trace() -> None:
    if hasattr(_TRACE_LOCAL, "current"):
        delattr(_TRACE_LOCAL, "current")


def record_event(stage: str, detail: Optional[Dict[str, Any]] = None) -> None:
    trace = get_current_trace()
    if not trace:
        return
    trace.setdefault("events", []).append(
        {
            "at": _now_iso(),
            "stage": stage,
            "detail": _json_safe(detail or {}),
        }
    )


def record_skip(reason: str) -> None:
    trace = get_current_trace()
    if not trace:
        return
    trace["status"] = reason
    trace["decision"] = {
        "should_search": False,
        "reason": reason,
    }
    usage = _base_usage()
    usage["reason"] = reason
    trace["usage"] = usage
    record_event("graphrag_skipped", {"reason": reason})


def record_query(
    *,
    question: str,
    top_k: int,
    api_base: str,
    result: Dict[str, Any],
    usage: Dict[str, Any],
    latency_ms: float,
    prompt_before_chars: int,
    prompt_after_chars: int,
    # ✨ 論文對比用：完整的 prompt 文字（送給 LLM 的原文）
    # 這 3 個都是 optional 為了向後相容 —— 舊呼叫端不帶也不會壞
    prompt_before_text: Optional[str] = None,
    prompt_after_text: Optional[str] = None,
    injected_context_text: Optional[str] = None,
    # Full retrieval snapshots are preserved outside the generic preview
    # clipping path so the trace can prove exactly what was found and injected.
    chunk_records: Optional[List[Dict[str, Any]]] = None,
) -> None:
    trace = get_current_trace()
    if not trace:
        return

    endpoint = f"{api_base.rstrip('/')}/query"
    safe_result = _json_safe(result)
    safe_usage = _json_safe(usage)

    trace["status"] = "used" if usage.get("used") else usage.get("reason", "no_hits")
    trace["decision"] = {
        "should_search": True,
        "reason": "academic_or_learning_intent",
    }
    trace["graphrag"] = {
        "called": True,
        "api_base": api_base,
        "endpoint": endpoint,
        "top_k": top_k,
        "latency_ms": latency_ms,
        "query": question,
        "result": safe_result,
    }
    trace["prompt"] = {
        "before_chars": prompt_before_chars,
        "after_chars": prompt_after_chars,
        "injected_chars": max(prompt_after_chars - prompt_before_chars, 0),
        "injected_utf8_bytes": len((injected_context_text or "").encode("utf-8")),
        "injected_estimated_tokens": max(0, round(len(injected_context_text or "") / 4)),
        # ✨ 完整 prompt 文字（論文對比用）
        # before_text = 沒 GraphRAG 前的 prompt（純教學角色 + 題目 + 學生答案）
        # after_text  = GraphRAG 增強後的完整 prompt（before + injected）
        # injected    = GraphRAG 這次「新塞」進去的 KG 結構化 context
        "before_text": prompt_before_text,
        "after_text": prompt_after_text,
        "injected_context": injected_context_text,
    }
    records = deepcopy(chunk_records or [])
    content_hashes = {
        str(item.get("content_sha256") or item.get("chunk_id") or "")
        for item in records
        if item.get("content_sha256") or item.get("chunk_id")
    }
    trace["chunks"] = {
        "records": records,
        "summary": {
            "retrieved": len(records),
            "unique_retrieved": len(content_hashes),
            "selected": sum(
                1 for item in records if item.get("selected_for_injection")
            ),
            "rejected": sum(
                1 for item in records if not item.get("selected_for_injection")
            ),
            "duplicates": sum(
                1 for item in records
                if item.get("omitted_reason") == "duplicate_content"
            ),
            "noise_filtered": sum(
                1 for item in records
                if str(item.get("omitted_reason") or "").endswith("_section")
                or item.get("omitted_reason") == "reference_list"
            ),
            "injected": sum(1 for item in records if not item.get("omitted", True)),
            "complete_injected": sum(1 for item in records if item.get("is_complete")),
            "shortened": sum(
                1
                for item in records
                if not item.get("omitted", True) and not item.get("is_complete")
            ),
            "omitted": sum(1 for item in records if item.get("omitted", True)),
            "budget_omitted": sum(
                1 for item in records
                if item.get("selected_for_injection")
                and item.get("omitted_reason") == "context_budget"
            ),
        },
    }
    trace["usage"] = safe_usage
    record_event(
        "graphrag_query_completed",
        {
            "endpoint": endpoint,
            "top_k": top_k,
            "latency_ms": latency_ms,
            "used": usage.get("used"),
            "seed_concepts": usage.get("seed_concepts", []),
            "total_items": usage.get("total_items", 0),
            "retrieved_chunks": len(records),
            "selected_chunks": trace["chunks"]["summary"]["selected"],
            "rejected_chunks": trace["chunks"]["summary"]["rejected"],
            "injected_chunks": trace["chunks"]["summary"]["injected"],
        },
    )


def record_error(stage: str, error: Exception) -> None:
    trace = get_current_trace()
    if not trace:
        return
    trace["status"] = "error"
    record_event(stage, {"error": str(error), "type": type(error).__name__})


def finalize_trace(answer: Any, usage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    trace = get_current_trace()
    if not trace:
        return dict(usage or _base_usage())

    final_usage = dict(usage or trace.get("usage") or _base_usage())
    trace["usage"] = _json_safe(final_usage)
    answer_text = "" if answer is None else str(answer)
    trace["answer"] = {
        "chars": len(answer_text),
        "preview": _clip_text(answer_text, 1200),
    }
    trace["finished_at"] = _now_iso()
    if trace.get("status") == "started":
        trace["status"] = final_usage.get("reason", "completed")

    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    json_path = TRACE_DIR / f"{trace['trace_id']}.json"
    md_path = TRACE_DIR / f"{trace['trace_id']}.md"
    trace["files"] = {
        "json": str(json_path),
        "markdown": str(md_path),
    }
    record_event("trace_written", {"json": str(json_path), "markdown": str(md_path)})

    json_payload = _json_safe(trace)
    # Prompt/context is the experimental evidence.  Preserve it verbatim instead
    # of applying the generic 12k trace preview limit used for retrieval payloads.
    # The injected GraphRAG context already has a bounded size upstream.
    json_payload["prompt"] = deepcopy(trace.get("prompt") or {})
    json_payload["chunks"] = deepcopy(trace.get("chunks") or {})
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(json_payload, f, ensure_ascii=False, indent=2)
    with md_path.open("w", encoding="utf-8") as f:
        f.write(_render_markdown(json_payload))

    enriched_usage = dict(final_usage)
    enriched_usage["trace_id"] = trace["trace_id"]
    enriched_usage["trace_json_path"] = str(json_path)
    enriched_usage["trace_markdown_path"] = str(md_path)
    clear_current_trace()
    return enriched_usage


def _render_markdown(trace: Dict[str, Any]) -> str:
    usage = trace.get("usage") or {}
    graphrag = trace.get("graphrag") or {}
    result = graphrag.get("result") or {}
    prompt = trace.get("prompt") or {}
    chunks = trace.get("chunks") or {}
    chunk_summary = chunks.get("summary") or {}
    answer = trace.get("answer") or {}

    lines: List[str] = [
        f"# GraphRAG Trace {trace.get('trace_id')}",
        "",
        "## Request",
        f"- Time: {trace.get('started_at')} -> {trace.get('finished_at')}",
        f"- User: {trace.get('user_id')}",
        f"- Platform: {trace.get('platform')}",
        f"- Route: {trace.get('route')}",
        f"- Status: {trace.get('status')}",
        "",
        "### Question",
        "```text",
        str(trace.get("question") or ""),
        "```",
        "",
        "## Decision",
        f"- Should search GraphRAG: {trace.get('decision', {}).get('should_search')}",
        f"- Reason: {trace.get('decision', {}).get('reason')}",
        "",
        "## GraphRAG Call",
        f"- Called: {graphrag.get('called')}",
        f"- Endpoint: {graphrag.get('endpoint')}",
        f"- top_k: {graphrag.get('top_k')}",
        f"- Latency ms: {graphrag.get('latency_ms')}",
        "",
        "## Usage Summary",
        f"- Used: {usage.get('used')}",
        f"- Reason: {usage.get('reason')}",
        f"- Seed concepts injected/retrieved: {usage.get('seed_count')} / {usage.get('retrieved_seed_count', usage.get('seed_count'))} ({', '.join(usage.get('seed_concepts') or [])})",
        f"- Expanded concepts injected/retrieved: {usage.get('expanded_count')} / {usage.get('retrieved_expanded_count', usage.get('expanded_count'))}",
        f"- Snippets injected/retrieved: {usage.get('snippet_count')} / {usage.get('retrieved_snippet_count', usage.get('snippet_count'))}",
        f"- Prereq material concepts injected/retrieved: {usage.get('prereq_material_count')} / {usage.get('retrieved_prereq_material_count', usage.get('prereq_material_count'))}",
        f"- Prereq material chunks injected/retrieved: {usage.get('prereq_chunk_count')} / {usage.get('retrieved_prereq_chunk_count', usage.get('prereq_chunk_count'))}",
        f"- Prereq material depth/chunks-per-concept: {usage.get('prereq_chunk_depth')} / {usage.get('prereq_chunks_per_concept')}",
        f"- Prereq material concepts: {', '.join(usage.get('prereq_chunk_concepts') or [])}",
        f"- Prereq chains injected/retrieved: {usage.get('prereq_chain_count')} / {usage.get('retrieved_prereq_chain_count', usage.get('prereq_chain_count'))}",
        f"- Prereq nodes injected/retrieved: {usage.get('prereq_node_count')} / {usage.get('retrieved_prereq_node_count', usage.get('prereq_node_count'))}",
        f"- Descendant chains injected/retrieved: {usage.get('descendant_chain_count')} / {usage.get('retrieved_descendant_chain_count', usage.get('descendant_chain_count'))}",
        f"- Descendant nodes injected/retrieved: {usage.get('descendant_node_count')} / {usage.get('retrieved_descendant_node_count', usage.get('descendant_node_count'))}",
        f"- Total injected items: {usage.get('total_items')}",
        f"- Context chars/budget: {usage.get('context_chars')} / {usage.get('context_char_budget')}",
        f"- Context UTF-8 bytes: {usage.get('context_utf8_bytes')}",
        f"- Context estimated tokens: {usage.get('context_estimated_tokens')}",
        f"- Context truncated: {usage.get('context_truncated')}",
        f"- Textbook chunks retrieved/unique: {chunk_summary.get('retrieved', 0)} / {chunk_summary.get('unique_retrieved', 0)}",
        f"- Textbook chunks selected/rejected: {chunk_summary.get('selected', 0)} / {chunk_summary.get('rejected', 0)}",
        f"- Duplicate/noise candidates rejected: {chunk_summary.get('duplicates', 0)} / {chunk_summary.get('noise_filtered', 0)}",
        f"- Textbook chunks injected/complete: {chunk_summary.get('injected', 0)} / {chunk_summary.get('complete_injected', 0)}",
        f"- Textbook chunks shortened/omitted/budget-omitted: {chunk_summary.get('shortened', 0)} / {chunk_summary.get('omitted', 0)} / {chunk_summary.get('budget_omitted', 0)}",
        f"- Chunk reranker: {(usage.get('chunk_rerank') or {}).get('backend')} | mode={(usage.get('chunk_rerank') or {}).get('scoring_mode')} | core-threshold={(usage.get('chunk_rerank') or {}).get('effective_threshold')} | prerequisite-first={(usage.get('chunk_rerank') or {}).get('prerequisite_first')}",
        "",
        "## Prompt Injection",
        f"- Before chars: {prompt.get('before_chars')}",
        f"- After chars: {prompt.get('after_chars')}",
        f"- Injected chars: {prompt.get('injected_chars')}",
        f"- Injected UTF-8 bytes: {prompt.get('injected_utf8_bytes')}",
        f"- Injected estimated tokens: {prompt.get('injected_estimated_tokens')}",
        "",
    ]

    chunk_records = chunks.get("records") or []
    if chunk_records:
        lines.extend([
            "## Textbook Chunk Audit (verbatim retrieval evidence)",
            "",
            "> `raw_content` is the unabridged text returned by retrieval. ",
            "> Complete injected chunks also appear verbatim in the final prompt below.",
            "",
        ])
        for index, item in enumerate(chunk_records, 1):
            lines.extend([
                f"### Chunk {index}: {item.get('concept') or '?'} ({item.get('section') or '?'})",
                f"- Chunk ID: {item.get('chunk_id')}",
                f"- SHA-256: {item.get('content_sha256')}",
                f"- Source: {item.get('source') or ''}",
                f"- Source IDs: {', '.join(item.get('source_ids') or [])}",
                f"- Chunk sequence: {item.get('chunk_seq_id')}",
                f"- Chapter: {item.get('chapter_id') or ''}",
                f"- Pages: {item.get('page_start')} -> {item.get('page_end')}",
                f"- Source char range: {item.get('char_start')} -> {item.get('char_end')}",
                f"- Retrieved chars: {item.get('raw_chars')}",
                f"- Selection status: {item.get('selection_status')}",
                f"- Selected for injection: {item.get('selected_for_injection')}",
                f"- Injection priority: {item.get('selection_priority')}",
                f"- Relevance rank/score: {item.get('relevance_rank')} / {item.get('relevance_score')}",
                f"- Scoring basis/scope rank: {item.get('scoring_basis')} / {item.get('score_scope_rank')}",
                f"- Scoring query: {item.get('scoring_query') or ''}",
                f"- Source seed rank / graph distance: {item.get('source_seed_rank')} / {item.get('prerequisite_distance')}",
                f"- Prerequisite targets: {item.get('prerequisite_target_ranks') or {}}",
                f"- Effective threshold: {item.get('effective_threshold')}",
                f"- Semantic/lexical/concept scores: {item.get('semantic_score')} / {item.get('lexical_score')} / {item.get('concept_match_score')}",
                f"- Section priority bonus: {item.get('section_priority_bonus', 0)}",
                f"- Weak concept-evidence penalty: {item.get('weak_concept_evidence_penalty', 0)} | anchors={item.get('concept_anchor_tokens') or []} | occurrences={item.get('concept_anchor_evidence_count', 0)}",
                f"- Duplicate of SHA-256: {item.get('duplicate_of_sha256')}",
                f"- Injected chars: {item.get('injected_chars')}",
                f"- Complete: {item.get('is_complete')}",
                f"- Omitted: {item.get('omitted')}",
                f"- Omitted reason: {item.get('omitted_reason')}",
                "",
                "#### Retrieved Raw Content",
                "```text",
                str(item.get("raw_content") or ""),
                "```",
                "",
            ])
            if not item.get("is_complete"):
                lines.extend([
                    "#### Actually Injected Content",
                    "```text",
                    str(item.get("injected_content") or ""),
                    "```",
                    "",
                ])

    # ✨ 論文對比用：完整 prompt 三段（before / injected / after）
    # 若沒紀錄就跳過，不吵人
    before_text = prompt.get("before_text")
    injected_ctx = prompt.get("injected_context")
    after_text = prompt.get("after_text")
    if before_text or injected_ctx or after_text:
        lines.extend([
            "## Full Prompt Texts (for thesis comparison)",
            "",
            "> 這三段是本次 GraphRAG 實際「送給 LLM」的完整內容，",
            "> 可與 ChromaDB trace 對應欄位並列比較資料完整度差異。",
            "",
        ])
        if before_text is not None:
            lines.extend([
                "### 1) Prompt Before GraphRAG Enhancement",
                "（原始教學 prompt = 教學角色 + 題目 + 學生答案 + 對話歷史）",
                "",
                "```text",
                str(before_text),
                "```",
                "",
            ])
        if injected_ctx is not None:
            lines.extend([
                "### 2) GraphRAG Injected Context",
                "（GraphRAG 從 Neo4j 抽出的結構化知識 —— 這是與純向量 RAG 的關鍵差異）",
                "",
                "```text",
                str(injected_ctx),
                "```",
                "",
            ])
        if after_text is not None:
            lines.extend([
                "### 3) Final Prompt Sent to LLM (Before + Injected)",
                "",
                "```text",
                str(after_text),
                "```",
                "",
            ])

    retrieval_trace = result.get("retrieval_trace") or {}
    if retrieval_trace:
        embedding = retrieval_trace.get("embedding") or {}
        vector_search = retrieval_trace.get("vector_search") or {}
        graph_expansion = retrieval_trace.get("graph_expansion") or {}
        lines.extend(
            [
                "## Retrieval Evidence",
                f"- Pipeline: {' -> '.join(retrieval_trace.get('pipeline') or [])}",
                f"- Embedding backend: {embedding.get('backend')}",
                f"- Embedding model: {embedding.get('model')}",
                f"- Embedding task: {embedding.get('task_type')}",
                f"- Embedding vector length: {embedding.get('vector_length')}",
                f"- Neo4j database: {vector_search.get('database')}",
                f"- Vector index: {vector_search.get('index_name')}",
                f"- Vector query: {vector_search.get('cypher')}",
                f"- Graph relation used for prerequisites: {graph_expansion.get('prereq_relation')}",
                f"- Prereq depth: {graph_expansion.get('prereq_depth')}",
                f"- Descendant depth: {graph_expansion.get('descendant_depth')}",
                "",
            ]
        )

        seed_matches = vector_search.get("seed_matches") or []
        if seed_matches:
            lines.extend(["### Vector Seed Matches", ""])
            for item in seed_matches[:20]:
                score = item.get("score")
                score_text = f"{score:.4f}" if isinstance(score, (int, float)) else str(score)
                lines.append(
                    f"- {item.get('name')} | score={score_text} | category={item.get('category')}"
                )
            lines.append("")

    seeds = result.get("seed_concepts") or []
    if seeds:
        lines.extend(["## Seed Concepts", ""])
        lines.extend(f"- {seed}" for seed in seeds[:20])
        lines.append("")

    remedial = result.get("stateful_remediation") or {}
    if remedial:
        lines.extend([
            "## Stateful Remedial Learning",
            "",
            f"- Active core concept: {remedial.get('active_core_concept')}",
            f"- Active teaching concept: {remedial.get('active_teaching_concept')}",
            f"- Teaching mode: {remedial.get('teaching_mode')}",
            f"- Cached concepts: {', '.join(remedial.get('cached_concepts') or [])}",
            "",
        ])
        gap = remedial.get("last_knowledge_gap") or {}
        if gap:
            lines.extend([
                "### Last Knowledge Gap",
                "",
                f"- Core concept: {gap.get('core_concept')}",
                f"- Evidence: {gap.get('text')}",
                "",
            ])
        queue = remedial.get("prerequisite_queue") or []
        if queue:
            lines.extend(["### Prerequisite Queue", ""])
            for item in queue:
                components = item.get("score_components") or {}
                lines.append(
                    f"- {item.get('name')} | status={item.get('status')} | "
                    f"priority={item.get('priority_score')} | seed_rank={item.get('seed_rank')} | "
                    f"distance={item.get('distance')} | components={components}"
                )
            lines.append("")
        rejections = remedial.get("prerequisite_rejections") or []
        if rejections:
            lines.extend(["### Rejected Prerequisites (Anti-drift Guard)", ""])
            for item in rejections:
                lines.append(
                    f"- {item.get('name')} | parent_of={item.get('parent_of')} | "
                    f"reason={item.get('reason')} | depth={item.get('graph_depth')} | "
                    f"gap_semantic={item.get('knowledge_gap_semantic')} | "
                    f"chunk_evidence={item.get('chunk_evidence_quality')}"
                )
            lines.append("")

    expanded = result.get("expanded") or []
    if expanded:
        lines.extend([
            "## Retrieved Graph Expansion (audit only)",
            "",
            "> 以下是上游圖譜回傳資料，用於稽核；是否送入 LLM 請以 Usage Summary 的 injected 數量與 GraphRAG Injected Context 為準。",
            "",
        ])
        for item in expanded[:10]:
            lines.append(f"### {item.get('name')}")
            lines.append(f"- Prerequisites: {', '.join(item.get('prerequisites') or [])}")
            lines.append(f"- Leads to: {', '.join(item.get('leads_to') or [])}")
            chunks = item.get("sample_chunks") or []
            for i, chunk in enumerate(chunks[:2], 1):
                lines.append(f"- Snippet {i}: {_clip_text(str(chunk), 500)}")
            lines.append("")

    prereq_chains = result.get("prereq_chains") or []
    if prereq_chains:
        lines.extend([
            "## Retrieved Prerequisite Chains (audit only)",
            "",
            "> 距離 2 以上節點只保留於 trace，不會注入初次 Prompt。",
            "",
        ])
        for chain in prereq_chains[:10]:
            names = [node.get("name", "?") for node in (chain.get("ancestors") or [])]
            lines.append(f"- {chain.get('concept')}: {' -> '.join(names)}")
        lines.append("")

    descendant_chains = result.get("descendant_chains") or []
    if descendant_chains:
        lines.extend([
            "## Retrieved Descendant Chains (audit only; locked until score >= 90)",
            "",
            "> 未達 90 分前，這些節點只存在於檢索稽核資料，不會注入 Prompt。",
            "",
        ])
        for c in descendant_chains[:10]:
            names = [d.get("name") for d in c.get("descendants", [])]
            lines.append(f"- {c.get('concept', '?')} -> {' -> '.join(names)}")
        lines.append("")

    return "\n".join(lines)


def list_traces(limit: int = 20) -> List[Dict[str, Any]]:
    """列出最近 N 筆 trace（給 UI 檢視器用）。"""
    try:
        files = sorted(
            [f for f in os.listdir(TRACE_DIR) if f.endswith(".json")],
            reverse=True,
        )[:limit]
        result = []
        for fn in files:
            try:
                with open(os.path.join(TRACE_DIR, fn), encoding="utf-8") as f:
                    result.append(json.load(f))
            except Exception:
                pass
        return result
    except Exception:
        return []


def read_trace(trace_id: str) -> Optional[Dict[str, Any]]:
    """讀單筆 trace（給 UI 詳情頁 / 論文匯出用）。

    trace_id 可以是完整檔名（不含 .json）或前綴（會取第一個符合的檔）。
    """
    try:
        # 完整匹配優先
        exact = os.path.join(str(TRACE_DIR), f"{trace_id}.json")
        if os.path.exists(exact):
            with open(exact, encoding="utf-8") as fp:
                return json.load(fp)
        # 前綴匹配（trace_id 常常只給前面時間戳）
        for fn in sorted(os.listdir(str(TRACE_DIR)), reverse=True):
            if fn.startswith(trace_id) and fn.endswith(".json"):
                with open(os.path.join(str(TRACE_DIR), fn), encoding="utf-8") as fp:
                    return json.load(fp)
    except Exception:
        pass
    return None
