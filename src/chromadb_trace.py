#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Per-request ChromaDB RAG tracing（跟 graphrag_trace 對稱）。

寫兩個檔：
- logs/chromadb_traces/{timestamp}_{uuid}.json  → 結構化
- logs/chromadb_traces/{timestamp}_{uuid}.md    → 人看的排版

用法（在 chromadb_rag.py 內部呼叫）：
    from src.chromadb_trace import start_trace, record_query, finalize_trace

    trace = start_trace(question=question, top_k=top_k)
    ...檢索...
    record_query(question, top_k, items, latency_ms, prompt_before_chars, prompt_after_chars)
    finalize_trace(answer=answer_text)
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 跟 graphrag_trace 共用 utility（避免程式重複）
try:
    from src.graphrag_trace import (
        _clip_text,
        _json_safe,
        _now_iso,
    )
except Exception:
    # 保險 fallback：如果 import 失敗，就自己定義
    def _now_iso() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _clip_text(v: str, limit: int = 12000) -> str:
        return v if len(v) <= limit else v[:limit] + "...[truncated]"

    def _json_safe(v: Any, depth: int = 0) -> Any:
        if depth > 6:
            return repr(v)
        if v is None or isinstance(v, (bool, int, float)):
            return v
        if isinstance(v, str):
            return _clip_text(v)
        if isinstance(v, dict):
            return {str(k): _json_safe(x, depth + 1) for k, x in v.items()}
        if isinstance(v, (list, tuple, set)):
            return [_json_safe(x, depth + 1) for x in list(v)]
        return repr(v)


_TRACE_LOCAL = threading.local()
BACKEND_ROOT = Path(__file__).resolve().parents[1]
TRACE_DIR = Path(
    os.getenv(
        "CHROMADB_TRACE_DIR",
        str(BACKEND_ROOT / "logs" / "chromadb_traces"),
    )
)


def _new_trace_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{stamp}_{uuid.uuid4().hex[:8]}"


