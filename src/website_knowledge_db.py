#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
網站知識庫 RAG 系統
使用 ChromaDB 向量資料庫儲存和檢索網站功能資訊，支援 AI 回答網站相關問題
"""

import logging
import os
from typing import List, Dict, Any, Optional
import json

logger = logging.getLogger(__name__)

try:
    import chromadb
    from chromadb.config import Settings
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False
    logger.error("❌ ChromaDB 未安裝，請安裝後再使用：pip install chromadb")
    raise ImportError("ChromaDB 是必需的依賴，請安裝：pip install chromadb")

# ==================== ChromaDB 知識庫管理（唯一儲存方式）====================

def init_chromadb_knowledge_collection():
    """初始化 ChromaDB 網站知識庫集合"""
    if not CHROMADB_AVAILABLE:
        raise RuntimeError("ChromaDB 未安裝，請安裝：pip install chromadb")

    try:
        # 獲取當前文件的絕對路徑
        current_dir = os.path.dirname(os.path.abspath(__file__))
        # 構建向量資料庫的絕對路徑
        db_path = os.path.join(current_dir, "rag_sys", "data", "knowledge_db", "chroma_db")

        # 確保目錄存在
        os.makedirs(db_path, exist_ok=True)

        chroma_client = chromadb.PersistentClient(
            path=db_path,
            settings=Settings(anonymized_telemetry=False)
        )

        # 獲取或創建集合
        collection = chroma_client.get_or_create_collection(
            name="website_knowledge",
            metadata={"hnsw:space": "cosine", "description": "網站功能知識庫"}
        )

        logger.info("✅ ChromaDB 網站知識庫集合初始化成功")
        return chroma_client, collection
    except Exception as e:
        logger.error(f"❌ ChromaDB 知識庫初始化失敗: {e}")
        raise

def save_knowledge_to_chromadb(knowledge_item: Dict[str, Any]) -> str:
    """
    將知識項目儲存到 ChromaDB（向量化）

    Args:
        knowledge_item: 知識項目字典，包含：
            - title: 標題
            - content: 內容
            - category: 分類（如：功能介紹、操作說明、FAQ等）
            - page_path: 相關頁面路徑（可選）

    Returns:
        文檔 ID（用於後續更新或刪除）
    """
    try:
        client, collection = init_chromadb_knowledge_collection()
        if not collection:
            raise RuntimeError("無法初始化 ChromaDB 集合")

        # 構建文檔文本（用於向量化）
        doc_text = f"{knowledge_item.get('title', '')}\n\n{knowledge_item.get('content', '')}"

        # 構建簡化的元數據（只包含必要資訊）
        metadata = {
            "category": knowledge_item.get("category", "general"),
            "page_path": knowledge_item.get("page_path", ""),
            "title": knowledge_item.get("title", "")[:200]  # 限制長度
        }

        # 如果有自定義 ID，使用它；否則基於標題生成唯一 ID
        doc_id = knowledge_item.get("doc_id") or f"doc_{hash(knowledge_item.get('title', 'unknown')) % 1000000}"

        # 檢查是否已存在（避免重複）
        try:
            existing = collection.get(ids=[doc_id])
            if existing['ids']:
                # 已存在，更新它
                collection.update(
                    ids=[doc_id],
                    documents=[doc_text],
                    metadatas=[metadata]
                )
                logger.info(f"✅ 知識項目已更新到 ChromaDB: {knowledge_item.get('title', 'Unknown')}")
            else:
                # 不存在，添加它
                collection.add(
                    documents=[doc_text],
                    metadatas=[metadata],
                    ids=[doc_id]
                )
                logger.info(f"✅ 知識項目已儲存到 ChromaDB: {knowledge_item.get('title', 'Unknown')}")
        except Exception:
            # 如果 get 失敗（可能不存在），直接添加
            collection.add(
                documents=[doc_text],
                metadatas=[metadata],
                ids=[doc_id]
            )
            logger.info(f"✅ 知識項目已儲存到 ChromaDB: {knowledge_item.get('title', 'Unknown')}")

        return doc_id
    except Exception as e:
        logger.error(f"❌ 儲存知識到 ChromaDB 失敗: {e}")
        raise

def search_knowledge_in_chromadb(query: str, n_results: int = 5, category: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    從 ChromaDB 搜索知識（基於向量相似度）

    Args:
        query: 搜索查詢
        n_results: 返回結果數量
        category: 分類篩選（可選），如果指定則只在該分類中搜索

    Returns:
        匹配的知識項目列表，包含完整的知識項目資訊
    """
    try:
        client, collection = init_chromadb_knowledge_collection()
        if not collection:
            return []

        # 構建查詢條件
        where_clause = None
        if category:
            where_clause = {"category": category}

        # 執行向量搜索
        if where_clause:
            results = collection.query(
                query_texts=[query],
                n_results=n_results,
                where=where_clause
            )
        else:
            results = collection.query(
                query_texts=[query],
                n_results=n_results
            )

        # 格式化結果為完整的知識項目格式
        formatted_results = []
        if results['documents'] and len(results['documents'][0]) > 0:
            for i in range(len(results['documents'][0])):
                metadata = results['metadatas'][0][i] if results['metadatas'] else {}
                doc_text = results['documents'][0][i]
                distance = results['distances'][0][i] if results['distances'] else 0.0
                doc_id = results['ids'][0][i] if results['ids'] else None

                # 從文檔文本中提取標題和內容
                # 文檔格式：標題\n\n內容
                lines = doc_text.split('\n\n', 1)
                title = metadata.get('title', lines[0] if lines else '無標題')
                content = lines[1] if len(lines) > 1 else doc_text

                formatted_results.append({
                    "doc_id": doc_id,
                    "title": title,
                    "content": content,
                    "category": metadata.get("category", "general"),
                    "page_path": metadata.get("page_path", ""),
                    "similarity": 1.0 - distance,  # 轉換為相似度分數（0-1，越高越相似）
                    "source": "chromadb"
                })

        logger.info(f"✅ ChromaDB 搜索完成: 找到 {len(formatted_results)} 個結果")
        return formatted_results
    except Exception as e:
        logger.error(f"❌ ChromaDB 搜索失敗: {e}")
        return []

# ==================== 檢索函數 ====================

def retrieve_website_knowledge(query: str, max_results: int = 5, category: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    檢索網站知識（使用 ChromaDB 向量搜索）

    Args:
        query: 搜索查詢
        max_results: 最大結果數量
        category: 分類篩選（可選）

    Returns:
        相關知識項目列表，按相似度排序
    """
    try:
        # 使用 ChromaDB 向量搜索
        results = search_knowledge_in_chromadb(query, n_results=max_results, category=category)

        # 按相似度排序（已由 ChromaDB 排序，但確保順序正確）
        results.sort(key=lambda x: x.get("similarity", 0.0), reverse=True)

        logger.info(f"✅ 知識檢索完成: 找到 {len(results)} 個結果")
        return results
    except Exception as e:
        logger.error(f"❌ 知識檢索失敗: {e}")
        return []
