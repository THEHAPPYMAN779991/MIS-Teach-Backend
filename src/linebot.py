#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LINE Bot Blueprint - 只負責接收訊息、調用主代理人、回復訊息
"""

from flask import Blueprint, request, jsonify
import json
import os
import requests
from datetime import datetime
from typing import Dict, Any, Optional
import sys

# LINE Bot 相關導入
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest,
    PushMessageRequest, TextMessage, FlexMessage, FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent

# 本地模組導入
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, backend_dir)
from tool.api_keys import get_api_key

# 創建 Blueprint
linebot_bp = Blueprint('linebot', __name__)

# ===== 配置 =====
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')

# 主代理人配置 - 使用現有的 web_ai_assistant
MAIN_AGENT_API_URL = "http://localhost:5000/web-ai/chat"  # 現有的主代理人端點

# 初始化 LINE Bot
_line_bot_configured = bool(LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET)


class _DisabledWebhookHandler:
    """Allows callback decorators to load while LINE integration is disabled."""

    def add(self, *args, **kwargs):
        def decorator(func):
            return func
        return decorator

    def handle(self, *args, **kwargs):
        raise RuntimeError('LINE Bot is not configured')


configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN) if _line_bot_configured else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if _line_bot_configured else _DisabledWebhookHandler()
line_bot_api = MessagingApi(ApiClient(configuration)) if configuration else None


@linebot_bp.before_request
def require_line_bot_configuration():
    """Keep the optional LINE integration from blocking the whole Flask app."""
    if request.method == 'OPTIONS' or _line_bot_configured:
        return None
    return jsonify({
        'error': 'LINE Bot is not configured',
        'message': (
            'Set LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET in local api.env '
            'or the deployment environment before using LINE endpoints.'
        ),
    }), 503

# ===== 全局變數 =====
# 移除 user_quiz_data，現在使用主代理人的記憶管理

# ===== Line 綁定功能 =====
import qrcode
import io
import base64
import redis
import random
from accessories import redis_client, sqldb
from sqlalchemy import text

@linebot_bp.route('/generate-qr', methods=['POST', 'OPTIONS'])
def generate_line_qr():
    """生成 Line Bot 綁定 QR Code"""
    if request.method == 'OPTIONS':
        return '', 204

    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return jsonify({'token': None, 'message': '未提供認證 token'}), 401

    token = auth_header.split(" ")[1]
    from src.api import get_user_info
    student_email = get_user_info(token, 'email')
    data = request.get_json()
    binding_token = data.get('bindingToken')

    if not binding_token:
        return jsonify({'token': None, 'message': '缺少綁定 token'}), 400

    try:
        # 使用正確的加好友連結生成 QR Code
        line_bot_url = "https://lin.ee/rG5sXkM"  # 正確的加好友連結


        # 生成 QR Code
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(line_bot_url)
        qr.make(fit=True)

        # 創建 QR Code 圖片
        img = qr.make_image(fill_color="black", back_color="white")

        # 轉換為 base64
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        img_str = base64.b64encode(buffer.getvalue()).decode()


        # 儲存綁定 token 到 Redis (3分鐘過期)
        # 同時儲存 email 和綁定 token 的映射，用於 FollowEvent 自動綁定
        redis_client.setex(f"line_binding:{binding_token}", 180, student_email)
        # 儲存 email -> binding_token 的映射，用於 FollowEvent 查詢
        redis_client.setex(f"line_pending_binding:{student_email}", 180, binding_token)


        from accessories import refresh_token
        refreshed_token = refresh_token(token)
        return jsonify({
            'token': refreshed_token,
            'qrCodeUrl': f"data:image/png;base64,{img_str}",
            'bindingToken': binding_token
        })

    except Exception as e:
        print(f"❌ 生成 QR Code 失敗: {e}")
        return jsonify({'token': None, 'message': f'生成 QR Code 失敗: {str(e)}'}), 500

@linebot_bp.route('/check-binding', methods=['POST', 'OPTIONS'])
def check_line_binding():
    """檢查 Line 綁定狀態"""
    if request.method == 'OPTIONS':
        return '', 204

    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return jsonify({'token': None, 'message': '未提供認證 token'}), 401

    token = auth_header.split(" ")[1]
    from src.api import get_user_info
    student_email = get_user_info(token, 'email')
    data = request.get_json()
    binding_token = data.get('bindingToken')

    if not binding_token:
        return jsonify({'token': None, 'message': '缺少綁定 token'}), 400

    # 檢查 Redis 中是否有綁定成功的記錄
    binding_key = f"line_binding_success:{binding_token}"
    line_user_id = redis_client.get(binding_key)

    if line_user_id:
        # 綁定成功，更新用戶資料
        line_user_id = line_user_id.decode('utf-8')

        # 更新 MongoDB 中的用戶資料
        from accessories import mongo
        result = mongo.db.user.update_one(
            {"email": student_email},
            {"$set": {"lineId": line_user_id}}
        )

        if result.matched_count == 0:
            print(f"❌ 找不到用戶: {student_email}")
            return jsonify({'token': None, 'message': '找不到用戶資料'}), 404


        # 清除綁定記錄
        redis_client.delete(binding_key)
        redis_client.delete(f"line_binding:{binding_token}")

        from accessories import refresh_token
        refreshed_token = refresh_token(token)
        return jsonify({
            'token': refreshed_token,
            'bound': True,
            'lineId': line_user_id
        })
    else:
        from accessories import refresh_token
        refreshed_token = refresh_token(token)
        return jsonify({
            'token': refreshed_token,
            'bound': False
        })

# ===== 主代理人調用 =====
def call_main_agent(user_message: str, user_id: str) -> str:
    """調用現有的主代理人系統 (web_ai_assistant)"""
    try:
        request_data = {
            "message": user_message,
            "user_id": f"line_{user_id}",  # 加上 line_ 前綴區分來源
            "platform": "linebot",  # 標識為 LINE Bot 平台
            "conversation_id": f"line_{user_id}",  # 添加對話ID，用於記憶管理
            "maintain_context": True  # 標識需要保持對話上下文
        }
        # 調用現有的主代理人 API
        response = requests.post(
            MAIN_AGENT_API_URL,
            json=request_data,
            headers={"Content-Type": "application/json"},
            timeout=30
        )

        if response.status_code == 200:
            result = response.json()

            if result.get('success'):
                message = result.get("content", result.get("message", "主代理人回應格式錯誤"))
                return message
            else:
                error_msg = result.get("error", "未知錯誤")
                return f"抱歉，主代理人處理失敗：{error_msg}"
        else:
            print(f"❌ 主代理人 API 錯誤: {response.status_code} - {response.text}")
            return f"抱歉，主代理人系統暫時無法使用。錯誤代碼：{response.status_code}"

    except requests.exceptions.RequestException as e:
        print(f"❌ 調用主代理人 API 失敗: {e}")
        return "抱歉，無法連接到主代理人系統，請稍後再試"
    except Exception as e:
        print(f"❌ 調用主代理人失敗: {e}")
        return f"抱歉，主代理人系統暫時無法使用。錯誤：{str(e)}"

# ===== 消息處理 =====
def reply_text(reply_token: str, text: str, user_id: str = None):
    """發送文字回覆"""
    try:
        # 檢查消息是否為空
        if not text or not text.strip():
            text = "抱歉，系統暫時無法回應，請稍後再試。"

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )

        # 如果提供了 user_id，將助手的回應存儲到記憶中（用於批改答案時提取題目）
        if user_id:
            try:
                from src.memory_manager import add_ai_message
                user_memory_key = f"line_{user_id}"
                add_ai_message(user_memory_key, text)
            except Exception as e:
                print(f"⚠️ 存儲助手回應到記憶失敗: {e}")
    except Exception as e:
        print(f"❌ 發送消息失敗: {e}")

def send_thinking_message(reply_token: str):
    """發送思考中提示訊息"""
    try:
        thinking_text = "小幫手正在思考中，請稍候..."
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=thinking_text)]
            )
        )
    except Exception as e:
        print(f"❌ 發送思考中提示失敗: {e}")

def push_text_message(user_id: str, text: str):
    """發送推播訊息（用於後續回應）"""
    try:
        # 檢查消息是否為空
        if not text or not text.strip():
            text = "抱歉，系統暫時無法回應，請稍後再試。"

        line_bot_api.push_message(
            PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=text)]
            )
        )

        # 將助手的回應存儲到記憶中（重要：用於後續的題目提取）
        try:
            from src.memory_manager import add_ai_message
            user_memory_key = f"line_{user_id}"
            add_ai_message(user_memory_key, text)
        except Exception as e:
            print(f"⚠️ 存儲助手回應到記憶失敗: {e}")
    except Exception as e:
        print(f"❌ 發送推播消息失敗: {e}")

def send_error_message(reply_token: str):
    """發送錯誤訊息"""
    try:
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text="抱歉，消息發送失敗，請稍後再試。")]
            )
        )
    except Exception as fallback_error:
        print(f"❌ 錯誤消息發送也失敗: {fallback_error}")

def handle_binding_command(user_id: str, binding_token: str, reply_token: str):
    """處理綁定指令"""
    try:

        # 檢查綁定 token 是否存在
        binding_key = f"line_binding:{binding_token}"
        user_email = redis_client.get(binding_key)


        if user_email:
            user_email = user_email.decode('utf-8')

            # 記錄綁定成功
            success_key = f"line_binding_success:{binding_token}"
            redis_client.setex(success_key, 180, user_id)

            # 更新 MongoDB 中的用戶資料
            from accessories import mongo
            result = mongo.db.user.update_one(
                {"email": user_email},
                {"$set": {"lineId": user_id}}
            )

            if result.matched_count > 0:
                # 清除相關記錄
                redis_client.delete(binding_key)
                redis_client.delete(f"line_pending_binding:{user_email}")

                # 獲取用戶名稱
                user = mongo.db.user.find_one({"email": user_email})
                user_name = user.get('name', '用戶') if user else '用戶'

                # 發送確認訊息給用戶
                success_message = f"""綁定成功！

