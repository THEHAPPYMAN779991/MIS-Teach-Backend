"""
AI 教學系統 API 端點
整合 RAG 系統，提供完整的智能教學 API 服務
"""

import logging
import json
from datetime import datetime
from flask import Blueprint, request, jsonify
from typing import Dict, Any, List, Optional
from src.api import get_user_info
from accessories import mongo, refresh_token
from bson.objectid import ObjectId

# 導入 RAG 系統模組
RAG_AVAILABLE = False

try:
    from .rag_sys.rag_ai_role import handle_tutoring_conversation
    RAG_AVAILABLE = True
    logger = logging.getLogger(__name__)
except ImportError as e:
    logger = logging.getLogger(__name__)
    logger.warning(f"⚠️ RAG 系統模組導入失敗: {e}")

# 創建 Blueprint
ai_teacher_bp = Blueprint('ai_teacher', __name__)

# ==================== 工具函數 ====================

def _question_collection_from_source(source: Optional[str]):
    """Return the Mongo collection for a selectable question source.

    The quiz module allows question banks to be selected by collection name
    (e.g. "paper", "501") or by "database.collection".  AI tutoring must use
    the same source saved in quiz_templates; otherwise a submitted answer from
    a non-default collection cannot be loaded back for remedial learning.
    """
    source = str(source or "").strip()
    if not source:
        return None
    if "." in source:
        db_name, collection_name = source.split(".", 1)
        return mongo.cx[db_name][collection_name]
    return mongo.db[source]


def _find_question_document(question_id: Any, question_source: Optional[str] = None) -> Optional[dict]:
    """Find a question in its saved source collection, then fallback banks."""
    question_id_str = str(question_id)
    query_ids = []
    if ObjectId.is_valid(question_id_str):
        query_ids.append(ObjectId(question_id_str))
    query_ids.append(question_id)

    tried = set()
    collections = []

    if question_source:
        try:
            collection = _question_collection_from_source(question_source)
            if collection is not None:
                collections.append(collection)
                tried.add(f"{collection.database.name}.{collection.name}")
        except Exception as e:
            logger.warning("Unable to use question_source %s: %s", question_source, e)

    for collection_name in ('test5', 'exam'):
        collection = mongo.db[collection_name]
        key = f"{collection.database.name}.{collection.name}"
        if key not in tried:
            collections.append(collection)
            tried.add(key)

    for collection in collections:
        for query_id in query_ids:
            question = collection.find_one({'_id': query_id})
            if question:
                return question
    return None


def _normalize_difficulty(value: Any) -> int:
    """Convert numeric and textual difficulty values to the UI's 1-3 scale."""
    try:
        return int(value)
    except (TypeError, ValueError):
        normalized = str(value or '').strip().lower()
        return {
            'easy': 1,
            '簡單': 1,
            'medium': 2,
            'normal': 2,
            '中等': 2,
            'hard': 3,
            '困難': 3,
        }.get(normalized, 2)

