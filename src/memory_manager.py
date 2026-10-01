#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
記憶管理工具實現 - 使用 Redis 儲存
"""

import logging
import json
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

# Redis 客戶端（將在運行時初始化）
_redis_client = None

def init_redis_client(redis_client):
    """初始化 Redis 客戶端"""
    global _redis_client
    _redis_client = redis_client

def _get_redis():
    """獲取 Redis 客戶端，如果未初始化則嘗試從 accessories 導入"""
    global _redis_client
    if _redis_client is None:
        try:
            from accessories import redis_client
            _redis_client = redis_client
        except ImportError:
            logger.error("無法導入 redis_client，請確保已初始化")
            raise
    return _redis_client

def _get_memory_key(user_id: str) -> str:
    """獲取記憶的 Redis key"""
    return f"memory:{user_id}"

def manage_user_memory(action: str, user_id: str = None) -> str:
    """管理用戶記憶

    Args:
        action: 操作類型，必須是 'view'（查看）、'clear'（清除）或 'stats'（統計）
        user_id: 用戶ID，如果為 None 則從 input_text 中提取

    Returns:
        操作結果字符串
    """
    # 如果 user_id 為 None 或 "default"，嘗試從上下文提取
    if not user_id or user_id == "default":
        import re
        import sys

        # 嘗試從全局變數或參數中獲取 user_id
        # 這是一個臨時解決方案，更好的方式是通過參數傳遞
        frame = sys._getframe(1)
        if 'input_text' in frame.f_locals:
            input_text = frame.f_locals['input_text']
            user_id_match = re.search(r'用戶ID:\s*(line_[^\s\n]+)', input_text)
            if user_id_match:
                user_id = user_id_match.group(1)

        # 如果還是沒有找到，使用 "default"
        if not user_id or user_id == "default":
            logger.warning("無法獲取用戶ID，使用 default，記憶可能無法正確保存")
            user_id = "default"

    if action == 'view':
        return _get_memory_summary(user_id)
    elif action == 'clear':
        return _clear_user_memory(user_id)
    elif action == 'stats':
        return _get_memory_stats()
    else:
        return f"未知操作\n\n支持的操作: view(查看), clear(清除), stats(統計)"

def _get_memory_summary(user_id: str) -> str:
    """獲取用戶記憶摘要"""
    try:
        redis_client = _get_redis()
        memory_key = _get_memory_key(user_id)

        # 從 Redis 獲取記憶列表
        memory_list = redis_client.lrange(memory_key, 0, -1)

        if not memory_list:
            return "對話記憶摘要\n\n無對話記憶\n\n我們可以開始新的對話！"

        # 解析 JSON 格式的記憶
        memory = []
        for msg in memory_list:
            try:
                if isinstance(msg, bytes):
                    msg_str = msg.decode('utf-8')
                else:
                    msg_str = msg
                memory.append(json.loads(msg_str))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # 如果是舊格式（直接是字符串），直接使用
                if isinstance(msg, bytes):
                    memory.append(msg.decode('utf-8'))
                else:
                    memory.append(msg)

        # 返回最近的對話記錄（最多10條，用於完整回答「我剛剛做了什麼」）
        recent_messages = memory[-min(10, len(memory)):]
        summary = "對話記憶摘要\n\n"

        if not recent_messages:
            return "對話記憶摘要\n\n無對話記憶\n\n我們可以開始新的對話！"

        for i, msg in enumerate(recent_messages, 1):
            summary += f"{i}. {msg}\n"

        summary += "\n這是您最近的對話內容，根據這些信息回答用戶的問題。"
        return summary
    except Exception as e:
        logger.error(f"獲取記憶摘要失敗: {e}")
        return f"獲取記憶摘要失敗: {str(e)}"

def _clear_user_memory(user_id: str) -> str:
    """清除用戶記憶"""
    try:
        redis_client = _get_redis()
        memory_key = _get_memory_key(user_id)
        redis_client.delete(memory_key)
        return "記憶已清除\n\n您的對話記憶已經清除，我們可以開始新的對話！"
    except Exception as e:
        logger.error(f"清除記憶失敗: {e}")
        return f"清除記憶失敗: {str(e)}"

def _get_memory_stats() -> str:
    """獲取記憶統計信息"""
    try:
        redis_client = _get_redis()

        # 獲取所有記憶相關的 key
        memory_keys = redis_client.keys("memory:*")
        total_users = len(memory_keys)
        user_memories = {}

        for key in memory_keys:
            if isinstance(key, bytes):
                key_str = key.decode('utf-8')
            else:
                key_str = key

            user_id = key_str.replace("memory:", "")
            memory_count = redis_client.llen(key_str)
            user_memories[user_id] = memory_count

        stats_text = f"記憶統計\n\n總用戶數: {total_users}\n\n各用戶記憶條數:\n"

        if user_memories:
            for uid, count in user_memories.items():
                stats_text += f"• {uid}: {count} 條\n"
        else:
            stats_text += "• 暫無用戶記憶\n"

        return stats_text
    except Exception as e:
        logger.error(f"獲取記憶統計失敗: {e}")
        return f"獲取記憶統計失敗: {str(e)}"

def add_user_message(user_id: str, message: str):
    """添加用戶訊息到記憶"""
    try:
        redis_client = _get_redis()
        memory_key = _get_memory_key(user_id)

        # 將訊息儲存為 JSON 格式
        message_data = f"用戶: {message}"
        message_json = json.dumps(message_data, ensure_ascii=False)

        # 使用 Redis list 的 rpush 添加訊息
        redis_client.rpush(memory_key, message_json)

        # 限制記憶條數（保留最近10條）
        memory_count = redis_client.llen(memory_key)
        if memory_count > 10:
            # 刪除最舊的訊息，保留最新的10條
            redis_client.ltrim(memory_key, -10, -1)

        # 設置過期時間（30天）
        redis_client.expire(memory_key, 30 * 24 * 60 * 60)

    except Exception as e:
        logger.error(f"添加用戶訊息到記憶失敗: {e}")

def add_ai_message(user_id: str, message: str):
    """添加AI回應到記憶"""
    try:
        redis_client = _get_redis()
        memory_key = _get_memory_key(user_id)

        # 將訊息儲存為 JSON 格式
        message_data = f"助手: {message}"
        message_json = json.dumps(message_data, ensure_ascii=False)

        # 使用 Redis list 的 rpush 添加訊息
        redis_client.rpush(memory_key, message_json)

        # 限制記憶條數（保留最近10條）
        memory_count = redis_client.llen(memory_key)
        if memory_count > 10:
            # 刪除最舊的訊息，保留最新的10條
            redis_client.ltrim(memory_key, -10, -1)

        # 設置過期時間（30天）
        redis_client.expire(memory_key, 30 * 24 * 60 * 60)

    except Exception as e:
        logger.error(f"添加AI回應到記憶失敗: {e}")

def get_user_memory(user_id: str) -> List[str]:
    """獲取用戶的完整記憶列表（用於內部使用）"""
    try:
        redis_client = _get_redis()
        memory_key = _get_memory_key(user_id)

        memory_list = redis_client.lrange(memory_key, 0, -1)
        if not memory_list:
            return []

        # 解析 JSON 格式的記憶
        memory = []
        for msg in memory_list:
            try:
                if isinstance(msg, bytes):
                    msg_str = msg.decode('utf-8')
                else:
                    msg_str = msg
                memory.append(json.loads(msg_str))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # 如果是舊格式（直接是字符串），直接使用
                if isinstance(msg, bytes):
                    memory.append(msg.decode('utf-8'))
                else:
                    memory.append(msg)

        return memory
    except Exception as e:
        logger.error(f"獲取用戶記憶失敗: {e}")
        return []

# 為了向後兼容，保留全局變數（但實際使用 Redis）
# 這個變數只在沒有 Redis 的情況下使用（作為 fallback）
_user_memories = {}