您已成功綁定 MIS 教學助手
用戶姓名：{user_name}
綁定帳號：{user_email}

現在您可以使用所有功能：
• 問我任何資管相關問題
• 生成隨機測驗題目
• 獲得學習建議
• 查看學習分析
• 設定學習目標
• 管理行事曆

直接發送訊息開始使用吧！"""
                reply_text(reply_token, success_message)
            else:
                reply_text(reply_token, "綁定失敗，找不到用戶資料，請聯繫客服。")

        else:

            # 列出所有相關的 Redis keys 進行調試
            try:
                all_keys = redis_client.keys("line_binding:*")
            except Exception as e:
                print(f"🔍 無法列出 Redis keys: {e}")

            reply_text(reply_token, f"綁定失敗，綁定碼無效或已過期。\n\n請確認：\n1. 綁定碼是否正確複製\n2. 是否在 3 分鐘內完成綁定\n3. 是否重新生成了 QR Code\n\n當前綁定碼：{binding_token}")

    except Exception as e:
        print(f"❌ 處理綁定指令失敗: {e}")
        reply_text(reply_token, "綁定過程中發生錯誤，請稍後再試。")

def handle_test_binding(user_id: str, reply_token: str):
    """處理綁定測試指令"""
    try:

        # 檢查用戶是否已綁定
        from accessories import mongo
        user = mongo.db.user.find_one({"lineId": user_id})

        if user:
            # 用戶已綁定，返回完整用戶資料
            user_name = user.get('name', '未知')
            user_email = user.get('email', '未知')
            user_school = user.get('school', '未知')
            user_birthday = user.get('birthday', '未知')

            # 構建詳細的用戶資料訊息
            test_message = f"""👤 您的個人資料

📝 姓名：{user_name}
📧 帳號：{user_email}
🏫 學校：{user_school}"""

            if user_birthday and user_birthday != '未知':
                test_message += f"\n🎂 生日：{user_birthday}"

            test_message += f"\n🆔 LINE ID：{user_id[:20]}..."  # 只顯示前20個字符

            test_message += f"""

✅ 綁定狀態：已綁定
💡 您可以使用所有功能：
• 問我任何資管相關問題
• 生成隨機測驗題目
• 獲得學習建議
• 查看學習分析
• 設定學習目標
• 管理行事曆"""
        else:
            # 用戶未綁定
            test_message = """❌ 您尚未綁定 MIS 教學助手

📋 綁定步驟：
1. 在網站設定頁面生成 QR Code
2. 掃描 QR Code 後點擊一鍵綁定按鈕
   或複製顯示的綁定碼
3. 直接發送綁定碼（以 bind_ 開頭）