def get_quiz_from_database(quiz_ids: List[str]) -> dict:
    """從資料庫獲取考卷數據"""
    try:
        # 從 MongoDB 獲取考卷數據
        # 根據你提供的數據結構，quiz_ids 應該是考卷的 _id，而不是題目的 _id
        quiz_doc = None

        for quiz_id in quiz_ids:
            try:
                # 優先使用 ObjectId 查詢（AI生成的測驗使用ObjectId）
                quiz_doc = mongo.db.exam.find_one({"_id": ObjectId(quiz_id)})

                if not quiz_doc:
                    # 如果 ObjectId 查詢失敗，嘗試直接查詢（支援字串格式ID）
                    quiz_doc = mongo.db.exam.find_one({"_id": quiz_id})

                if quiz_doc:
                    break

            except Exception as e:
                continue

        if not quiz_doc:
            return {
                'success': False,
                'message': '沒有找到有效的考卷數據'
            }

        # 從考卷文檔中提取題目數據
        questions = quiz_doc.get('questions', [])

        if not questions:
            return {
                'success': False,
                'message': '考卷中沒有題目數據'
            }

        # 記錄第一個題目的詳細信息
        if questions:
            first_question = questions[0]

        # 直接使用 MongoDB 中的題目數據，不進行格式轉換
        # 確保每個題目都有必要的字段
        formatted_questions = []
        for i, question in enumerate(questions):
            # 保持原始數據結構，只確保必要字段存在
            formatted_question = {
                'id': question.get('id', i + 1),
                'question_text': question.get('question_text', ''),
                'type': question.get('type', 'single-choice'),
                'options': question.get('options', []),
                'correct_answer': question.get('correct_answer', ''),
                'original_exam_id': question.get('original_exam_id', ''),
                'image_file': question.get('image_file', ''),
                'key_points': question.get('key_points', ''),
                'explanation': question.get('explanation', ''),
                'topic': question.get('topic', ''),
                'difficulty': question.get('difficulty', 'medium'),
                # 保留所有原始字段
                **question
            }
            formatted_questions.append(formatted_question)

        # 構建考卷數據
        quiz_data = {
            'quiz_id': quiz_doc.get('quiz_id', f"ai_generated_{int(datetime.now().timestamp())}"),
            'template_id': f"ai_template_{int(datetime.now().timestamp())}",
            'questions': formatted_questions,
            'time_limit': quiz_doc.get('time_limit', 60),
            'quiz_info': {
                'title': quiz_doc.get('title', f'AI生成的考卷 ({len(formatted_questions)}題)'),
                'exam_type': quiz_doc.get('type', 'knowledge'),
                'topic': quiz_doc.get('metadata', {}).get('topic', '計算機概論'),
                'difficulty': quiz_doc.get('metadata', {}).get('difficulty', 'medium'),
                'question_count': len(formatted_questions),
                'time_limit': quiz_doc.get('time_limit', 60),
                'total_score': len(formatted_questions) * 5,
                'created_at': quiz_doc.get('create_time', datetime.now().isoformat())
            },
            'database_ids': quiz_ids
        }


        return {
            'success': True,
            'data': quiz_data
        }

    except Exception as e:
        logger.error(f"獲取考卷數據時發生錯誤: {e}")
        return {
            'success': False,
            'message': f'獲取考卷數據失敗: {str(e)}'
        }

def _extract_user_answer(user_answer_raw: str) -> str:
    """提取用戶答案的實際內容"""
    print(f"🔍 _extract_user_answer 輸入: {user_answer_raw[:50]}..." if user_answer_raw else "🔍 _extract_user_answer 輸入: None")

    if not user_answer_raw:
        return '未作答'

    # 處理 LONG_ANSWER_ 引用
    if user_answer_raw.startswith('LONG_ANSWER_'):
        try:
            from .quiz import _parse_user_answer
            parsed_answer = _parse_user_answer(user_answer_raw)
            print(f"✅ LONG_ANSWER_ 解析成功: {parsed_answer[:50]}..." if parsed_answer else "✅ LONG_ANSWER_ 解析成功: None")
            return parsed_answer
        except Exception as e:
            print(f"❌ 解析長答案引用失敗: {e}")
            return f"[長答案解析錯誤: {user_answer_raw}]"

    # 如果是 JSON 格式，提取用戶答案
    if user_answer_raw.startswith('{'):
        try:
            answer_data = json.loads(user_answer_raw)

            # 優先從 answer 欄位獲取
            answer = answer_data.get('answer', '')
            if answer:
                return answer

            # 如果 answer 為空，從 feedback.explanation 中提取用戶答案
            feedback = answer_data.get('feedback', {})
            explanation = feedback.get('explanation', '')

            # 從 explanation 中提取用戶答案的關鍵詞
            if '您的答案' in explanation:
                # 提取「您的答案 X 是」或類似格式
                import re
                patterns = [
                    r'您的答案\s*([^\s是]+)',
                    r'學生答案\s*[『「]([^』」]+)[』」]',
                    r'學生答案為\s*[『「]([^』」]+)[』」]',
                    r'答案\s*[『「]([^』」]+)[』」]'
                ]

                for pattern in patterns:
                    match = re.search(pattern, explanation)
                    if match:
                        return match.group(1).strip()

            # 如果都沒有，返回 '未作答'
            return '未作答'

        except json.JSONDecodeError:
            return user_answer_raw

    return user_answer_raw


