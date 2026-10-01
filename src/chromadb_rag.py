"""舊版 ChromaDB RAG 檢索（從備份 rag_ai_role.py 撿回，重新整理）。

用來給 rag_backend 切換到 'chromadb' 時使用。

跟舊版差別：
* 教材已改成中文 markdown（data/materials/*.md），所以拿掉了「翻英再查」的步驟
* 拆成獨立 module，不再跟 rag_ai_role.py 混在一起
* 用 lazy import，沒裝 chromadb 也不會炸 rag_ai_role import
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# lazy 快取
_client = None
_collection = None


def _get_db_path() -> str:
    """回舊 ChromaDB 存放位置：src/rag_sys/data/knowledge_db/chroma_db"""
    src_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(
        src_dir, "rag_sys", "data", "knowledge_db", "chroma_db"
    )


def init_vector_database() -> Tuple[Any, Any]:
    """開 ChromaDB（懶初始化 + 快取）。回傳 (client, collection)；失敗回 (None, None)。"""
    global _client, _collection

    if _collection is not None:
        return _client, _collection

    try:
        import chromadb  # type: ignore
        from chromadb.config import Settings  # type: ignore
    except ImportError:
        logger.warning(
            "⚠️ chromadb 未安裝；請 pip install chromadb 才能用舊 RAG backend"
        )
        return None, None

    try:
        db_path = _get_db_path()
        os.makedirs(db_path, exist_ok=True)

        _client = chromadb.PersistentClient(
            path=db_path,
            settings=Settings(anonymized_telemetry=False),
        )
        _collection = _client.get_or_create_collection(
            name=os.getenv("CHROMADB_COLLECTION", "textbook_knowledge"),
            metadata={"hnsw:space": "cosine"},
        )
        return _client, _collection
    except Exception as e:
        logger.warning(f"⚠️ ChromaDB 初始化失敗: {e}")
        _client, _collection = None, None
        return None, None


def search_knowledge_chromadb(
    query: str,
    top_k: int = 5,
    write_trace: bool = True,
) -> List[Dict[str, Any]]:
    """從 ChromaDB 檢索知識。找不到 / 集合為空時回 []。"""
    _, collection = init_vector_database()
    if collection is None:
        return []

    # 開啟 trace（跟 GraphRAG 對稱）
    trace_started = False
    if write_trace:
        try:
            from src.chromadb_trace import start_trace, get_current_trace
            if get_current_trace() is None:
                start_trace(question=query, top_k=top_k, route="search_knowledge")
                trace_started = True
        except Exception:
            pass

    vector_count = None
    try:
        try:
            vector_count = collection.count()
            if vector_count == 0:
                logger.info("ChromaDB 集合是空的（尚未灌資料）")
                if trace_started:
                    try:
                        from src.chromadb_trace import record_skip, finalize_trace
                        record_skip("empty_collection")
                        finalize_trace(answer="")
                    except Exception:
                        pass
                return []
        except Exception:
            pass

        t0 = time.perf_counter()
        results = collection.query(
            query_texts=[query],
            n_results=max(1, top_k),
        )
        latency_ms = (time.perf_counter() - t0) * 1000
    except Exception as e:
        logger.error(f"❌ ChromaDB 檢索失敗: {e}")
        if trace_started:
            try:
                from src.chromadb_trace import record_error, finalize_trace
                record_error("chromadb_query", e)
                finalize_trace(answer="")
            except Exception:
                pass
        return []

    items: List[Dict[str, Any]] = []
    docs = (results.get("documents") or [[]])[0]
    metas = (results.get("metadatas") or [[]])[0]
    dists = (results.get("distances") or [[]])[0]

    for i, doc in enumerate(docs):
        items.append({
            "content": doc,
            "metadata": metas[i] if i < len(metas) else {},
            "distance": dists[i] if i < len(dists) else 0,
            "source": "chromadb",
        })

    # 寫 trace（跟 GraphRAG 對稱）
    try:
        from src.chromadb_trace import record_query
        record_query(hits=items, latency_ms=latency_ms, vector_count=vector_count)
    except Exception:
        pass

    if trace_started:
        try:
            from src.chromadb_trace import finalize_trace
            finalize_trace(answer="")
        except Exception:
            pass

    return items


def enhance_prompt_chromadb(
    prompt: str,
    question: str,
    top_k: int = 2,
) -> str:
    """把 ChromaDB 檢索結果拼進 prompt。連帶記 trace（跟 GraphRAG 對稱）。"""
    trace_started = False
    try:
        from src.chromadb_trace import start_trace, get_current_trace
        if get_current_trace() is None:
            start_trace(question=question, top_k=top_k, route="enhance_prompt")
            trace_started = True
    except Exception:
        pass

    try:
        before_chars = len(prompt or "")
        knowledge = search_knowledge_chromadb(question, top_k=top_k, write_trace=False)

        # ✨ 論文對比用：把 injected 那段獨立出來，跟 GraphRAG trace 對稱
        injected_context = ""
        if not knowledge:
            enhanced = prompt
        else:
            block = "\n\n**相關知識參考（ChromaDB RAG）：**\n"
            for i, item in enumerate(knowledge, 1):
                content = item.get("content") or ""
                metadata = item.get("metadata") or {}
                source = metadata.get("source") or ""
                chunk_idx = metadata.get("chunk")
                location = ""
                if source or chunk_idx is not None:
                    location = f" [source={source} chunk={chunk_idx}]"
                block += f"{i}.{location}\n{content}\n\n"
            injected_context = block
            enhanced = prompt + block

        # 補 prompt 前後長度到 trace（保留舊行為，向後相容）
        try:
            from src.chromadb_trace import get_current_trace as _get
            t = _get()
            if t:
                t["prompt"]["before_chars"] = before_chars
                t["prompt"]["after_chars"] = len(enhanced)
                t["prompt"]["injected_chars"] = len(enhanced) - before_chars
        except Exception:
            pass

        # 補 hits + 完整 prompt 文字進 trace（論文對比用）
        try:
            from src.chromadb_trace import record_query
            record_query(
                hits=knowledge,
                latency_ms=0.0,
                vector_count=None,
                prompt_before_chars=before_chars,
                prompt_after_chars=len(enhanced),
                # ✨ 完整 prompt 三段（跟 graphrag_trace 平行）
                prompt_before_text=prompt,
                prompt_after_text=enhanced,
                injected_context_text=injected_context,
            )
        except Exception:
            pass

        return enhanced
    except Exception as e:
        logger.error(f"❌ ChromaDB 增強 prompt 失敗: {e}")
        try:
            from src.chromadb_trace import record_error
            record_error("enhance_prompt", e)
        except Exception:
            pass
        return prompt
    finally:
        if trace_started:
            try:
                from src.chromadb_trace import finalize_trace
                finalize_trace(answer="")
            except Exception:
                pass


def healthcheck() -> Dict[str, Any]:
    """給 API / debug 用：看 ChromaDB 現況（有沒有向量資料）。"""
    try:
        _, collection = init_vector_database()
        if collection is None:
            return {
                "ok": False,
                "reason": "ChromaDB not initialized",
                "vector_count": 0,
            }
        return {
            "ok": True,
            "vector_count": collection.count(),
        }
    except Exception as e:
        return {"ok": False, "reason": str(e), "vector_count": 0}