💡 例如：bind_1757907057155_e47dt5lib"""

        reply_text(reply_token, test_message)

    except Exception as e:
        print(f"❌ 測試綁定狀態失敗: {e}")
        reply_text(reply_token, "測試過程中發生錯誤，請稍後再試。")

def handle_message(event: MessageEvent):
    """處理用戶文字消息"""
    user_message = event.message.text.strip()
    user_id = event.source.user_id

    # 處理圖文選單指令（可能帶有 @ 前綴）
    # 先移除 @ 前綴，然後進行匹配
    clean_message = user_message.lstrip('@').strip()

    # 檢查是否為綁定碼格式（以 bind_ 開頭）
    if user_message.startswith('bind_'):
        binding_token = user_message.strip()
        handle_binding_command(user_id, binding_token, event.reply_token)
        return

    # 檢查是否為測試指令或查詢用戶資料
    # 支持多種問法：「我是誰」、「我名稱是誰」、「我的資料」、「查詢我的資料」等
    user_query_patterns = [
        '我是誰', '我名稱是誰', '我的名稱', '我的名字', '我叫什麼',
        '我的資料', '查詢我的資料', '我的資訊', '我的信息',
        '測試綁定', 'test', '檢查綁定', '綁定狀態'
    ]

    # 檢查是否匹配任何查詢模式
    if any(pattern in user_message for pattern in user_query_patterns):
        handle_test_binding(user_id, event.reply_token)
        return

    # 處理圖文選單功能
    # 支持完全匹配和包含匹配（例如「最新消息/考試資訊」或「@最新消息」）
    if clean_message == "學習分析" or clean_message.startswith("學習分析"):
        handle_learning_analysis(user_id, event.reply_token)
        return
    elif clean_message == "目標設定" or clean_message.startswith("目標設定"):
        handle_goal_setting(user_id, event.reply_token)
        return
    elif clean_message == "最新消息" or clean_message.startswith("最新消息"):
        handle_news(user_id, event.reply_token)
        return
    elif clean_message == "行事曆" or clean_message.startswith("行事曆"):
        handle_calendar(user_id, event.reply_token)
        return
    elif clean_message == "隨機知識" or clean_message.startswith("隨機知識"):
        handle_random_knowledge(user_id, event.reply_token)
        return

    # 所有圖文選單功能都通過主代理人處理

    # 所有其他訊息都交給主代理人處理，包括測驗答案
    # 主代理人會自動維護對話上下文和記憶

    # 檢查是否為測驗答案（根據前一次對話判斷）
    def is_likely_quiz_answer(message: str, user_id: str) -> bool:
        """根據前一次對話智能判斷是否為測驗答案"""
        try:
            from src.memory_manager import get_user_memory
            user_memory_key = f"line_{user_id}"

            # 從 Redis 獲取對話記憶
            memory = get_user_memory(user_memory_key)
            if not memory:
                return False

            # 獲取最近的對話記錄
            recent_messages = memory[-3:]  # 最近3條

            # 檢查前一次對話是否包含測驗題目
            def has_quiz_context(messages: list) -> bool:
                """檢查對話中是否包含測驗題目"""
                for msg in messages:
                    msg_lower = msg.lower()
                    quiz_indicators = [
                        "測驗", "題目", "選擇題", "問題", "a)", "b)", "c)", "d)",
                        "quiz", "question", "test", "選擇", "答案", "請輸入您的答案"
                    ]
                    if any(indicator in msg_lower for indicator in quiz_indicators):
                        return True
                return False

            # 如果前一次對話包含測驗題目，且當前輸入是簡短答案，則可能是測驗答案
            if has_quiz_context(recent_messages):
                message_clean = message.strip().upper()

                # 檢查是否為可能的測驗答案格式
                if message_clean in ["A", "B", "C", "D"]:
                    return True

                if any(pattern in message_clean for pattern in ["(A)", "(B)", "(C)", "(D)", "A)", "B)", "C)", "D)"]):
                    return True

                # 簡短答案但排除常見單詞
                if len(message_clean) <= 3 and any(option in message_clean for option in ["A", "B", "C", "D"]):
                    common_words = ["HI", "HEY", "YES", "NO", "OK", "BYE", "THX", "THANKS"]
                    if message_clean not in common_words:
                        return True

            return False

        except Exception as e:
            print(f"❌ 檢查測驗答案失敗：{e}")
            return False

    if is_likely_quiz_answer(user_message, user_id):

        # 從記憶管理器中獲取最近的對話上下文
        try:
            from src.memory_manager import get_user_memory
            user_memory_key = f"line_{user_id}"

            # 從 Redis 獲取對話記憶
            memory = get_user_memory(user_memory_key)
            if memory:
                # 獲取最近的對話記錄
                recent_messages = memory[-5:]  # 最近5條
                context = "\n".join(recent_messages)

                # 從上下文中提取題目
                import re
                question = ""

                # 尋找題目：只查找最近的一條包含選項的訊息（即當前題目）
                # 從最近的訊息開始反向查找，找到第一條包含選項的訊息就停止
                for msg in reversed(recent_messages):
                    # 跳過用戶的答案（通常是簡短的 A、B、C、D 或簡短文字）
                    msg_stripped = msg.strip()
                    if len(msg_stripped) <= 5 and msg_stripped.upper() in ['A', 'B', 'C', 'D', 'A.', 'B.', 'C.', 'D.']:
                        continue

                    # 檢查是否包含完整的選擇題格式（A. B. C. D.）
                    if "A." in msg and "B." in msg and "C." in msg and "D." in msg:
                        # 提取完整的題目（包括問題和選項，但移除提示文字）
                        # 移除提示文字和表情符號
                        question = msg
                        question = re.sub(r'💡\s*請輸入您的答案.*', '', question, flags=re.IGNORECASE | re.DOTALL)
                        question = re.sub(r'請輸入您的答案.*', '', question, flags=re.IGNORECASE | re.DOTALL)
                        question = question.strip()

                        # 確保題目包含選項（至少包含 A. 和 B.）
                        if "A." in question and "B." in question:
                            break
                        else:
                            question = ""  # 如果移除後沒有選項，重置

                # 如果沒有找到完整題目，使用最近的包含選項的訊息（但不是用戶答案）
                if not question:
                    for msg in reversed(recent_messages):
                        # 跳過用戶的答案
                        msg_stripped = msg.strip()
                        if len(msg_stripped) <= 5 and msg_stripped.upper() in ['A', 'B', 'C', 'D', 'A.', 'B.', 'C.', 'D.']:
                            continue

                        if any(opt in msg for opt in ["A.", "B.", "C.", "D."]):
                            question = msg
                            # 移除表情符號和提示文字
                            question = re.sub(r'💡\s*請輸入您的答案.*', '', question, flags=re.IGNORECASE | re.DOTALL)
                            question = re.sub(r'請輸入您的答案.*', '', question, flags=re.IGNORECASE | re.DOTALL)
                            question = question.strip()

                            # 確保題目包含選項
                            if "A." in question or "B." in question:
                                break
                            else:
                                question = ""

                # 如果還是沒有找到題目，嘗試從更早的對話中查找（最多查找最近10條）
                if not question and len(memory) > 5:
                    extended_messages = memory[-10:]
                    for msg in reversed(extended_messages):
                        # 跳過用戶的答案
                        msg_stripped = msg.strip()
                        if len(msg_stripped) <= 5 and msg_stripped.upper() in ['A', 'B', 'C', 'D', 'A.', 'B.', 'C.', 'D.']:
                            continue

                        if "A." in msg and "B." in msg and "C." in msg and "D." in msg:
                            question = msg
                            question = re.sub(r'💡\s*請輸入您的答案.*', '', question, flags=re.IGNORECASE | re.DOTALL)
                            question = re.sub(r'請輸入您的答案.*', '', question, flags=re.IGNORECASE | re.DOTALL)
                            question = question.strip()

                            # 確保題目包含選項
                            if "A." in question and "B." in question:
                                break
                            else:
                                question = ""

                # 調試輸出
                if not question:
                    print(f"⚠️ 題目提取失敗 - 用戶答案: {user_message}")
                    print(f"⚠️ 最近5條訊息: {recent_messages}")

                # 直接調用批改工具，不通過主代理人，避免輸出過長
                # 正確答案暫時為空，讓 AI 根據題目和用戶答案判斷
                if question:
                    response = grade_answer(user_message, "", question)
                    reply_text(event.reply_token, response, user_id)
                else:
                    # 如果還是找不到題目，嘗試使用完整的上下文
                    print(f"⚠️ 嘗試使用完整上下文進行批改")
                    full_context = "\n".join(recent_messages[-3:])  # 使用最近3條
                    # 如果上下文包含選項，直接使用
                    if "A." in full_context or "B." in full_context:
                        response = grade_answer(user_message, "", full_context)
                        reply_text(event.reply_token, response, user_id)
                    else:
                        reply_text(event.reply_token, "❌ 無法識別題目內容，請重新發送題目後再回答。", user_id)
                return
            else:
                print(f"❌ 沒有找到對話記憶，按一般訊息處理")
        except Exception as e:
            print(f"❌ 獲取記憶失敗：{e}，按一般訊息處理")

    # 處理特殊指令
    if user_message in ["@每日測驗"]:
        # 發送測驗選擇輪盤
        try:
            carousel = create_quiz_selection_carousel()

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[carousel]
                )
            )
            return
        except Exception as e:
            # 如果輪盤發送失敗，回退到文字回應
            response = "🎯 開始測驗！\n\n請選擇知識點：\n• 基本計概\n• 數位邏輯\n• 作業系統\n• 程式語言\n• 資料結構\n• 網路通訊\n• 資料庫\n• AI與機器學習\n• 資訊安全\n• 雲端運算\n• MIS系統\n• 軟體工程\n• 隨機\n\n系統會自動生成隨機題型（選擇題或知識問答題）"
            reply_text(event.reply_token, response)
            return

    # 處理測驗知識點選擇指令
    if user_message.startswith("@測驗 "):
        topic = user_message.replace("@測驗 ", "").strip()

        # 直接調用主代理人生成隨機測驗（不顯示答案）
        if topic == "隨機":
            prompt = "請生成一道隨機測驗題目，題型隨機（選擇題或知識問答題），適合 LINE Bot 顯示。要求：1. 直接生成題目內容，不要有任何前綴說明如「好的，這是一道...」或「---」等 2. 只顯示題目和選項，絕對不要顯示正確答案 3. 如果是選擇題，提供4個選項（A、B、C、D） 4. 如果是知識問答題，只顯示問題 5. 內容要專注於資管相關的計算機科學知識 6. 包含適當的表情符號和格式 7. 重要：不要顯示「正確答案：」或任何答案相關信息"
        else:
            prompt = f"請生成一道關於「{topic}」的隨機測驗題目，題型隨機（選擇題或知識問答題），適合 LINE Bot 顯示。要求：1. 直接生成題目內容，不要有任何前綴說明如「好的，這是一道...」或「---」等 2. 只顯示題目和選項，絕對不要顯示正確答案 3. 如果是選擇題，提供4個選項（A、B、C、D） 4. 如果是知識問答題，只顯示問題 5. 內容要專注於資管相關的計算機科學知識 6. 包含適當的表情符號和格式 7. 重要：不要顯示「正確答案：」或任何答案相關信息"

        # 發送思考中提示
        send_thinking_message(event.reply_token)

        response = call_main_agent(prompt, user_id)

        # 添加答題說明
        if "選擇題" in response or "A)" in response or "B)" in response:
            response += "\n\n💡 請輸入您的答案（A、B、C 或 D）："
        else:
            response += "\n\n💡 請輸入您的答案："
        push_text_message(user_id, response)
        return

    # 直接調用現有的主代理人處理所有其他訊息
    # 發送思考中提示
    send_thinking_message(event.reply_token)

    response = call_main_agent(user_message, user_id)
    # 使用推播訊息發送最終回應（因為 reply_token 已經用過）
    push_text_message(user_id, response)

def handle_postback(event: PostbackEvent):
    """處理用戶按鈕點擊事件"""
    data = event.postback.data
    user_id = event.source.user_id

    # 檢查是否為綁定操作
    if data.startswith("action=bind"):
        # 解析 token
        import urllib.parse
        params = urllib.parse.parse_qs(data)
        binding_token = params.get('token', [None])[0] if 'token' in params else None

        if not binding_token:
            # 嘗試從 Redis 獲取
            binding_token_key = f"line_user_binding:{user_id}"
            binding_token = redis_client.get(binding_token_key)
            if binding_token:
                binding_token = binding_token.decode('utf-8')

        if binding_token:
            # 執行綁定
            try:
                # 檢查綁定 token 是否存在
                binding_key = f"line_binding:{binding_token}"
                user_email = redis_client.get(binding_key)

                if user_email:
                    user_email = user_email.decode('utf-8')

                    # 記錄綁定成功
                    success_key = f"line_binding_success:{binding_token}"
                    redis_client.setex(success_key, 180, user_id)

                    # 更新 MongoDB 中的用戶資料
                    from accessories import mongo
                    result = mongo.db.user.update_one(
                        {"email": user_email},
                        {"$set": {"lineId": user_id}}
                    )

                    if result.matched_count > 0:
                        # 清除相關記錄
                        redis_client.delete(binding_key)
                        redis_client.delete(success_key)
                        redis_client.delete(f"line_user_binding:{user_id}")
                        redis_client.delete(f"line_pending_binding:{user_email}")

                        # 發送成功訊息
                        success_message = """🎉 綁定成功！