def _stringify_for_tutoring(value: Any) -> str:
    """把選項、答案或批改資料轉成穩定可讀文字，避免直接塞 Python repr。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


def _format_options_for_tutoring(options: Any) -> str:
    """保留單選/多選題所有選項，供教學端辨識學生錯選或漏選。"""
    if not options:
        return ""

    normalized = []

    if isinstance(options, dict):
        for key, value in options.items():
            key_text = str(key).strip()
            value_text = _stringify_for_tutoring(value)
            if key_text or value_text:
                normalized.append(f"{key_text}. {value_text}".strip())
    elif isinstance(options, list):
        for index, item in enumerate(options):
            if isinstance(item, dict):
                label = (
                    item.get("label")
                    or item.get("key")
                    or item.get("option")
                    or item.get("id")
                    or chr(ord("A") + index)
                )
                text = (
                    item.get("text")
                    or item.get("content")
                    or item.get("value")
                    or item.get("answer")
                    or ""
                )
                text = _stringify_for_tutoring(text)
                normalized.append(f"{label}. {text}".strip())
            else:
                text = _stringify_for_tutoring(item)
                if text:
                    normalized.append(text)
    else:
        text = _stringify_for_tutoring(options)
        if text:
            normalized.append(text)

    return "\n".join(f"- {line}" for line in normalized if line)


def _build_tutoring_question_context(
    question_text: str,
    question_type: str,
    options: Any,
    user_answer: Any,
    correct_answer: Any,
    grading_feedback: Any,
) -> str:
    """建立給教學模型看的完整題目脈絡；GraphRAG 搜尋仍使用原始題目文字。"""
    question_type = str(question_type or "").strip()
    options_text = _format_options_for_tutoring(options)
    feedback_text = ""

    if isinstance(grading_feedback, dict) and grading_feedback:
        feedback_parts = []
        for label, key in (
            ("批改說明", "explanation"),
            ("學生優點", "strengths"),
            ("錯誤或不足", "weaknesses"),
            ("學習建議", "suggestions"),
        ):
            value = grading_feedback.get(key)
            value_text = _stringify_for_tutoring(value)
            if value_text:
                feedback_parts.append(f"- {label}：{value_text}")
        feedback_text = "\n".join(feedback_parts)
    elif grading_feedback:
        feedback_text = _stringify_for_tutoring(grading_feedback)

    is_choice_like = any(
        token in question_type.lower()
        for token in ("single", "multiple", "choice", "true-false", "select")
    ) or bool(options_text)

    choice_instruction = ""
    if is_choice_like:
        choice_instruction = """

