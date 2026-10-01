"""RAG backend 切換器：讓 rag_ai_role 可以在 GraphRAG / ChromaDB 之間自由選擇。

三種切換方式（優先度由高到低）：

1. Runtime API 覆寫（`set_backend()`）
   → 前端切下拉、或 curl `POST /api/rag/backend`
   → 有效直到重啟或再切
2. Request-level 覆寫（`with_backend()`）
   → 只影響單次呼叫（A/B 比較實驗用）
3. 環境變數 `RAG_BACKEND=graphrag|chromadb`
   → 預設值

使用：
    from src.rag_backend import get_active_backend, set_backend

    backend = get_active_backend()   # 'graphrag' or 'chromadb'
    if backend == 'chromadb':
        ...
    else:
        ...

    # 動態切
    set_backend('chromadb')          # 全域
    with with_backend('chromadb'):   # 單次
        do_query(...)
"""
from __future__ import annotations

import contextlib
import os
import threading
from typing import Iterator, Literal, Optional

# llm_only = 純 LLM baseline，完全不呼叫 RAG，用來對照論文
BackendName = Literal["graphrag", "chromadb", "llm_only"]

_VALID = ("graphrag", "chromadb", "llm_only")
_lock = threading.RLock()

# global override（None 代表未覆寫，讀 env）
_global_override: Optional[BackendName] = None

# thread-local override（給單次 request 用，比 global override 更優先）
_request_local = threading.local()


def _read_env_default() -> BackendName:
    val = (os.getenv("RAG_BACKEND") or "graphrag").strip().lower()
    if val not in _VALID:
        val = "graphrag"
    return val  # type: ignore[return-value]


def get_active_backend() -> BackendName:
    """回傳目前實際生效的 backend。"""
    # 1. thread-local 最高優先
    override = getattr(_request_local, "backend", None)
    if override in _VALID:
        return override
    # 2. global runtime override
    with _lock:
        if _global_override in _VALID:
            return _global_override  # type: ignore[return-value]
    # 3. env default
    return _read_env_default()


def set_backend(name: str) -> BackendName:
    """全域切換 backend（會持續到重啟或再切）。回傳實際切到的名稱。"""
    global _global_override
    name = (name or "").strip().lower()
    if name not in _VALID:
        raise ValueError(f"invalid backend: {name!r}, must be one of {_VALID}")
    with _lock:
        _global_override = name  # type: ignore[assignment]
    return name  # type: ignore[return-value]


def reset_backend() -> BackendName:
    """清掉 global override，回到讀 env 的預設值。"""
    global _global_override
    with _lock:
        _global_override = None
    return _read_env_default()


@contextlib.contextmanager
def with_backend(name: str) -> Iterator[BackendName]:
    """單次 request 覆寫（不影響全域）。適合 A/B 比較實驗。

    with with_backend('chromadb'):
        result = search_knowledge('...')
    """
    name = (name or "").strip().lower()
    if name not in _VALID:
        raise ValueError(f"invalid backend: {name!r}")
    prev = getattr(_request_local, "backend", None)
    _request_local.backend = name
    try:
        yield name  # type: ignore[misc]
    finally:
        if prev is None:
            try:
                del _request_local.backend
            except AttributeError:
                pass
        else:
            _request_local.backend = prev


def status() -> dict:
    """給 API / debug 用；回傳目前狀態全貌。"""
    with _lock:
        return {
            "active": get_active_backend(),
            "env_default": _read_env_default(),
            "global_override": _global_override,
            "request_override": getattr(_request_local, "backend", None),
            "valid": list(_VALID),
        }