✅ 您已成功綁定 MIS 教學助手
📧 綁定帳號：{email}

💡 現在您可以使用所有功能：
• 問我任何資管相關問題
• 生成隨機測驗題目
• 獲得學習建議
• 查看學習分析
• 設定學習目標
• 管理行事曆

直接發送訊息開始使用吧！""".format(email=user_email)
                        reply_text(event.reply_token, success_message)
                    else:
                        reply_text(event.reply_token, "綁定失敗，找不到用戶資料，請聯繫客服。")
                else:
                    reply_text(event.reply_token, "綁定失敗，綁定碼無效或已過期。請在網站上重新生成 QR Code。")
            except Exception as e:
                print(f"❌ 處理一鍵綁定失敗: {e}")
                reply_text(event.reply_token, "綁定過程中發生錯誤，請稍後再試。")
        else:
            reply_text(event.reply_token, "找不到綁定資訊，請在網站上重新生成 QR Code。")
    else:
        # 其他按鈕點擊事件交給主代理人處理
        user_message = f"按鈕點擊: {data}"
        response = call_main_agent(user_message, user_id)
        reply_text(event.reply_token, response)

# ===== LINE Bot 事件處理 =====
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message_event(event):
    """LINE Bot 文字消息事件處理"""
    handle_message(event)

@handler.add(PostbackEvent)
def handle_postback_event(event):
    """LINE Bot 按鈕點擊事件處理"""
    handle_postback(event)

# 添加 Follow 事件處理器
from linebot.v3.webhooks import FollowEvent

@handler.add(FollowEvent)
def handle_follow_event(event):
    """處理用戶加好友事件 - 支援自動綁定"""
    try:
        user_id = event.source.user_id
        print(f"🔔 [FollowEvent] 收到加好友事件，user_id: {user_id}")

        # 先檢查是否有待綁定記錄（優先處理待綁定狀態）
        # 查找是否有待綁定的記錄（通過掃描 QR Code 產生的記錄）
        pending_bindings = []
        try:
            # 獲取所有待綁定記錄，按創建時間排序（最接近當前的優先）
            all_keys = redis_client.keys("line_pending_binding:*")
            print(f"🔍 [FollowEvent] 找到 {len(all_keys)} 個待綁定記錄")

            for key in all_keys:
                key_str = key.decode('utf-8') if isinstance(key, bytes) else key
                binding_token = redis_client.get(key)
                if binding_token:
                    binding_token = binding_token.decode('utf-8') if isinstance(binding_token, bytes) else binding_token
                    # 檢查這個綁定 token 是否仍然有效
                    email_key = redis_client.get(f"line_binding:{binding_token}")
                    if email_key:
                        email_key = email_key.decode('utf-8') if isinstance(email_key, bytes) else email_key
                        # 獲取 TTL 來判斷優先級（TTL 越大表示越新）
                        ttl = redis_client.ttl(f"line_binding:{binding_token}")
                        print(f"  ✅ 找到有效綁定記錄: {key_str} -> token={binding_token}, email={email_key}, ttl={ttl}")
                        pending_bindings.append({
                            'token': binding_token,
                            'email': email_key,
                            'ttl': ttl  # 用於排序，TTL 越大表示越新
                        })
                    else:
                        print(f"  ⚠️ 綁定 token 無效或已過期: {binding_token}")
                else:
                    print(f"  ⚠️ 無法獲取綁定 token: {key_str}")

            # 按照 TTL 降序排序（最新的在前）
            pending_bindings.sort(key=lambda x: x['ttl'], reverse=True)
            print(f"📊 [FollowEvent] 有效待綁定記錄數: {len(pending_bindings)}")
        except Exception as e:
            print(f"❌ [FollowEvent] 查詢待綁定記錄時發生錯誤: {e}")
            import traceback
            traceback.print_exc()

        # 檢查用戶是否已經綁定
        from accessories import mongo
        user = mongo.db.user.find_one({"lineId": user_id})

        # 如果有待綁定記錄，優先處理綁定（即使已綁定也允許重新綁定）
        if pending_bindings:
            binding_info = pending_bindings[0]
            binding_token = binding_info['token']
            email = binding_info['email']


            # 儲存 user_id -> binding_token 的映射，供 Postback 使用
            redis_client.setex(f"line_user_binding:{user_id}", 180, binding_token)

            # 發送帶有綁定按鈕的訊息
            from linebot.v3.messaging import FlexMessage, FlexBubble, FlexBox, FlexText, FlexButton, PostbackAction

            bubble = FlexBubble(
                body=FlexBox(
                    layout="vertical",
                    contents=[
                        FlexText(
                            text="🎉 歡迎使用 MIS 教學助手！",
                            weight="bold",
                            size="xl",
                            color="#1DB446"
                        ),
                        FlexText(
                            text="檢測到您正在進行帳號綁定",
                            size="sm",
                            color="#666666",
                            margin="md"
                        ),
                        FlexText(
                            text="點擊下方按鈕即可完成綁定，無需手動輸入綁定碼！",
                            size="sm",
                            color="#666666",
                            margin="md",
                            wrap=True
                        )
                    ]
                ),
                footer=FlexBox(
                    layout="vertical",
                    contents=[
                        FlexButton(
                            style="primary",
                            color="#1DB446",
                            height="sm",
                            action=PostbackAction(
                                label="✅ 一鍵綁定",
                                data=f"action=bind&token={binding_token}",
                                display_text="完成綁定"
                            )
                        ),
                        FlexText(
                            text="或直接發送綁定碼：" + binding_token,
                            size="xs",
                            color="#999999",
                            margin="md",
                            wrap=True
                        )
                    ]
                )
            )

            flex_message = FlexMessage(
                alt_text="歡迎訊息",
                contents=bubble
            )

            try:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[flex_message]
                    )
                )
                print(f"✅ [FollowEvent] 成功發送綁定按鈕訊息給 user_id: {user_id}")
                return
            except Exception as e:
                print(f"❌ [FollowEvent] 發送 Flex 訊息失敗，回退到文字訊息: {e}")
                import traceback
                traceback.print_exc()
                # 回退到文字訊息
                welcome_message = f"""🎉 歡迎使用 MIS 教學助手！

✅ 檢測到您的綁定請求！

📋 請選擇以下方式完成綁定：

方式一：直接發送綁定碼
{binding_token}