**選擇題/多選題教學要求：**
- 題詞中保留所有選項，因為錯題診斷需要知道每個選項代表的概念。
- 回饋時不要平均解釋所有選項；請優先說明學生選錯、漏選或多選的選項。
- 若某個未選選項其實是正確答案，請說明它為何正確。
- 若某個已選選項是錯誤答案，請說明它代表的迷思概念。
- GraphRAG 補救學習仍以原題核心概念與批改指出的錯誤概念為主，不要被其他無關選項帶偏。
"""

    sections = [
        "**原始題目：**",
        str(question_text or "").strip() or "無題目文字",
    ]
    if question_type:
        sections.extend(["", f"**題型：** {question_type}"])
    if options_text:
        sections.extend(["", "**所有選項：**", options_text])

    sections.extend(
        [
            "",
            f"**學生答案：** {_stringify_for_tutoring(user_answer) or '未作答'}",
            f"**正確答案：** {_stringify_for_tutoring(correct_answer) or '無'}",
        ]
    )
    if feedback_text:
        sections.extend(["", "**批改回饋重點：**", feedback_text])
    if choice_instruction:
        sections.append(choice_instruction.strip())

    return "\n".join(sections).strip()


def _build_retrieval_question_context(
    question_text: str,
    question_type: str,
    options: Any,
) -> str:
    """Build a safe GraphRAG search query from the question and options only.

    This intentionally excludes the student answer, correct answer, and grading
    feedback.  Choice questions often have short stems such as "Which statement
    is false?", so the options are needed for accurate concept retrieval.
    """
    sections = [str(question_text or "").strip()]
    type_text = str(question_type or "").strip()
    if type_text:
        sections.extend(["", f"題型：{type_text}"])
    options_text = _format_options_for_tutoring(options)
    if options_text:
        sections.extend(["", "選項：", options_text])
    return "\n".join(part for part in sections if str(part).strip()).strip()

def direct_answer_question(question: str, user_email: str = None) -> str:
    """
    直接解答問題 - 使用RAG系統，直接給出答案和解釋
    不使用引導式教學，不進行評分，不管理學習進度

    Args:
        question: 用戶的問題
        user_email: 用戶email（可選）

    Returns:
        str: AI的直接解答
    """
    try:
        if not RAG_AVAILABLE:
            return "抱歉，AI 直接解答服務暫時不可用。"

        # 調用RAG系統的直接解答功能
        from .rag_sys.rag_ai_role import handle_direct_answer
        return handle_direct_answer(question, user_email)

    except ImportError as e:
        logger.error(f"❌ RAG系統導入失敗: {e}")
        return "抱歉，AI直接解答系統暫時不可用。"
    except Exception as e:
        logger.error(f"❌ 直接解答失敗: {e}")
        return f"抱歉，處理問題時發生錯誤：{str(e)}"

def chat_with_ai(question: str, conversation_type: str = "general", session_id: str = None, request_data: dict = None, auth_token: str = None) -> dict:
    """AI 對話處理 - 簡化版本"""
    try:
        if not RAG_AVAILABLE:
            return {
                'success': False,
                'error': 'AI 服務不可用',
                'response': '抱歉，AI 教學服務暫時不可用。'
            }

        if conversation_type == "tutoring" and session_id:
            try:
                # 從傳入的數據中獲取必要數據
                data = request_data or {}
                correct_answer = data.get('correct_answer', '')
                user_answer = data.get('user_answer', '')

                # 新增：獲取AI批改的評分反饋
                grading_feedback = data.get('grading_feedback', {})
                question_type = (
                    data.get('question_type')
                    or data.get('answer_type')
                    or data.get('type')
                    or ''
                )
                options = data.get('options', data.get('choices', []))

                # 判斷是否為初始化請求
                is_initialization = question.startswith('開始學習會話：')
                if is_initialization:
                    actual_question = question.replace('開始學習會話：', '').strip()
                    user_input = None
                else:
                    if '用戶問題：' in question:
                        parts = question.split('用戶問題：', 1)
                        actual_question = parts[0].replace('題目：', '').strip()
                        user_input = parts[1].strip()
                    else:
                        actual_question = data.get('question_text', '')
                        user_input = question
                # 直接調用 verify_token 獲取用戶 email
                from .api import verify_token
                user_email = verify_token(auth_token) if auth_token else "anonymous_user"

                tutoring_question_context = _build_tutoring_question_context(
                    actual_question,
                    question_type,
                    options,
                    user_answer,
                    correct_answer,
                    grading_feedback,
                )
                retrieval_question_context = _build_retrieval_question_context(
                    actual_question,
                    question_type,
                    options,
                )

                # 傳遞AI批改的評分反饋與完整題目脈絡。
                # GraphRAG 檢索使用「題目 + 選項」的安全查詢文字，
                # 但不包含學生答案、正確答案或批改回饋，避免洩漏答案。
                response = handle_tutoring_conversation(
                    user_email,
                    actual_question,
                    user_answer,
                    correct_answer,
                    user_input,
                    grading_feedback,
                    question_context=tutoring_question_context,
                    retrieval_context=retrieval_question_context,
                )

                # 把 GraphRAG usage 拉到頂層，方便前端顯示徽章
                graphrag_usage = None
                if isinstance(response, dict):
                    graphrag_usage = response.get('graphrag_usage')

                return {
                    'success': True,
                    'response': response,
                    'conversation_type': 'tutoring',
                    'session_id': session_id,
                    # ✨ 讓前端直接看得到「本次對話用了 GraphRAG 幾個概念、幾條先輩鏈」
                    'graphrag_usage': graphrag_usage,
                }
            except Exception as e:
                logger.error(f"❌ 教學對話失敗: {e}")
                return {
                    'success': False,
                    'error': f'教學對話失敗：{str(e)}',
                    'response': '抱歉，教學對話處理失敗，請重試。'
                }
        else:
            # 其他類型的對話處理
            return {
                'success': False,
                'error': '不支援的對話類型',
                'response': '抱歉，此對話類型不支援。'
            }

    except Exception as e:
        logger.error(f"❌ AI對話處理失敗: {e}")
        return {
            'success': False,
            'error': f'AI對話處理失敗：{str(e)}',
            'response': '抱歉，AI對話處理失敗，請重試。'
        }

def get_quiz_result_data(result_id: str) -> dict:
    """獲取測驗結果數據"""
    try:
        if not result_id.startswith('result_'):
            return None

        try:
            quiz_history_id = int(result_id.split('_')[1])
        except (ValueError, IndexError):
            return None

        from accessories import sqldb
        from sqlalchemy import text

        with sqldb.engine.connect() as conn:
            # 查詢 quiz_history 和 quiz_templates
            history_result = conn.execute(text("""
                SELECT qh.id, qh.quiz_template_id, qh.user_email, qh.quiz_type,
                       qh.total_questions, qh.answered_questions, qh.correct_count, qh.wrong_count,
                       qh.accuracy_rate, qh.average_score, qh.total_time_taken,
                       qh.submit_time, qh.status, qh.created_at,
                       qt.question_ids, qt.question_source
                FROM quiz_history qh
                LEFT JOIN quiz_templates qt ON qh.quiz_template_id = qt.id
                WHERE qh.id = :quiz_history_id
            """), {
                'quiz_history_id': quiz_history_id
            }).fetchone()

            if not history_result:
                return None

            # 獲取所有題目的用戶答案
            answers_result = conn.execute(text("""
                SELECT mongodb_question_id, user_answer, is_correct, score, feedback, created_at
                FROM quiz_answers
                WHERE quiz_history_id = :quiz_history_id
                ORDER BY created_at
            """), {
                'quiz_history_id': quiz_history_id
            }).fetchall()

            # 構建答案字典，用於快速查找
            answers_dict = {}
            for answer in answers_result:
                question_id = str(answer[0])  # 確保ID為字符串格式
                user_answer = answer[1]
                is_correct = bool(answer[2])  # 確保為 boolean 類型
                score = float(answer[3]) if answer[3] is not None else 0.0
                feedback = json.loads(answer[4]) if answer[4] else {}  # 將JSON字符串轉換回Python字典
                created_at = answer[5]

                answers_dict[question_id] = {
                    'user_answer': user_answer,
                    'is_correct': is_correct,
                    'score': score,
                    'feedback': feedback,  # 添加feedback字段
                    'created_at': created_at
                }

            # 解析題目ID列表
            question_ids_str = history_result[14]
            question_source = history_result[15] if len(history_result) > 15 and history_result[15] else None
            if question_ids_str:
                try:
                    question_ids = json.loads(question_ids_str)
                except json.JSONDecodeError:
                    question_ids = []
            else:
                question_ids = []

            # 構建題目陣列
            questions = []
            for question_id in question_ids:
                question_obj = _find_question_document(question_id, question_source)
                if not question_obj:
                    logger.warning(
                        "Quiz result %s references missing question %s in source %s",
                        result_id,
                        question_id,
                        question_source,
                    )
                    continue

                # 從 answers_dict 獲取題目資訊 - 確保ID格式一致
                question_id_str = str(question_id)
                answer_info = answers_dict.get(question_id_str, {})
                is_correct = answer_info.get('is_correct', False)  # 預設為錯誤，確保能撈取到錯題
                user_answer_raw = answer_info.get('user_answer', '')

                # 解析用戶答案
                actual_user_answer = _extract_user_answer(user_answer_raw)

                print(f"🔍 題目 {question_id_str} 數據:", {
                    'answer_type': question_obj.get('answer_type', 'single-choice'),
                    'user_answer_raw': user_answer_raw[:50] + '...' if user_answer_raw else 'None',
                    'actual_user_answer': actual_user_answer[:50] + '...' if actual_user_answer else 'None',
                    'is_base64': actual_user_answer.startswith('data:image/') if actual_user_answer else False
                })

                question_data = {
                    'question_id': str(question_obj['_id']),
                    'question_text': question_obj.get('question_text', ''),
                    'correct_answer': question_obj.get('answer', ''),
                    'user_answer': actual_user_answer,
                    'is_correct': is_correct,
                    'score': answer_info.get('score', 0),
                    'is_marked': False,
                    'type': question_obj.get('answer_type', question_obj.get('type', 'single-choice')),
                    'topic': question_obj.get('topic', question_obj.get('primary_concept', '計算機概論')),
                    'difficulty': _normalize_difficulty(
                        question_obj.get('difficulty', question_obj.get('difficulty_level', 2))
                    ),
                    'options': question_obj.get('options', []),
                    'image_file': question_obj.get('image_file', ''),
                    'key_points': question_obj.get('key_points', question_obj.get('key-points', '')),
                    'question_source': question_source,
                    'feedback': answer_info.get('feedback', {})  # 添加feedback字段
                }

                questions.append(question_data)

            # 構建返回結果 - 確保所有數值字段都是 JSON 可序列化的
            result = {
                'quiz_history_id': int(history_result[0]) if history_result[0] is not None else 0,
                'quiz_template_id': int(history_result[1]) if history_result[1] is not None else 0,
                'user_email': str(history_result[2]) if history_result[2] else '',
                'quiz_type': str(history_result[3]) if history_result[3] else '',
                'total_questions': int(history_result[4]) if history_result[4] is not None else 0,
                'answered_questions': int(history_result[5]) if history_result[5] is not None else 0,
                'correct_count': int(history_result[6]) if history_result[6] is not None else 0,
                'wrong_count': int(history_result[7]) if history_result[7] is not None else 0,
                'accuracy_rate': float(history_result[8]) if history_result[8] is not None else 0.0,
                'average_score': float(history_result[9]) if history_result[9] is not None else 0.0,
                'total_time_taken': int(history_result[10]) if history_result[10] is not None else 0,
                'submit_time': history_result[11].isoformat() if history_result[11] else None,
                'status': str(history_result[12]) if history_result[12] else '',
                'created_at': history_result[13].isoformat() if history_result[13] else None,
                'questions': questions,
                'errors': [q for q in questions if not q['is_correct']]
            }

            return result

    except Exception:
        logger.exception("Failed to load quiz result %s", result_id)
        return None

# ==================== API 路由 ====================

@ai_teacher_bp.route('/ai-tutoring', methods=['POST', 'OPTIONS'])
def ai_tutoring():
    """AI 教學對話端點"""
    try:
        if request.method == 'OPTIONS':
            return jsonify({'token': None, 'success': True}), 204

        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return jsonify({'token': None, 'message': '未提供token'}), 401

        token = auth_header.split(" ")[1]

        data = request.get_json()
        user_input = data.get('user_input', '')
        session_id = data.get('session_id', '')
        conversation_type = data.get('conversation_type', 'tutoring')


        # 調用 AI 對話處理，傳遞必要的數據
        result = chat_with_ai(
            user_input or "初始化會話",
            conversation_type,
            session_id,
            request_data=data,
            auth_token=token
        )

        # 確保返回正確的結構給前端
        if isinstance(result, dict) and 'success' in result:
            # 將 token 加入到結果中
            result['token'] = refresh_token(token)
            return jsonify(result)
        else:
            # 如果 result 不是期待的格式，建立一個標準回應
            return jsonify({
                'success': False,
                'error': 'AI回應格式錯誤',
                'response': '抱歉，AI教學對話處理失敗，請重試。',
                'token': refresh_token(token)
            })

    except Exception as e:
        logger.error(f"❌ AI教學對話端點錯誤: {e}")
        return jsonify({
            'success': False,
            'error': f'AI教學對話失敗：{str(e)}',
            'response': '抱歉，AI教學對話處理失敗，請重試。',
            'token': None
        }), 500

@ai_teacher_bp.route('/get-quiz-result/<result_id>', methods=['GET', 'OPTIONS'])
def get_quiz_result(result_id):
    """獲取測驗結果"""
    if request.method == 'OPTIONS':
        return jsonify({'token': None, 'success': True}), 204

    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return jsonify({'token': None, 'message': '未提供token'}), 401

    token = auth_header.split(" ")[1]
    user_email = get_user_info(token, 'email')
    if not user_email:
        return jsonify({'token': None, 'message': '無效的token'}), 401

    # 獲取測驗結果數據
    result_data = get_quiz_result_data(result_id)

    if result_data:
        return jsonify({
            'token': refresh_token(token),
            'success': True,
            'data': result_data
        })
    else:
        return jsonify({
            'token': refresh_token(token),
            'success': False,
            'message': '未找到測驗結果'
        }), 404

@ai_teacher_bp.route('/get-quiz-from-database', methods=['POST', 'OPTIONS'])
def get_quiz_from_database_endpoint():
    """從資料庫獲取考卷數據"""
    try:
        if request.method == 'OPTIONS':
            return jsonify({'token': None, 'success': True}), 204

        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return jsonify({'token': None, 'message': '未提供token'}), 401

        token = auth_header.split(" ")[1]

        data = request.get_json()
        quiz_ids = data.get('quiz_ids', [])

        if not quiz_ids:
            return jsonify({
                'success': False,
                'message': '缺少考卷ID'
            }), 400

        # 調用獲取考卷數據函數
        result = get_quiz_from_database(quiz_ids)

        return jsonify({'token': refresh_token(token), 'data': result})

    except Exception as e:
        logger.error(f"❌ 獲取考卷數據失敗: {e}")
        return jsonify({
            'success': False,
            'message': f'獲取考卷數據失敗：{str(e)}'
        }), 500