def start_trace(
    question: str,
    user_id: str = "default",
    platform: str = "web",
    route: str = "rag_ai_role",
    top_k: int = 5,
) -> Dict[str, Any]:
    trace = {
        "trace_id": _new_trace_id(),
        "backend": "chromadb",
        "started_at": _now_iso(),
        "finished_at": None,
        "status": "started",
        "route": route,
        "platform": platform,
        "user_id": user_id,
        "question": question,
        "chromadb": {
            "called": False,
            "collection": os.getenv("CHROMADB_COLLECTION", "textbook_knowledge"),
            "top_k": top_k,
            "latency_ms": None,
            "hits": [],   # [{"source", "chunk", "content_preview", "content_full", "distance"}]
            "vector_count_at_query": None,
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
        "answer": {
            "chars": None,
            "preview": None,
        },
        "events": [],
    }
    _TRACE_LOCAL.current = trace
    return trace


def get_current_trace() -> Optional[Dict[str, Any]]:
    return getattr(_TRACE_LOCAL, "current", None)


def clear_current_trace() -> None:
    if hasattr(_TRACE_LOCAL, "current"):
        delattr(_TRACE_LOCAL, "current")


def record_query(
    hits: List[Dict[str, Any]],
    latency_ms: float,
    vector_count: Optional[int] = None,
    prompt_before_chars: Optional[int] = None,
    prompt_after_chars: Optional[int] = None,
    # ✨ 論文對比用：完整 prompt 三段（跟 graphrag_trace 平行）
    prompt_before_text: Optional[str] = None,
    prompt_after_text: Optional[str] = None,
    injected_context_text: Optional[str] = None,
) -> None:
    trace = get_current_trace()
    if not trace:
        return
    slim_hits = []
    for h in hits[:20]:
        content = (h.get("content") or "")
        slim_hits.append({
            "source": (h.get("metadata") or {}).get("source"),
            "chunk_idx": (h.get("metadata") or {}).get("chunk"),
            # 論文實驗需要能完整比對「檢索到且提供給 LLM 的教材 chunk」。
            # 舊版 content_preview 只存前 400 字，畫面會出現 ...[truncated 800 chars]。
            # 為了相容既有前端欄位，content_preview 現在也保存完整 chunk；
            # 若只需要短摘要，使用 content_excerpt。
            "content_preview": content,
            "content_excerpt": content[:400],
            "content_full": content,
            "content_chars": len(content),
            "content_truncated_in_trace": False,
            "distance": h.get("distance"),
        })
    trace["chromadb"]["called"] = True
    trace["chromadb"]["latency_ms"] = round(latency_ms, 2)
    trace["chromadb"]["hits"] = slim_hits
    trace["chromadb"]["vector_count_at_query"] = vector_count
    trace["chromadb"]["hit_count"] = len(hits)
    if prompt_before_chars is not None:
        trace["prompt"]["before_chars"] = prompt_before_chars
    if prompt_after_chars is not None:
        trace["prompt"]["after_chars"] = prompt_after_chars
        if prompt_before_chars is not None:
            trace["prompt"]["injected_chars"] = prompt_after_chars - prompt_before_chars
    # ✨ 完整 prompt 文字（給論文對比 GraphRAG vs ChromaDB）
    if prompt_before_text is not None:
        trace["prompt"]["before_text"] = prompt_before_text
    if prompt_after_text is not None:
        trace["prompt"]["after_text"] = prompt_after_text
    if injected_context_text is not None:
        trace["prompt"]["injected_context"] = injected_context_text
        trace["prompt"]["injected_utf8_bytes"] = len(
            injected_context_text.encode("utf-8")
        )
        trace["prompt"]["injected_estimated_tokens"] = max(
            0, round(len(injected_context_text) / 4)
        )


def record_skip(reason: str) -> None:
    trace = get_current_trace()
    if not trace:
        return
    trace["status"] = f"skipped:{reason}"


def record_error(stage: str, error: Exception) -> None:
    trace = get_current_trace()
    if not trace:
        return
    trace["status"] = f"error:{stage}"
    trace.setdefault("events", []).append({
        "at": _now_iso(),
        "stage": stage,
        "error": str(error),
    })


def _render_markdown(trace: Dict[str, Any]) -> str:
    lines = []
    lines.append(f"# ChromaDB Trace `{trace['trace_id']}`")
    lines.append("")
    lines.append(f"- **開始**: {trace['started_at']}")
    lines.append(f"- **結束**: {trace.get('finished_at') or '(未關閉)'}")
    lines.append(f"- **狀態**: {trace.get('status')}")
    lines.append(f"- **來源**: {trace.get('route')}  /  平台: {trace.get('platform')}")
    lines.append("")
    lines.append("## 問題")
    lines.append(f"> {_clip_text(trace.get('question', ''), 800)}")
    lines.append("")

    cdb = trace.get("chromadb") or {}
    lines.append("## ChromaDB 檢索")
    lines.append(f"- collection: `{cdb.get('collection')}`")
    lines.append(f"- top_k: {cdb.get('top_k')}")
    lines.append(f"- vector_count: {cdb.get('vector_count_at_query')}")
    lines.append(f"- hit_count: {cdb.get('hit_count')}")
    lines.append(f"- latency: {cdb.get('latency_ms')} ms")
    for i, h in enumerate((cdb.get("hits") or [])[:10], 1):
        lines.append(f"\n### 第 {i} 筆檢索結果")
        lines.append(f"- 來源: `{h.get('source')}` chunk={h.get('chunk_idx')}")
        lines.append(f"- distance: {h.get('distance')}")
        lines.append("")
        lines.append("```")
        lines.append(h.get("content_full") or h.get("content_preview") or "")
        lines.append("```")

    prm = trace.get("prompt") or {}
    lines.append("\n## Prompt 增強")
    lines.append(f"- before: {prm.get('before_chars')} chars")
    lines.append(f"- after:  {prm.get('after_chars')} chars")
    lines.append(f"- injected: {prm.get('injected_chars')} chars")
    lines.append(f"- injected UTF-8 bytes: {prm.get('injected_utf8_bytes')}")
    lines.append(f"- injected estimated tokens: {prm.get('injected_estimated_tokens')}")

    # ✨ 論文對比用：完整 prompt 三段（跟 graphrag_trace 同格式）
    before_text = prm.get("before_text")
    injected_ctx = prm.get("injected_context")
    after_text = prm.get("after_text")
    if before_text or injected_ctx or after_text:
        lines.append("\n## Full Prompt Texts (for thesis comparison)")
        lines.append("")
        lines.append("> 這三段是本次 ChromaDB RAG 實際「送給 LLM」的完整內容，")
        lines.append("> 可與 GraphRAG trace 對應欄位並列比較資料完整度差異。")
        lines.append("")
        if before_text is not None:
            lines.append("### 1) Prompt Before ChromaDB Enhancement")
            lines.append("（原始教學 prompt = 教學角色 + 題目 + 學生答案 + 對話歷史）")
            lines.append("")
            lines.append("```text")
            lines.append(str(before_text))
            lines.append("```")
            lines.append("")
        if injected_ctx is not None:
            lines.append("### 2) ChromaDB Injected Context")
            lines.append("（純向量檢索的教材原文片段 —— 沒有結構化關係）")
            lines.append("")
            lines.append("```text")
            lines.append(str(injected_ctx))
            lines.append("```")
            lines.append("")
        if after_text is not None:
            lines.append("### 3) Final Prompt Sent to LLM (Before + Injected)")
            lines.append("")
            lines.append("```text")
            lines.append(str(after_text))
            lines.append("```")
            lines.append("")

    ans = trace.get("answer") or {}
    lines.append("\n## AI 回答")
    lines.append(f"- 長度: {ans.get('chars')} chars")
    lines.append("")
    lines.append(ans.get("preview") or "(空)")

    return "\n".join(lines)


def finalize_trace(answer: Any) -> Dict[str, Any]:
    trace = get_current_trace()
    if not trace:
        return {}
    answer_text = answer if isinstance(answer, str) else str(answer or "")
    trace["finished_at"] = _now_iso()
    trace["answer"]["chars"] = len(answer_text)
    trace["answer"]["preview"] = _clip_text(answer_text, 1600)
    trace["status"] = "used" if (trace.get("chromadb") or {}).get("called") else "skipped"

    # 寫 JSON + MD
    trace_id = trace["trace_id"]
    json_path = os.path.join(TRACE_DIR, f"{trace_id}.json")
    md_path = os.path.join(TRACE_DIR, f"{trace_id}.md")
    try:
        os.makedirs(TRACE_DIR, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(trace, f, ensure_ascii=False, indent=2)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(_render_markdown(trace))
    except Exception as e:
        print(f"chromadb_trace finalize error: {e}")

    result = dict(trace)
    result["trace_json_path"] = json_path
    result["trace_markdown_path"] = md_path
    clear_current_trace()
    return result


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