方式二：在網站設定頁面重新生成 QR Code

💡 綁定成功後即可使用所有功能！"""
                reply_text(event.reply_token, welcome_message)
                return

        # 如果沒有待綁定記錄，檢查是否已綁定
        if user:
            # 用戶已綁定
            welcome_message = f"""🎉 歡迎回來，{user.get('name', '用戶')}！

✅ 您已經成功綁定 MIS 教學助手
📧 綁定帳號：{user.get('email', '未知')}

💡 現在您可以使用所有功能：
• 問我任何資管相關問題
• 生成隨機測驗題目
• 獲得學習建議

直接發送訊息開始使用吧！"""
            reply_text(event.reply_token, welcome_message)
        else:
            # 用戶未綁定且沒有待綁定記錄，使用傳統方式
            redis_client.setex(f"line_user:{user_id}", 3600, "pending_binding")

            welcome_message = """🎉 歡迎使用 MIS 教學助手！

📋 綁定步驟：
1. 在網站設定頁面生成 QR Code
2. 掃描 QR Code 後點擊一鍵綁定按鈕
   或複製綁定碼並直接發送（以 bind_ 開頭）

💡 例如：bind_1757907057155_e47dt5lib

🔧 如果沒有綁定碼，請先在網站上生成 QR Code"""
            reply_text(event.reply_token, welcome_message)

    except Exception as e:
        print(f"❌ 處理加好友事件失敗: {e}")
        try:
            reply_text(event.reply_token, "🎉 歡迎使用 MIS 教學助手！\n\n請在網站上生成 QR Code 完成綁定。")
        except:
            pass

# 添加 Unfollow 事件處理器
from linebot.v3.webhooks import UnfollowEvent

@handler.add(UnfollowEvent)
def handle_unfollow_event(event):
    """處理用戶取消好友事件"""
    try:
        user_id = event.source.user_id

        # 清除相關記錄
        redis_client.delete(f"line_user:{user_id}")

    except Exception as e:
        print(f"❌ 處理取消好友事件失敗: {e}")

# ===== Blueprint 路由 =====
@linebot_bp.route("/webhook", methods=['POST'])
def webhook():
    """LINE Bot Webhook 回調"""
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)

    try:
        # 解析事件類型以便日誌記錄
        try:
            import json
            events_data = json.loads(body)
            events = events_data.get('events', [])
            for event in events:
                event_type = event.get('type', 'unknown')
                print(f"📨 [Webhook] 收到事件: {event_type}")
                if event_type == 'follow':
                    user_id = event.get('source', {}).get('userId', 'unknown')
                    print(f"   - FollowEvent user_id: {user_id}")
        except Exception as parse_error:
            print(f"⚠️ [Webhook] 解析事件失敗（繼續處理）: {parse_error}")

        handler.handle(body, signature)
    except InvalidSignatureError:
        print(f"❌ [Webhook] Invalid signature")
        return jsonify({'error': 'Invalid signature'}), 400
    except Exception as e:
        print(f"❌ [Webhook] 處理事件時發生錯誤: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Internal server error'}), 500

    return jsonify({'status': 'OK'})

# ==================== LINE Bot 純邏輯函數 ====================

def generate_quiz_question(requirements: str) -> str:
    """生成測驗題目的純邏輯 - 調用 Gemini API"""
    try:
        from accessories import init_ai

        # 預設使用 Ollama
        llm = init_ai(ai_type='ollama')

        # 構建提示詞
        prompt = f"""請根據以下需求生成一道測驗題目：

需求：{requirements}

要求：
1. 直接生成題目內容，不要有任何前綴說明如「好的，這是一道...」或「---」等
2. 如果是選擇題，請提供 4 個選項，並標記正確答案
3. 如果是知識問答題，請提供問題和參考答案
4. 題目內容要適合 LINE Bot 顯示（簡潔明瞭）
5. 包含適當的表情符號和格式
6. 題目內容要專注於資管相關的計算機科學知識，如：
   - 基本計算機概論
   - 數位邏輯與設計
   - 作業系統原理
   - 程式語言基礎
   - 資料結構與演算法
   - 網路通訊技術
   - 資料庫系統
   - 人工智慧與機器學習
   - 資訊安全基礎
   - 雲端運算概念
   - 管理資訊系統(MIS)
   - 軟體工程基礎

請生成題目："""

        # 調用 Gemini API
        response = llm.invoke(prompt)
        return response.content

    except Exception as e:
        print(f"測驗生成失敗: {e}")
        return f"❌ 測驗生成失敗，請稍後再試。錯誤: {str(e)}"

def generate_knowledge_point(query: str) -> str:
    """生成知識點的純邏輯 - 調用 Gemini API"""
    try:
        from accessories import init_ai

        # 初始化 Ollama（預設使用 Qwen）
        llm = init_ai(ai_type='ollama')

        # 構建提示詞
        if query and query.strip():
            # 根據用戶查詢生成相關知識
            prompt = f"""請生成一個關於「{query}」的資管計算機科學知識點。

⚠️ 重要格式要求（必須遵守）：
1. 只返回純文字內容，絕對不要使用 HTML 標籤（如 <h1>, <p>, <strong> 等）
2. 絕對不要使用 Markdown 語法（如 **粗體**, # 標題, - 列表, ``` 代碼塊 等）
3. 直接生成知識點內容，不要有任何前綴說明
4. 內容要簡潔明瞭，適合 LINE Bot 顯示（每行不要太長，建議每行不超過 50 字）
5. 使用簡單的換行符號分隔段落，不要使用複雜的列表格式
6. 包含適當的表情符號（如 📚、💡、🔍 等）
7. 提供實用的學習建議
8. 如果是專業術語，請提供簡單解釋
9. 專注於資管相關的計算機科學知識
10. 使用簡單的數字編號（如 1. 2. 3.）或表情符號來組織內容，不要使用 Markdown 列表語法

範例格式（正確）：
📚 知識點名稱

1. 核心定義
這裡是定義內容，使用簡單的文字說明。

2. 關鍵要點
這裡是要點內容，每行不要太長。

💡 學習建議
這裡是簡短的學習建議。

範例格式（錯誤，不要使用）：
**知識點名稱**（不要用 Markdown）
# 標題（不要用 Markdown）
- 列表項（不要用 Markdown）
<strong>粗體</strong>（不要用 HTML）

請生成知識點："""
        else:
            # 隨機生成一個知識點
            prompt = """請隨機生成一個資管計算機科學的知識點，主題可以是：
基本計算機概論、數位邏輯與設計、作業系統原理、程式語言基礎、資料結構與演算法、網路通訊技術、資料庫系統、人工智慧與機器學習、資訊安全基礎、雲端運算概念、管理資訊系統(MIS)、軟體工程基礎

⚠️ 重要格式要求（必須遵守）：
1. 只返回純文字內容，絕對不要使用 HTML 標籤（如 <h1>, <p>, <strong> 等）
2. 絕對不要使用 Markdown 語法（如 **粗體**, # 標題, - 列表, ``` 代碼塊 等）
3. 直接生成知識點內容，不要有任何前綴說明如「好的，這是一個...」或「---」等
4. 內容要簡潔明瞭，適合 LINE Bot 顯示（每行不要太長，建議每行不超過 50 字）
5. 總字數不超過 250 字，保持簡潔
6. 使用簡單的換行符號分隔段落，不要使用複雜的列表格式
7. 包含適當的表情符號（如 📚、💡、🔍 等）
8. 提供簡短的學習建議（1-2句話）
9. 知識點要有實用價值，專注於資管領域
10. 使用簡單的數字編號（如 1. 2. 3.）或表情符號來組織內容，不要使用 Markdown 列表語法

範例格式（正確）：
📚 知識點名稱

1. 核心定義
這裡是定義內容，使用簡單的文字說明。

2. 關鍵要點
這裡是要點內容，每行不要太長。

💡 學習建議
這裡是簡短的學習建議。

範例格式（錯誤，不要使用）：
**知識點名稱**（不要用 Markdown）
# 標題（不要用 Markdown）
- 列表項（不要用 Markdown）
<strong>粗體</strong>（不要用 HTML）

