"""AI 教學對話進度持久化。

背景：
    AI 智能教學對話的 `understanding_level`（0-99 智能分數）原本只存在
    後端 in-memory `learning_sessions` dict 裡，重啟就消失、也不會影響
    知識診斷中心跟學習分析。

這支 module 做的事：
    1. 建表 tutoring_progress（MySQL）
    2. `upsert_tutoring_progress()`：AI 教學對話結束時（達到 99 或每輪都可）呼叫
       → 記錄「使用者 × 題目 × 概念 → 智能分數 + 階段 + 對話輪數」
    3. `get_user_tutoring_records()`：學習分析讀出來，跟 quiz_answers 合併

Schema：
    tutoring_progress
      - id (PK)
      - user_email (INDEX)
      - mongodb_question_id (使用的 test5/exam 題目 ID，可空)
      - question_text_hash (SHA1 前16字，判重)
      - question_preview (前 200 字，方便肉眼看)
      - concept_names (JSON list，本次對話 GraphRAG 命中的概念)
      - key_points (JSON list，題目原始 key-points，跟 concept_names 對應知識點)
      - domain (領域，用來對到「知識診斷中心」的分類，例：'作業系統')
      - smart_score (0-99，最終智能分數)
      - learning_stage (最終階段名稱)
      - conversation_count (對話輪數)
      - is_completed (BOOL，smart_score 是否達到 99)
      - graphrag_usage (JSON，本次 trace 摘要)
      - created_at, updated_at
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def init_tutoring_progress_table():
    """建表（idempotent）。app.py 啟動時呼叫一次。"""
    from accessories import sqldb
    with sqldb.engine.connect() as conn:
        conn.execute(sqldb.text("""
            CREATE TABLE IF NOT EXISTS tutoring_progress (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_email VARCHAR(255) NOT NULL,
                mongodb_question_id VARCHAR(50) DEFAULT NULL,
                question_text_hash VARCHAR(32) NOT NULL,
                question_preview VARCHAR(500) DEFAULT '',
                concept_names JSON,
                key_points JSON,
                domain VARCHAR(100) DEFAULT '未知領域',
                smart_score INT DEFAULT 0,
                learning_stage VARCHAR(50) DEFAULT 'core_concept_confirmation',
                conversation_count INT DEFAULT 0,
                is_completed BOOLEAN DEFAULT FALSE,
                graphrag_usage JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_user_qhash (user_email, question_text_hash),
                INDEX idx_user (user_email),
                INDEX idx_domain (domain),
                INDEX idx_smart_score (smart_score),
                INDEX idx_completed (is_completed),
                INDEX idx_updated (updated_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        conn.commit()
    logger.info("✅ tutoring_progress 資料表 ready")


def _question_hash(question_text: str) -> str:
    q = (question_text or "").strip()
    return hashlib.sha1(q.encode("utf-8", errors="ignore")).hexdigest()[:32]


def upsert_tutoring_progress(
    user_email: str,
    question_text: str,
    smart_score: int,
    learning_stage: str,
    conversation_count: int,
    mongodb_question_id: Optional[str] = None,
    concept_names: Optional[List[str]] = None,
    key_points: Optional[List[str]] = None,
    domain: str = "未知領域",
    graphrag_usage: Optional[Dict[str, Any]] = None,
) -> bool:
    """AI 教學對話每輪結束呼叫；如果同一題 hash 已存在就更新，否則新增。

    只在 smart_score 有進展時才呼叫，避免每次對話都寫 DB。
    """
    from accessories import sqldb

    q_hash = _question_hash(question_text)
    preview = (question_text or "")[:500]
    is_completed = int(smart_score >= 99)

    try:
        with sqldb.engine.connect() as conn:
            conn.execute(sqldb.text("""
                INSERT INTO tutoring_progress
                    (user_email, mongodb_question_id, question_text_hash,
                     question_preview, concept_names, key_points, domain,
                     smart_score, learning_stage, conversation_count,
                     is_completed, graphrag_usage)
                VALUES
                    (:email, :qid, :qhash, :preview, :concepts, :kps, :domain,
                     :score, :stage, :ccnt, :done, :gu)
                ON DUPLICATE KEY UPDATE
                    smart_score = GREATEST(smart_score, VALUES(smart_score)),
                    learning_stage = VALUES(learning_stage),
                    conversation_count = VALUES(conversation_count),
                    is_completed = VALUES(is_completed),
                    concept_names = VALUES(concept_names),
                    key_points = VALUES(key_points),
                    domain = COALESCE(NULLIF(VALUES(domain), '未知領域'), domain),
                    graphrag_usage = VALUES(graphrag_usage)
            """), {
                "email": user_email,
                "qid": mongodb_question_id,
                "qhash": q_hash,
                "preview": preview,
                "concepts": json.dumps(concept_names or [], ensure_ascii=False),
                "kps": json.dumps(key_points or [], ensure_ascii=False),
                "domain": domain,
                "score": int(smart_score),
                "stage": learning_stage,
                "ccnt": int(conversation_count),
                "done": is_completed,
                "gu": json.dumps(graphrag_usage or {}, ensure_ascii=False),
            })
            conn.commit()
        return True
    except Exception as e:
        logger.warning(f"⚠️ upsert_tutoring_progress 失敗: {e}")
        return False


def get_user_tutoring_records(user_email: str) -> List[Dict[str, Any]]:
    """給 learning_analytics 用：把 tutoring_progress 轉成類似 quiz_answers 的
    紀錄格式，方便合併統計。

    每筆會被視為「一次答題」：
      - is_correct = (smart_score >= 85)  ← 85 分以上算「答對」
      - domain / key_points 一起帶
    """
    from sqlalchemy import text
    from accessories import sqldb

    try:
        result = sqldb.session.execute(text("""
            SELECT id, mongodb_question_id, domain, concept_names, key_points,
                   smart_score, learning_stage, conversation_count, is_completed,
                   updated_at
            FROM tutoring_progress
            WHERE user_email = :email
            ORDER BY updated_at DESC
        """), {"email": user_email})
        rows = result.fetchall()
    except Exception as e:
        logger.warning(f"⚠️ 讀 tutoring_progress 失敗: {e}")
        return []

    records = []
    for row in rows:
        try:
            concept_names = json.loads(row.concept_names or "[]")
        except Exception:
            concept_names = []
        try:
            key_points = json.loads(row.key_points or "[]")
        except Exception:
            key_points = []

        records.append({
            "answer_id": f"tutor_{row.id}",  # 加前綴避開跟 quiz_answers 撞
            "source": "tutoring",             # ← 讓分析知道這筆是教學對話來的
            "question_id": row.mongodb_question_id or f"tutor_q_{row.id}",
            "attempt_time": row.updated_at,
            "time_spent": 0,  # 教學對話不記時
            "is_correct": bool(row.smart_score >= 85),
            "score": float(row.smart_score),
            "feedback": {
                "learning_stage": row.learning_stage,
                "conversation_count": row.conversation_count,
                "is_completed": bool(row.is_completed),
            },
            "concept_names": concept_names,
            "primary_concept": concept_names[0] if concept_names else "",
            "key_points": key_points,
            "domain": row.domain or "未知領域",
            "difficulty": "中等",  # 教學對話沒難度概念
        })
    return records