請生成知識點："""

        # 調用 Gemini API
        response = llm.invoke(prompt)
        content = response.content

        # 清理 HTML 和 Markdown 標記（以防萬一 AI 沒有遵守格式要求）
        import re
        # 移除 HTML 標籤
        content = re.sub(r'<[^>]+>', '', content)
        # 移除 Markdown 標題符號
        content = re.sub(r'^#+\s+', '', content, flags=re.MULTILINE)
        # 移除 Markdown 粗體/斜體
        content = re.sub(r'\*\*([^*]+)\*\*', r'\1', content)
        content = re.sub(r'\*([^*]+)\*', r'\1', content)
        # 移除 Markdown 列表符號（保留內容）
        content = re.sub(r'^[-*+]\s+', '', content, flags=re.MULTILINE)
        # 移除 Markdown 代碼塊
        content = re.sub(r'```[^`]*```', '', content, flags=re.DOTALL)
        content = re.sub(r'`([^`]+)`', r'\1', content)
        # 移除多餘的換行
        content = re.sub(r'\n{3,}', '\n\n', content)
        # 移除分隔線
        content = re.sub(r'^---+$', '', content, flags=re.MULTILINE)

        return content.strip()

    except Exception as e:
        print(f"知識點生成失敗: {e}")
        return f"❌ 知識點生成失敗，請稍後再試。錯誤: {str(e)}"

def grade_answer(answer: str, correct_answer: str, question: str) -> str:
    """批改答案的純邏輯 - 調用 Gemini API"""
    try:
        from accessories import init_ai

        # 預設使用 Ollama Qwen
        llm = init_ai(ai_type='ollama')

        # 如果沒有提供正確答案，讓 AI 根據題目判斷
        if not correct_answer and question:
            # 從題目中提取選項，讓 AI 判斷正確答案
            correct_answer_hint = "請根據題目內容判斷正確答案"
        else:
            correct_answer_hint = correct_answer

        # 確保問題和答案的對應關係正確
        # 如果問題為空或不明確，在提示詞中強調要根據題目內容判斷
        if not question:
            return "❌ 無法識別題目內容，請重新發送題目後再回答。"

        # 檢查題目是否包含選項（至少要有 A. 或 B.）
        if "A." not in question and "B." not in question:
            return "❌ 無法識別題目內容，請重新發送題目後再回答。"

        # 如果題目太短（少於5個字），可能是提取錯誤
        if len(question.strip()) < 5:
            return "❌ 無法識別題目內容，請重新發送題目後再回答。"

        prompt = f"""請批改以下測驗答案，輸出必須簡潔明瞭，適合 LINE Bot 顯示：

**重要：請仔細閱讀題目內容，確保批改的是與題目相關的答案。**

問題：{question}
用戶答案：{answer}
{'正確答案：' + correct_answer if correct_answer else '請根據題目內容判斷正確答案'}

**注意：如果用戶答案與題目內容無關，請明確指出答案錯誤，並解釋為什麼錯誤。**

**輸出格式要求（嚴格遵守）：**
1. 第一行：答案是否正確（✅ 答案正確 或 ❌ 答案錯誤）
2. 第二行：簡短解釋（如果錯誤，用1-2句話說明為什麼錯誤；如果正確，用1句話說明正確原因）
3. 第三行：簡短學習建議（1句話，不超過20字）

**嚴格禁止：**
- 不要輸出詳細的教學內容或長篇解釋
- 不要使用 markdown 格式（如 #、*、-、** 等）
- 不要輸出多行列表或複雜結構
- 總字數不超過100字
- 不要輸出「---」分隔線或任何前綴說明

**範例輸出（答案錯誤）：**
❌ 答案錯誤

主鍵的主要功用是確保資料表中每一筆記錄都具有唯一識別性，而不是用於連結兩個資料表。

💡 建議：複習主鍵和外鍵的區別，主鍵用於唯一識別，外鍵用於建立關聯。

**範例輸出（答案正確）：**
✅ 答案正確

主鍵確實用於確保資料表中每一筆記錄都具有唯一識別性。

💡 建議：可以進一步了解主鍵的設計原則和實務應用。"""

        response = llm.invoke(prompt)
        result = response.content

        # 後處理：移除 markdown 格式，限制長度
        import re
        # 移除 markdown 標記
        result = re.sub(r'#{1,6}\s+', '', result)  # 移除標題標記
        result = re.sub(r'\*\*([^*]+)\*\*', r'\1', result)  # 移除粗體
        result = re.sub(r'\*([^*]+)\*', r'\1', result)  # 移除斜體
        result = re.sub(r'^[-*+]\s+', '', result, flags=re.MULTILINE)  # 移除列表標記
        result = re.sub(r'^---+\s*$', '', result, flags=re.MULTILINE)  # 移除分隔線

        # 如果結果太長，截斷到前200字
        if len(result) > 200:
            result = result[:200] + "..."

        return result

    except Exception as e:
        return f"❌ 批改失敗：{str(e)}"

def provide_tutoring(question: str, user_answer: str, correct_answer: str) -> str:
    """提供教學指導的純邏輯 - 調用 Gemini API"""
    try:
        from accessories import init_ai

        # 預設使用 Ollama Qwen
        llm = init_ai(ai_type='ollama')

        prompt = f"""請作為 AI 導師，為以下問題提供教學指導：

問題：{question}
用戶答案：{user_answer}
正確答案：{correct_answer}

請提供：
1. 為什麼答案錯誤的解釋
2. 正確的學習方法
3. 相關知識點複習建議
4. 練習建議

要求：
1. 直接生成教學指導內容，不要有任何前綴說明如「好的，我來指導...」或「---」等
2. 內容要簡潔明瞭，適合 LINE Bot 顯示
3. 包含適當的表情符號"""

        response = llm.invoke(prompt)
        return response.content

    except Exception as e:
        return f"❌ 導師指導失敗：{str(e)}"



# ==================== 圖文選單功能處理 ====================

def handle_learning_analysis(user_id: str, reply_token: str):
    """處理學習分析功能 - 通過主代理人"""
    try:

        # 檢查用戶是否已綁定
        from accessories import mongo
        user = mongo.db.user.find_one({"lineId": user_id})

        if not user:
            reply_text(reply_token, "請先綁定您的帳號才能使用學習分析功能！\n\n請在網站上生成QR Code完成綁定。")
            return

        # 發送思考中提示
        send_thinking_message(reply_token)

        # 通過主代理人處理學習分析請求
        response = call_main_agent("請提供我的學習分析報告，包括掌握度、弱點分析和學習建議", user_id)
        push_text_message(user_id, response)

    except Exception as e:
        print(f"❌ 學習分析處理失敗: {e}")
        reply_text(reply_token, "學習分析功能暫時無法使用，請稍後再試。")

# 移除複雜的格式化函數，讓主代理人處理

def handle_goal_setting(user_id: str, reply_token: str):
    """處理目標設定功能 - 通過主代理人"""
    try:

        # 檢查用戶是否已綁定
        from accessories import mongo
        user = mongo.db.user.find_one({"lineId": user_id})

        if not user:
            reply_text(reply_token, "請先綁定您的帳號才能使用目標設定功能！\n\n請在網站上生成QR Code完成綁定。")
            return

        # 發送思考中提示
        send_thinking_message(reply_token)

        # 通過主代理人處理目標設定請求
        response = call_main_agent("請幫我查看和設定學習目標，包括每日題數、掌握度目標和學習計畫", user_id)
        push_text_message(user_id, response)

    except Exception as e:
        print(f"❌ 目標設定處理失敗: {e}")
        reply_text(reply_token, "目標設定功能暫時無法使用，請稍後再試。")

# 移除複雜的格式化函數，讓主代理人處理

def handle_news(user_id: str, reply_token: str):
    """處理最新消息功能 - 從 SQL news 表隨機撈取新聞"""
    try:
        # 記錄用戶訊息到記憶
        try:
            from src.memory_manager import add_user_message, add_ai_message
            user_message = "@最新消息"  # 記錄圖文選單指令
            add_user_message(f"line_{user_id}", user_message)
        except Exception as e:
            print(f"記錄用戶訊息到記憶失敗: {e}")
        # 從 SQL news 表隨機撈取一條新聞
        with sqldb.engine.connect() as conn:
            # 先獲取總數
            count_result = conn.execute(text("SELECT COUNT(*) as total FROM news"))
            total_count = count_result.fetchone()[0]

            if total_count == 0:
                reply_text(reply_token, "目前沒有新聞資料，請稍後再試。")
                return

            # 隨機選擇一個 ID（使用 OFFSET）
            random_offset = random.randint(0, total_count - 1)

            # 查詢新聞
            result = conn.execute(text("""
                SELECT id, title, summary, href, image, date, tags, created_at
                FROM news
                ORDER BY created_at DESC
                LIMIT 1 OFFSET :offset
            """), {'offset': random_offset})

            row = result.fetchone()

            if not row:
                reply_text(reply_token, "獲取新聞失敗，請稍後再試。")
                return

            # 解析 tags JSON
            tags = []
            if row[6]:
                try:
                    parsed_tags = json.loads(row[6]) if isinstance(row[6], str) else row[6]
                    # 確保 tags 是列表
                    if isinstance(parsed_tags, list):
                        # 處理不同格式的 tags
                        for tag in parsed_tags:
                            if isinstance(tag, str):
                                tags.append(tag)
                            elif isinstance(tag, dict):
                                # 如果是字典，嘗試提取值
                                tag_value = tag.get('name') or tag.get('tag') or tag.get('label') or str(tag)
                                if tag_value:
                                    tags.append(str(tag_value))
                            else:
                                tags.append(str(tag))
                    elif isinstance(parsed_tags, str):
                        tags = [parsed_tags]
                    else:
                        tags = []
                except Exception as e:
                    print(f"⚠️ 解析 tags 失敗: {e}")
                    tags = []

            news_item = {
                'id': row[0],
                'title': row[1] or '無標題',
                'summary': row[2] or '無摘要',
                'href': row[3] or '',
                'image': row[4] or '',
                'date': row[5] or '',
                'tags': tags,
                'created_at': row[7].isoformat() if row[7] else None
            }

            # 調試信息：輸出新聞資料
            print(f"📰 新聞資料 - ID: {news_item['id']}, 標題: {news_item['title'][:50]}")
            print(f"📰 href: '{news_item['href']}', 類型: {type(news_item['href'])}")
            print(f"📰 image: '{news_item['image'][:50] if news_item['image'] else '無'}'")

            # 使用 LINE Bot 模板訊息（TemplateMessage）或 FlexMessage 顯示新聞
            from linebot.v3.messaging import (
                FlexMessage, FlexBubble, FlexBox, FlexText, FlexButton,
                URIAction, FlexImage, FlexSeparator
            )

            # 構建 Flex Message 內容
            contents = []

            # 標題
            contents.append(
                FlexText(
                    text=news_item['title'],
                    weight="bold",
                    size="lg",
                    wrap=True,
                    color="#1DB446"
                )
            )

            # 分隔線
            contents.append(FlexSeparator(margin="md"))

            # 日期
            if news_item['date']:
                contents.append(
                    FlexText(
                        text=f"📅 {news_item['date']}",
                        size="sm",
                        color="#666666",
                        margin="md"
                    )
                )

            # 摘要
            if news_item['summary']:
                summary_text = news_item['summary'][:200] + '...' if len(news_item['summary']) > 200 else news_item['summary']
                contents.append(
                    FlexText(
                        text=summary_text,
                        size="sm",
                        color="#333333",
                        wrap=True,
                        margin="md"
                    )
                )

            # 移除 Tags 顯示（用戶不需要）

            # 處理和驗證 URI
            def normalize_uri(uri: str) -> Optional[str]:
                """標準化 URI，處理相對路徑"""
                if not uri or not isinstance(uri, str):
                    return None

                uri = uri.strip()

                # 如果已經是完整的 HTTP/HTTPS URL，直接返回
                if uri.startswith('http://') or uri.startswith('https://'):
                    return uri

                # 如果是相對路徑，嘗試補全為完整 URL
                # 常見的相對路徑格式：/news/xxx 或 news/xxx
                if uri.startswith('/'):
                    # 如果是 iThome 新聞，補全為完整 URL
                    if 'ithome' in uri.lower() or 'it' in uri.lower():
                        return f"https://www.ithome.com.tw{uri}"
                    else:
                        # 嘗試其他常見的新聞網站
                        return f"https://www.ithome.com.tw{uri}"

                # 如果沒有任何協議前綴，嘗試添加 https://
                if uri and not uri.startswith('http'):
                    # 如果是域名格式（包含點）
                    if '.' in uri and not uri.startswith('/'):
                        return f"https://{uri}"

                return None

            # 獲取並處理 href
            raw_href = news_item.get('href', '')
            print(f"🔍 原始 href: '{raw_href}', 類型: {type(raw_href)}")
            valid_href = normalize_uri(raw_href)
            print(f"🔍 標準化後的 valid_href: '{valid_href}'")

            # 創建 Flex Bubble
            bubble_params = {
                'body': FlexBox(
                    layout="vertical",
                    contents=contents
                )
            }

            # 如果有有效連結，讓圖片和 footer 按鈕都可點擊
            if valid_href:
                print(f"✅ 設置 URI action，href: {valid_href}")
                uri_action = URIAction(uri=valid_href)

                # 如果有圖片，添加圖片到 hero（可點擊）
                if news_item['image']:
                    bubble_params['hero'] = FlexImage(
                        url=news_item['image'],
                        size="full",
                        aspect_ratio="20:13",
                        aspect_mode="cover",
                        action=uri_action
                    )
                    print("✅ 設置 hero 圖片和 action")

                # 無論是否有圖片，都添加按鈕到 footer（確保可見）
                bubble_params['footer'] = FlexBox(
                    layout="vertical",
                    contents=[
                        FlexButton(
                            style="primary",
                            color="#1DB446",
                            height="sm",
                            action=URIAction(
                                label="📖 閱讀全文",
                                uri=valid_href
                            )
                        )
                    ]
                )
                print("✅ 設置 footer 按鈕")
            else:
                print(f"⚠️ 沒有有效連結，raw_href: {raw_href}")
                # 如果沒有有效連結，只顯示圖片（不可點擊）
                if news_item['image']:
                    bubble_params['hero'] = FlexImage(
                        url=news_item['image'],
                        size="full",
                        aspect_ratio="20:13",
                        aspect_mode="cover"
                    )

            bubble = FlexBubble(**bubble_params)

            flex_message = FlexMessage(
                alt_text=f"📰 {news_item['title']}",
                contents=bubble
            )

            # 調試信息：輸出 FlexMessage 結構
            print(f"📦 FlexMessage 結構 - 是否有 hero: {'hero' in bubble_params}, 是否有 footer: {'footer' in bubble_params}")
            if 'footer' in bubble_params:
                print(f"📦 Footer 按鈕數量: {len(bubble_params['footer'].contents)}")

            # 發送 Flex Message
            try:
                print("📤 正在發送 FlexMessage...")
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[flex_message]
                    )
                )
                print("✅ FlexMessage 發送成功")
                # 記錄 AI 回應到記憶
                try:
                    from src.memory_manager import add_ai_message
                    ai_response = f"最新消息: {news_item['title']}"
                    add_ai_message(f"line_{user_id}", ai_response)
                except Exception as e:
                    print(f"記錄 AI 回應到記憶失敗: {e}")
            except Exception as e:
                print(f"❌ 發送 Flex 訊息失敗: {e}")
                # FlexMessage 發送失敗後，reply_token 可能已過期
                # 直接使用 push_message 發送文字訊息
                text_content = f"""📰 {news_item['title']}

{'📅 ' + news_item['date'] + chr(10) if news_item['date'] else ''}{news_item['summary'][:300] if news_item['summary'] else '無摘要'}

{f'🔗 {news_item["href"]}' if valid_href else ''}"""
                # 嘗試使用 reply_token（如果還沒用過）
                try:
                    reply_text(reply_token, text_content)
                except:
                    # reply_token 已過期，使用 push_message
                    push_text_message(user_id, text_content)

    except Exception as e:
        print(f"❌ 最新消息處理失敗: {e}")
        import traceback
        traceback.print_exc()
        reply_text(reply_token, "最新消息功能暫時無法使用，請稍後再試。")

# 移除複雜的 API 調用函數，讓主代理人處理

def handle_calendar(user_id: str, reply_token: str):
    """處理行事曆功能 - 直接調用行事曆函數"""
    try:
        # 記錄用戶訊息到記憶
        try:
            from src.memory_manager import add_user_message, add_ai_message
            user_message = "@行事曆"  # 記錄圖文選單指令
            add_user_message(f"line_{user_id}", user_message)
        except Exception as e:
            print(f"記錄用戶訊息到記憶失敗: {e}")

        # 檢查用戶是否已綁定
        from accessories import mongo
        user = mongo.db.user.find_one({"lineId": user_id})

        if not user:
            reply_text(reply_token, "請先綁定您的帳號才能使用行事曆功能！\n\n請在網站上生成QR Code完成綁定。")
            return

        # 直接調用行事曆函數，不通過主代理人（確保返回完整內容）
        from src.dashboard import get_calendar_for_linebot
        calendar_text = get_calendar_for_linebot(user_id)

        # 發送行事曆內容
        reply_text(reply_token, calendar_text)

        # 記錄 AI 回應到記憶
        try:
            from src.memory_manager import add_ai_message
            add_ai_message(f"line_{user_id}", calendar_text[:200])  # 只記錄前200字
        except Exception as e:
            print(f"記錄 AI 回應到記憶失敗: {e}")

    except Exception as e:
        print(f"❌ 行事曆處理失敗: {e}")
        import traceback
        traceback.print_exc()
        reply_text(reply_token, "行事曆功能暫時無法使用，請稍後再試。")

# 移除複雜的選單函數，讓主代理人處理

# 移除複雜的行事曆處理函數，讓主代理人處理

# 移除複雜的事件創建函數，讓主代理人處理

def handle_random_knowledge(user_id: str, reply_token: str):
    """處理隨機知識功能"""
    try:
        # 發送思考中提示
        send_thinking_message(reply_token)

        # 直接調用知識點生成函數，不通過主代理人，確保格式簡潔
        response = generate_knowledge_point("")

        # 如果結果太長，截斷到前300字
        if len(response) > 300:
            response = response[:300] + "..."

        # 使用推播訊息發送最終回應（因為 reply_token 已經用過）
        push_text_message(user_id, response)

    except Exception as e:
        print(f"❌ 隨機知識處理失敗: {e}")
        push_text_message(user_id, "隨機知識功能暫時無法使用，請稍後再試。")

# ==================== 測驗輪盤樣板 ====================

def create_quiz_selection_carousel() -> FlexMessage:
    """創建測驗選擇輪播樣板 - 使用 LINE Bot SDK v3 Flex Message 格式"""
    from linebot.v3.messaging import FlexMessage, FlexCarousel, FlexBubble, FlexBox, FlexText, FlexButton, MessageAction

    # 創建輪播樣板 - 5張卡片
    carousel = FlexCarousel(
        contents=[
            # 第一張卡片：基本計概、數位邏輯、作業系統
            FlexBubble(
                body=FlexBox(
                    layout="vertical",
                    contents=[
                        FlexText(text="🎯 每日測驗 - 基礎知識", weight="bold", size="xl", align="center", color="#1DB446"),
                        FlexText(text="選擇您想要測驗的基礎知識點：", size="sm", align="center", color="#666666", margin="md")
                    ]
                ),
                footer=FlexBox(
                    layout="vertical",
                    contents=[
                        FlexButton(
                            style="primary",
                            color="#1DB446",
                            height="sm",
                            action=MessageAction(label="基本計概", text="@測驗 基本計概")
                        ),
                        FlexButton(
                            style="primary",
                            color="#1DB446",
                            height="sm",
                            action=MessageAction(label="數位邏輯", text="@測驗 數位邏輯")
                        ),
                        FlexButton(
                            style="primary",
                            color="#1DB446",
                            height="sm",
                            action=MessageAction(label="作業系統", text="@測驗 作業系統")
                        )
                    ],
                    spacing="sm"
                )
            ),

            # 第二張卡片：程式語言、資料結構、網路通訊
            FlexBubble(
                body=FlexBox(
                    layout="vertical",
                    contents=[
                        FlexText(text="💻 每日測驗 - 程式技術", weight="bold", size="xl", align="center", color="#1DB446"),
                        FlexText(text="選擇您想要測驗的程式技術知識點：", size="sm", align="center", color="#666666", margin="md")
                    ]
                ),
                footer=FlexBox(
                    layout="vertical",
                    contents=[
                        FlexButton(
                            style="primary",
                            color="#1DB446",
                            height="sm",
                            action=MessageAction(label="程式語言", text="@測驗 程式語言")
                        ),
                        FlexButton(
                            style="primary",
                            color="#1DB446",
                            height="sm",
                            action=MessageAction(label="資料結構", text="@測驗 資料結構")
                        ),
                        FlexButton(
                            style="primary",
                            color="#1DB446",
                            height="sm",
                            action=MessageAction(label="網路通訊", text="@測驗 網路通訊")
                        )
                    ],
                    spacing="sm"
                )
            ),

            # 第三張卡片：資料庫、AI與機器學習、資訊安全
            FlexBubble(
                body=FlexBox(
                    layout="vertical",
                    contents=[
                        FlexText(text="🔐 每日測驗 - 進階技術", weight="bold", size="xl", align="center", color="#1DB446"),
                        FlexText(text="選擇您想要測驗的進階技術知識點：", size="sm", align="center", color="#666666", margin="md")
                    ]
                ),
                footer=FlexBox(
                    layout="vertical",
                    contents=[
                        FlexButton(
                            style="primary",
                            color="#1DB446",
                            height="sm",
                            action=MessageAction(label="資料庫", text="@測驗 資料庫")
                        ),
                        FlexButton(
                            style="primary",
                            color="#1DB446",
                            height="sm",
                            action=MessageAction(label="AI與機器學習", text="@測驗 AI與機器學習")
                        ),
                        FlexButton(
                            style="primary",
                            color="#1DB446",
                            height="sm",
                            action=MessageAction(label="資訊安全", text="@測驗 資訊安全")
                        )
                    ],
                    spacing="sm"
                )
            ),

            # 第四張卡片：雲端運算、MIS系統、軟體工程
            FlexBubble(
                body=FlexBox(
                    layout="vertical",
                    contents=[
                        FlexText(text="☁️ 每日測驗 - 系統應用", weight="bold", size="xl", align="center", color="#1DB446"),
                        FlexText(text="選擇您想要測驗的系統應用知識點：", size="sm", align="center", color="#666666", margin="md")
                    ]
                ),
                footer=FlexBox(
                    layout="vertical",
                    contents=[
                        FlexButton(
                            style="primary",
                            color="#1DB446",
                            height="sm",
                            action=MessageAction(label="雲端運算", text="@測驗 雲端運算")
                        ),
                        FlexButton(
                            style="primary",
                            color="#1DB446",
                            height="sm",
                            action=MessageAction(label="MIS系統", text="@測驗 MIS系統")
                        ),
                        FlexButton(
                            style="primary",
                            color="#1DB446",
                            height="sm",
                            action=MessageAction(label="軟體工程", text="@測驗 軟體工程")
                        )
                    ],
                    spacing="sm"
                )
            ),

            # 第五張卡片：隨機（只有一個按鈕）
            FlexBubble(
                body=FlexBox(
                    layout="vertical",
                    contents=[
                        FlexText(text="🎲 每日測驗 - 隨機挑戰", weight="bold", size="xl", align="center", color="#1DB446"),
                        FlexText(text="讓系統為您隨機選擇知識點和題型：", size="sm", align="center", color="#666666", margin="md")
                    ]
                ),
                footer=FlexBox(
                    layout="vertical",
                    contents=[
                        FlexButton(
                            style="primary",
                            color="#1DB446",
                            height="sm",
                            action=MessageAction(label="🎲 隨機測驗", text="@測驗 隨機")
                        )
                    ],
                    spacing="sm"
                )
            )
        ]
    )

    return FlexMessage(
        alt_text="選擇測驗知識點",
        contents=carousel
    )
