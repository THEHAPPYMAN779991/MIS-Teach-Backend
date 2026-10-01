#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能AI考卷生成器 - 動態生成題目，無備用題目
"""

import logging
from typing import Dict, List, Any, Optional
from datetime import datetime
from bson import ObjectId
import json
import random
import time
import re

# 設置日誌
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SmartQuizGenerator:
    """智能AI考卷生成器 - 無備用題目，純AI生成"""

    def __init__(self):
        self.question_types = {
            'single-choice': '單選題',
            'multiple-choice': '多選題',
            'fill-in-the-blank': '填空題',
            'true-false': '是非題',
            'short-answer': '簡答題',
            'long-answer': '申論題'
        }

        self.difficulty_levels = {
            'easy': '簡單',
            'medium': '中等',
            'hard': '困難'
        }

        # 重試配置
        self.max_retries = 3
        self.retry_delay = 2  # 秒

    def generate_quiz(self, requirements: Dict[str, Any]) -> Dict[str, Any]:
        """
        根據需求生成考卷 - 智能重試機制

        Args:
            requirements: 包含以下字段的字典
                - topic: 知識點/主題
                - question_types: 題型列表
                - difficulty: 難度
                - question_count: 題目數量
                - exam_type: 考卷類型 ('knowledge' 或 'pastexam')
                - school: 學校 (考古題用)
                - year: 年份 (考古題用)
                - department: 科系 (考古題用)

        Returns:
            生成的考卷數據
        """
        logger.info(f"🚀 開始智能生成考卷，需求: {requirements}")

        # 驗證需求
        validated_req = self._validate_requirements(requirements)

        # 根據考卷類型生成題目
        if validated_req['exam_type'] == 'pastexam':
            questions = self._generate_pastexam_questions(validated_req)
        elif validated_req['exam_type'] == 'content-based':
            questions = self._generate_content_based_questions(validated_req)
        else:
            questions = self._generate_knowledge_questions(validated_req)

        # 檢查是否成功生成足夠的題目
        if len(questions) < validated_req['question_count']:
            logger.warning(f"⚠️ 只成功生成 {len(questions)} 題，少於要求的 {validated_req['question_count']} 題")
            if len(questions) == 0:
                return {
                    'success': False,
                    'error': f"無法生成任何題目，請檢查AI服務是否正常"
                }

        # 生成考卷信息
        quiz_info = self._generate_quiz_info(validated_req, questions)

        logger.info(f"✅ 考卷生成完成，成功生成 {len(questions)} 題")

        return {
            'success': True,
            'quiz_info': quiz_info,
            'questions': questions,
            'generated_at': datetime.now().isoformat()
        }

    def generate_and_save_quiz(self, requirements: Dict[str, Any]) -> Dict[str, Any]:
        """
        生成考卷並保存到數據庫

        Args:
            requirements: 考卷需求

        Returns:
            包含數據庫ID的考卷數據
        """
        logger.info(f"🔍 開始生成考卷，需求: {requirements}")

        # 生成考卷
        logger.info("🔍 調用 generate_quiz 方法...")
        quiz_result = self.generate_quiz(requirements)
        logger.info(f"🔍 generate_quiz 結果: success={quiz_result.get('success', False)}")

        if not quiz_result['success']:
            logger.error(f"❌ 考卷生成失敗: {quiz_result.get('error', '未知錯誤')}")
            return quiz_result

        # 保存到數據庫
        logger.info("🔍 開始保存到數據庫...")
        saved_questions = self._save_questions_to_database(quiz_result['questions'], requirements)
        logger.info(f"🔍 數據庫保存結果: {len(saved_questions)} 個題目ID")

        if saved_questions:
            quiz_result['database_ids'] = saved_questions
            quiz_result['message'] = "考卷已成功生成並保存到數據庫"
            logger.info("✅ 考卷生成並保存成功")
        else:
            logger.warning("⚠️ 數據庫保存失敗，但考卷生成成功")

        return quiz_result

    def _save_questions_to_database(self, questions: List[Dict], requirements: Dict) -> List[str]:
        """
        將題目保存到MongoDB數據庫

        Args:
            questions: 題目列表
            requirements: 需求參數

        Returns:
            保存的題目ID列表
        """
        try:
            from accessories import mongo

            # 檢查 mongo 對象是否可用
            if mongo is None or mongo.db is None:
                logger.warning("⚠️ MongoDB 連接不可用")
                logger.info("📝 跳過數據庫保存，僅生成考卷")
                return []

            # 創建完整的考卷文檔
            quiz_id = f"ai_generated_{int(time.time())}"

            # 根據考卷類型設置不同的標題和類型
            if requirements.get('exam_type') == 'content-based':
                title = f"基於內容的AI生成測驗"
                quiz_type = "content-based"
            elif requirements.get('exam_type') == 'pastexam':
                title = f"{requirements.get('school', 'AI生成')}考古題測驗"
                quiz_type = "pastexam"
            else:
                title = f"{requirements.get('topic', 'AI生成')}知識點測驗"
                quiz_type = "knowledge"

            # 轉換題目格式以符合exam文檔結構
            formatted_questions = []
            for i, question in enumerate(questions):
                # 處理選項格式
                options = question.get('options', [])
                processed_options = []
                if options and isinstance(options, list):
                    for option in options:
                        if ': ' in option:
                            processed_options.append(option.split(': ', 1)[1])
                        else:
                            processed_options.append(option)

                # 確定答案類型
                answer_type = "single" if question.get('type') == 'multiple-choice' else "short-answer"

                formatted_question = {
                    "_id": ObjectId(),
                    "type": answer_type,
                    "school": "",
                    "department": "",
                    "year": "",
                    "question_number": str(i + 1),
                    "question_text": question.get('question_text', ''),
                    "options": processed_options,
                    "answer": question.get('correct_answer', ''),
                    "answer_type": answer_type,
                    "image_file": [],
                    "detail-answer": question.get('explanation', ''),
                    "key-points": question.get('key_points', requirements.get('topic', 'AI生成')),
                    # Canonical concepts are resolved by GraphRAG before grading.
                    # Do not invent graph nodes such as "主題基礎" or "主題應用".
                    "micro_concepts": [requirements.get('topic', 'AI生成')],
                    "difficulty_level": '中等' if requirements.get('difficulty', 'medium') == 'medium' else ('簡單' if requirements.get('difficulty', 'medium') == 'easy' else '困難'),
                    "error_reason": "",
                    "created_at": datetime.now()
                }
                formatted_questions.append(formatted_question)

            # 直接保存題目作為獨立文檔，不需要測驗文檔
            if formatted_questions:
                question_results = mongo.db.exam.insert_many(formatted_questions)

                # 創建SQL template（使用所有題目的ID）
                question_ids = [str(q_id) for q_id in question_results.inserted_ids]
                template_id = create_sql_template_for_quiz(question_ids, {
                    'title': title,
                    'total_questions': len(formatted_questions),
                    'difficulty': requirements.get('difficulty', 'medium'),
                    'concept': requirements.get('topic', 'AI生成'),
                    'domain': 'AI生成測驗'
                })

                return [str(question_results.inserted_ids[0])]  # 返回第一個題目的ID
            else:
                return []

        except ImportError as e:
            return []
        except Exception as e:
            return []

    def _convert_to_database_format(self, question: Dict, requirements: Dict) -> Dict:
        """
        將題目轉換為數據庫格式

        Args:
            question: 原始題目
            requirements: 需求參數

        Returns:
            數據庫格式的題目
        """
        # 根據您的數據庫格式創建題目
        db_question = {
            "type": "single",  # 單題類型
            "school": requirements.get('school', 'AI生成'),
            "department": requirements.get('department', 'AI生成'),
            "year": requirements.get('year', str(datetime.now().year)),
            "question_number": str(question.get('id', 1)),
            "question_text": question.get('question_text', ''),
            "options": question.get('options', []),
            "answer": question.get('correct_answer', ''),
            "answer_type": self._map_answer_type(question.get('type', 'single-choice')),
            "image_file": question.get('image_file', []),
            "detail-answer": question.get('explanation', ''),
            "key-points": [question.get('key_points', requirements.get('topic', 'AI生成'))],
            "difficulty level": self._map_difficulty(question.get('difficulty', 'medium')),
            "create_time": datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        }

        return db_question

    def _map_answer_type(self, question_type: str) -> str:
        """映射題目類型到答案類型"""
        type_mapping = {
            'single-choice': 'single-choice',
            'multiple-choice': 'multiple-choice',
            'fill-in-the-blank': 'fill-in-the-blank',
            'true-false': 'true-false',
            'short-answer': 'short-answer',
            'long-answer': 'long-answer'
        }
        return type_mapping.get(question_type, 'single-choice')

    def _map_difficulty(self, difficulty: str) -> str:
        """映射難度等級"""
        difficulty_mapping = {
            'easy': '簡單',
            'medium': '中等',
            'hard': '困難'
        }
        return difficulty_mapping.get(difficulty, '中等')

    def _validate_requirements(self, requirements: Dict[str, Any]) -> Dict[str, Any]:
        """驗證和標準化需求"""
        # 設置默認值
        defaults = {
            'topic': '計算機概論',
            'question_types': ['single-choice', 'multiple-choice'],
            'difficulty': 'medium',
            'question_count': 1,  # 改為1題默認，避免強制5題
            'exam_type': 'knowledge',
            'school': '',
            'year': '',
            'department': ''
        }

        # 合併用戶需求和默認值
        validated = defaults.copy()
        validated.update(requirements)

        # 確保題目數量在合理範圍內
        validated['question_count'] = max(1, min(50, validated['question_count']))

        return validated

    def _generate_knowledge_questions(self, requirements: Dict[str, Any]) -> List[Dict[str, Any]]:
        """使用AI生成知識點題目 - 智能重試機制"""
        questions = []
        topic = requirements['topic']
        difficulty = requirements['difficulty']
        question_count = requirements['question_count']
        question_types = requirements['question_types']

        logger.info(f"🧠 開始智能生成，總共需要 {question_count} 題")

        # 逐題生成，每題都有重試機制
        for i in range(question_count):
            question_type = random.choice(question_types)
            logger.info(f"🔄 正在生成第 {i + 1}/{question_count} 題，題型: {question_type}")

            # 智能生成單題，帶重試機制
            question = self._smart_generate_single_question(
                question_number=i + 1,
                topic=topic,
                difficulty=difficulty,
                question_type=question_type,
                selected_text=requirements.get('selected_text'),
                requirements=requirements
            )

            if question:
                questions.append(question)
                logger.info(f"✅ 第 {i + 1} 題生成成功")
            else:
                logger.warning(f"⚠️ 第 {i + 1} 題生成失敗，跳過此題")
                # 不再使用備用題目，直接跳過

            # 每題之間稍作延遲，避免API限制
            if i < question_count - 1:
                time.sleep(1)

        logger.info(f"🎯 題目生成完成，成功生成 {len(questions)} 題")
        return questions

    def _smart_generate_single_question(self, question_number: int, topic: str,
                                      difficulty: str, question_type: str, selected_text: str = None, requirements: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
        """智能生成單一題目 - 帶重試機制"""

        for attempt in range(self.max_retries):
            try:
                logger.info(f"🔄 第 {question_number} 題，第 {attempt + 1} 次嘗試")

                # 直接初始化LLM，預設使用 Ollama
                from accessories import init_ai

                # 初始化LLM（預設使用 Ollama）
                llm = init_ai(ai_type='ollama')

                # LLM已經初始化完成

                # 構建動態提示詞
                prompt = self._build_dynamic_prompt(topic, difficulty, question_type, selected_text, requirements)

                # 調用AI生成
                response = llm.invoke(prompt)
                response_text = response.content if hasattr(response, 'content') else str(response)

                logger.info(f"📝 AI回應長度: {len(response_text)} 字符")
                logger.info(f"📝 AI完整回應內容: {response_text}")

                # 如果回應為空，記錄更多調試信息
                if not response_text or len(response_text.strip()) == 0:
                    logger.error("❌ AI回應為空！")
                    logger.error(f"原始response對象: {response}")
                    logger.error(f"response.content: {getattr(response, 'content', 'N/A')}")
                    logger.error(f"response.text: {getattr(response, 'text', 'N/A')}")
                    logger.error(f"response.message: {getattr(response, 'message', 'N/A')}")

                    if attempt < self.max_retries - 1:
                        logger.info(f"⏳ 等待 {self.retry_delay} 秒後重試...")
                        time.sleep(self.retry_delay)
                        continue
                    else:
                        return None

                # 提取和驗證JSON
                logger.info(f"🔍 開始提取和驗證第 {question_number} 題的JSON")
                question_data = self._extract_and_validate_single_question(response_text)

                if question_data:
                    logger.info(f"✅ 第 {question_number} 題JSON提取成功")
                    # 添加題目編號和類型
                    question_data['id'] = question_number
                    question_data['type'] = question_type
                    question_data['topic'] = topic
                    question_data['difficulty'] = difficulty
                    question_data['image_file'] = []

                    return question_data
                else:
                    logger.warning(f"⚠️ 第 {question_number} 題JSON提取或驗證失敗")
                    logger.warning(f"失敗的AI回應內容: {response_text}")

                    if attempt < self.max_retries - 1:
                        logger.info(f"⏳ 等待 {self.retry_delay} 秒後重試...")
                        time.sleep(self.retry_delay)
                        continue
                    else:
                        return None

            except Exception as e:
                logger.error(f"❌ 生成第 {question_number} 題時發生錯誤: {e}")

                if attempt < self.max_retries - 1:
                    logger.info(f"⏳ 等待 {self.retry_delay} 秒後重試...")
                    time.sleep(self.retry_delay)
                    continue
                else:
                    return None

        return None

    def _smart_generate_content_based_question(self, question_number: int, selected_text: str,
                                             difficulty: str, question_type: str) -> Optional[Dict[str, Any]]:
        """基於內容智能生成單一題目 - 帶重試機制"""

        for attempt in range(self.max_retries):
            try:
                logger.info(f"🔄 基於內容生成第 {question_number} 題，第 {attempt + 1} 次嘗試")

                # 直接初始化LLM，避免循環導入問題
                from accessories import init_ai

                # 初始化LLM（預設使用 Ollama Qwen）
                llm = init_ai(ai_type='ollama')

                # 構建基於內容的動態提示詞
                prompt = self._build_content_based_prompt(selected_text, difficulty, question_type)

                # 調用AI生成
                response = llm.invoke(prompt)
                response_text = response.content if hasattr(response, 'content') else str(response)

                logger.info(f"📝 基於內容AI回應長度: {len(response_text)} 字符")

                if not response_text or len(response_text.strip()) == 0:
                    logger.error("❌ 基於內容AI回應為空！")
                    if attempt < self.max_retries - 1:
                        logger.info(f"⏳ 等待 {self.retry_delay} 秒後重試...")
                        time.sleep(self.retry_delay)
                        continue
                    else:
                        return None

                # 提取和驗證JSON
                logger.info(f"🔍 開始提取和驗證第 {question_number} 題的JSON")
                question_data = self._extract_and_validate_single_question(response_text)

                if question_data:
                    logger.info(f"✅ 第 {question_number} 題JSON提取成功")
                    # 添加題目編號和類型
                    question_data['id'] = question_number
                    question_data['type'] = question_type
                    question_data['topic'] = '基於內容生成'
                    question_data['difficulty'] = difficulty
                    question_data['image_file'] = []
                    question_data['generation_type'] = 'content-based'

                    return question_data
                else:
                    logger.warning(f"⚠️ 第 {question_number} 題JSON提取或驗證失敗")

                    if attempt < self.max_retries - 1:
                        logger.info(f"⏳ 等待 {self.retry_delay} 秒後重試...")
                        time.sleep(self.retry_delay)
                        continue
                    else:
                        return None

            except Exception as e:
                logger.error(f"❌ 基於內容生成第 {question_number} 題時發生錯誤: {e}")

                if attempt < self.max_retries - 1:
                    logger.info(f"⏳ 等待 {self.retry_delay} 秒後重試...")
                    time.sleep(self.retry_delay)
                    continue
                else:
                    return None

        return None

    def _build_content_based_prompt(self, selected_text: str, difficulty: str, question_type: str) -> str:
        """構建基於內容的AI提示詞"""

        # 根據題型調整提示詞
        if question_type == 'single-choice':
            option_instruction = "提供4個選項，只有1個正確答案"
            answer_format = '"A"'
        elif question_type == 'multiple-choice':
            option_instruction = "提供4個選項，正確答案可以是1-3個，用逗號分隔（如：'A,C'）"
            answer_format = '"A,C"'
        else:
            option_instruction = "提供4個選項"
            answer_format = '"A"'

        prompt = f"""請基於以下提供的內容，創建一道{self.difficulty_levels[difficulty]}程度的{self.question_types[question_type]}。

提供的內容：
「{selected_text}」

要求：
1. 題目必須完全基於提供的內容，不能偏離主題
2. 題目要測試對提供內容的理解和應用
3. 題目要真實、有教育意義，符合大學課程標準
4. 選項要合理且具有迷惑性，避免明顯錯誤的選項
5. 答案要正確且有詳細解釋，解釋要清晰易懂
6. 題目內容要符合{self.difficulty_levels[difficulty]}程度
7. {option_instruction}
8. 題目應該深入測試提供內容的核心概念

請務必以以下 JSON Schema 格式回傳（只生成一題）：

{{
  "question_text": "基於提供內容的題目",
  "options": [
    "選項A: 選項內容",
    "選項B: 選項內容",
    "選項C: 選項內容",
    "選項D: 選項內容"
  ],
  "correct_answer": {answer_format},
  "explanation": "詳細的解釋說明，包含與提供內容的關聯性",
  "key_points": "關鍵知識點, 與提供內容的關聯, 核心概念"
}}

重要提醒：
- 請確保JSON格式完整，不要中途截斷
- 所有字符串都要用雙引號包圍，不要使用單引號
- 選項數組必須包含4個元素，每個選項都要有標籤（A、B、C、D）
- 題目內容要專業且準確，完全基於提供的內容
- 請使用繁體中文撰寫所有內容
- 請嚴格按照上述JSON Schema格式，不要添加任何其他文字或格式
- 必須生成真實的題目內容，不要使用佔位符
- 題目應該深入測試提供內容的核心概念和應用
- 正確答案格式：{answer_format}"""

        return prompt

    def _build_dynamic_prompt(self, topic: str, difficulty: str, question_type: str, selected_text: str = None, requirements: Dict[str, Any] = None) -> str:
        """構建動態AI提示詞"""

        # 根據題型調整提示詞
        if question_type == 'single-choice':
            option_instruction = "提供4個選項，只有1個正確答案"
            answer_format = '"A"'
        elif question_type == 'multiple-choice':
            option_instruction = "提供4個選項，正確答案可以是1-3個，用逗號分隔（如：'A,C'）"
            answer_format = '"A,C"'
        else:
            option_instruction = "提供4個選項"
            answer_format = '"A"'

        # 構建更詳細的主題描述
        if requirements and 'domain_name' in requirements and 'concept_name' in requirements:
            domain_name = requirements['domain_name']
            concept_name = requirements['concept_name']
            detailed_topic = f"{domain_name}領域中的{concept_name}概念"
        else:
            detailed_topic = topic

        prompt = f"""請為我創建一道關於{detailed_topic}的{self.difficulty_levels[difficulty]}程度{self.question_types[question_type]}。

要求：
1. 題目要真實、有教育意義，符合大學課程標準
2. 選項要合理且具有迷惑性，避免明顯錯誤的選項
3. 答案要正確且有詳細解釋，解釋要清晰易懂
4. 題目內容要符合{self.difficulty_levels[difficulty]}程度
5. {option_instruction}
6. 題目必須緊密圍繞{detailed_topic}的核心概念和知識點
7. 如果提供了參考內容，題目應該與參考內容相關且具有相似性

請務必以以下 JSON Schema 格式回傳（只生成一題）：

{{
  "question_text": "在二元搜尋樹中，左子樹的所有節點值都必須滿足什麼條件？",
  "options": [
    "選項A: 大於根節點的值",
    "選項B: 小於根節點的值",
    "選項C: 等於根節點的值",
    "選項D: 與根節點值無關"
  ],
  "correct_answer": {answer_format},
  "explanation": "在二元搜尋樹中，左子樹的所有節點值都必須小於根節點的值，這是二元搜尋樹的基本性質。",
  "key_points": "二元搜尋樹, 左子樹性質, 節點值比較"
}}

重要提醒：
- 請確保JSON格式完整，不要中途截斷
- 所有字符串都要用雙引號包圍，不要使用單引號
- 選項數組必須包含4個元素，每個選項都要有標籤（A、B、C、D）
- 題目內容要專業且準確，符合{detailed_topic}學科標準
- 請使用繁體中文撰寫所有內容
- 請嚴格按照上述JSON Schema格式，不要添加任何其他文字或格式
- 必須生成真實的題目內容，不要使用佔位符
- 題目內容應該與{detailed_topic}相關，具有實際的教學價值
- 由於只生成一題，請確保JSON完整且不截斷
- 請根據{detailed_topic}創建全新的真實題目，不要複製示例內容
- 正確答案格式：{answer_format}
- 特別注意：題目必須是關於{detailed_topic}的，不要生成其他不相關的主題（如網路、作業系統等）"""

        return prompt

    def _extract_and_validate_single_question(self, response_text: str) -> Optional[Dict[str, Any]]:
        """提取和驗證單一題目的JSON"""
        try:
            logger.info(f"🔍 開始提取JSON，回應文本長度: {len(response_text)}")
            logger.info(f"🔍 回應文本前200字符: {response_text[:200]}")

            # 方法1: 尋找 ```json ... ``` 格式
            if '```json' in response_text:
                json_start = response_text.find('```json') + 7
                json_end = response_text.find('```', json_start)
                if json_end > json_start:
                    json_data = response_text[json_start:json_end].strip()
                    logger.info(f"✅ 找到JSON標記，提取的JSON: {json_data[:100]}...")

                    # 清理和驗證JSON
                    json_data = self._clean_json_string(json_data)
                    question_data = json.loads(json_data)

                    # 驗證題目數據
                    if self._validate_question_data(question_data):
                        return question_data

                # JSON標記不完整，嘗試修復
                json_data = response_text[json_start:].strip()
                logger.info(f"⚠️ JSON標記不完整，嘗試修復: {json_data[:100]}...")
                json_data = self._repair_truncated_json(json_data)
                if json_data:
                    try:
                        question_data = json.loads(json_data)
                        if self._validate_question_data(question_data):
                            return question_data
                    except:
                        logger.warning("❌ 修復後的JSON仍然無法解析")

            # 方法2: 尋找 { ... } 格式
            elif '{' in response_text and '}' in response_text:
                brace_start = response_text.find('{')
                brace_end = response_text.rfind('}')
                if brace_end > brace_start:
                    json_data = response_text[brace_start:brace_end + 1].strip()
                    logger.info(f"✅ 找到大括號，提取的JSON: {json_data[:100]}...")

                    # 清理和驗證JSON
                    json_data = self._clean_json_string(json_data)
                    question_data = json.loads(json_data)

                    if self._validate_question_data(question_data):
                        return question_data

                # 大括號不完整，嘗試修復
                json_data = response_text[brace_start:].strip()
                logger.info(f"⚠️ 大括號不完整，嘗試修復: {json_data[:100]}...")
                json_data = self._repair_truncated_json(json_data)
                if json_data:
                    try:
                        question_data = json.loads(json_data)
                        if self._validate_question_data(question_data):
                            return question_data
                    except:
                        logger.warning("❌ 修復後的JSON仍然無法解析")

            # 方法3: 嘗試直接解析整個回應
            else:
                logger.info("🔄 嘗試直接解析AI回應")
                # 清理回應內容
                cleaned_content = self._clean_json_string(response_text)
                question_data = json.loads(cleaned_content)

                if self._validate_question_data(question_data):
                    return question_data

            logger.warning("❌ 所有JSON提取方法都失敗")
            return None

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"❌ AI回應解析失敗: {e}")
            logger.warning(f"❌ AI回應內容: {response_text[:200]}...")
            return None

    def _clean_json_string(self, json_str: str) -> str:
        """清理JSON字符串，移除多餘的換行符和縮進"""
        # 移除開頭的 ```json 和結尾的 ```
        cleaned = json_str.strip()
        if cleaned.startswith('```json'):
            cleaned = cleaned[7:]
        if cleaned.endswith('```'):
            cleaned = cleaned[:-3]

        # 移除控制字符和無效字符
        # 移除控制字符（除了換行符和製表符）
        cleaned = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', cleaned)

        # 移除多餘的換行符和縮進
        cleaned = cleaned.replace('\n', ' ').replace('\r', ' ').replace('    ', ' ')

        # 移除多餘的空格
        cleaned = re.sub(r'\s+', ' ', cleaned)

        # 確保大括號和中括號的平衡
        if cleaned.count('{') > cleaned.count('}'):
            cleaned = cleaned.replace('{', '{\n', 1)
            cleaned = cleaned.replace('}', '\n}', 1)
        elif cleaned.count('{') == 1 and cleaned.count('}') == 0:
            cleaned += '\n}'
        elif cleaned.count('{') == 0 and cleaned.count('}') == 1:
            cleaned = '{\n' + cleaned

        if cleaned.count('[') > cleaned.count(']'):
            cleaned = cleaned.replace('[', '[\n', 1)
            cleaned = cleaned.replace(']', '\n]', 1)
        elif cleaned.count('[') == 1 and cleaned.count(']') == 0:
            cleaned += '\n]'
        elif cleaned.count('[') == 0 and cleaned.count(']') == 1:
            cleaned = '[\n' + cleaned

        # 移除開頭和結尾的空格
        cleaned = cleaned.strip()

        return cleaned

    def _repair_truncated_json(self, json_str: str) -> str:
        """修復被截斷的JSON字符串"""
        try:
            # 基本清理
            cleaned = json_str.strip()
            logger.info(f"🔧 開始修復截斷的JSON: {cleaned[:100]}...")

            # 如果JSON已經完整，直接返回
            try:
                json.loads(cleaned)
                logger.info("✅ JSON已經完整，無需修復")
                return cleaned
            except:
                pass

            # 檢查是否缺少結尾的大括號
            if cleaned.count('{') > cleaned.count('}'):
                logger.info("🔧 檢測到缺少結尾大括號，開始修復...")

                # 檢查最後一個字段是否完整
                if '"key_points"' in cleaned:
                    if not cleaned.endswith('"') and not cleaned.endswith('}'):
                        # 補全key_points字段
                        if cleaned.endswith(','):
                            cleaned = cleaned[:-1]  # 移除最後的逗號
                        cleaned += ': "關鍵知識點"'
                        logger.info("✅ 已補全key_points字段")

                if '"explanation"' in cleaned and not cleaned.endswith('"') and not cleaned.endswith('}'):
                    # 補全explanation字段
                    if cleaned.endswith(','):
                        cleaned = cleaned[:-1]  # 移除最後的逗號
                    cleaned += ': "詳細解釋"'
                    logger.info("✅ 已補全explanation字段")

                if '"correct_answer"' in cleaned and not cleaned.endswith('"') and not cleaned.endswith('}'):
                    # 補全correct_answer字段
                    if cleaned.endswith(','):
                        cleaned = cleaned[:-1]  # 移除最後的逗號
                    cleaned += ': "A"'
                    logger.info("✅ 已補全correct_answer字段")

                if '"options"' in cleaned and not cleaned.endswith(']'):
                    # 補全options字段
                    if not cleaned.endswith('"'):
                        cleaned += '"'
                    cleaned += ']'
                    logger.info("✅ 已補全options字段")

                if '"question_text"' in cleaned and not cleaned.endswith('"') and not cleaned.endswith('}'):
                    # 補全question_text字段
                    if cleaned.endswith(','):
                        cleaned = cleaned[:-1]  # 移除最後的逗號
                    cleaned += ': "題目內容"'
                    logger.info("✅ 已補全question_text字段")

                # 添加結尾大括號
                cleaned += '}'
                logger.info("✅ 已添加結尾大括號")

            # 檢查是否缺少結尾的中括號
            if cleaned.count('[') > cleaned.count(']'):
                cleaned += ']'
                logger.info("✅ 已補全結尾中括號")

            # 嘗試解析修復後的JSON
            try:
                json.loads(cleaned)
                logger.info(f"✅ JSON修復成功: {cleaned[:100]}...")
                return cleaned
            except:
                # 如果還是無法解析，嘗試更激進的修復
                logger.warning("⚠️ 基本修復失敗，嘗試激進修復")
                repaired = self._aggressive_json_repair(cleaned)
                if repaired:
                    return repaired
                else:
                    # 如果激進修復也失敗，返回None
                    logger.warning("❌ 所有JSON修復方法都失敗")
                    return None

        except Exception as e:
            logger.warning(f"❌ JSON修復失敗: {e}")
            return None

    def _aggressive_json_repair(self, json_str: str) -> str:
        """激進的JSON修復方法"""
        try:
            # 嘗試從原始字符串中提取可用的字段
            extracted_data = {}

            # 提取question_text
            if '"question_text"' in json_str:
                try:
                    start = json_str.find('"question_text"') + len('"question_text"')
                    start = json_str.find('"', start) + 1
                    end = json_str.find('"', start)
                    if end > start:
                        extracted_data["question_text"] = json_str[start:end]
                except:
                    pass

            # 提取options
            if '"options"' in json_str:
                try:
                    start = json_str.find('"options"') + len('"options"')
                    start = json_str.find('[', start)
                    if start > 0:
                        # 處理截斷的options
                        options_str = json_str[start+1:]
                        options = []
                        # 簡單提取選項
                        for i in range(4):
                            option_label = f"選項{chr(65 + i)}:"
                            if option_label in options_str:
                                option_start = options_str.find(option_label) + len(option_label)
                                option_end = options_str.find('"', option_start)
                                if option_end > option_start:
                                    option_content = options_str[option_start:option_end].strip()
                                    options.append(f"{option_label} {option_content}")
                                else:
                                    options.append(f"{option_label} 選項{chr(65 + i)}")
                            else:
                                options.append(f"{option_label} 選項{chr(65 + i)}")
                        extracted_data["options"] = options
                except:
                    pass

            # 提取correct_answer
            if '"correct_answer"' in json_str:
                try:
                    start = json_str.find('"correct_answer"') + len('"correct_answer"')
                    start = json_str.find('"', start) + 1
                    end = json_str.find('"', start)
                    if end > start:
                        extracted_data["correct_answer"] = json_str[start:end]
                except:
                    pass

            # 如果提取到了足夠的數據，使用提取的數據
            if len(extracted_data) >= 3:
                # 補充缺失的字段
                if "question_text" not in extracted_data:
                    extracted_data["question_text"] = "關於資料結構的問題"
                if "options" not in extracted_data:
                    extracted_data["options"] = [
                        "選項A: 選項A",
                        "選項B: 選項B",
                        "選項C: 選項C",
                        "選項D: 選項D"
                    ]
                if "correct_answer" not in extracted_data:
                    extracted_data["correct_answer"] = "A"
                if "explanation" not in extracted_data:
                    extracted_data["explanation"] = "這是一個關於資料結構的專業問題，需要深入理解相關概念和原理。"
                if "key_points" not in extracted_data:
                    extracted_data["key_points"] = "資料結構, 算法分析"

                return json.dumps(extracted_data, ensure_ascii=False)

            # 如果提取失敗，返回None而不是佔位符
            logger.warning("❌ 無法從截斷的JSON中提取有效數據")
            return None

        except Exception as e:
            logger.error(f"❌ 激進JSON修復失敗: {e}")
            # 返回最基本的JSON結構
            return '{"question_text": "題目內容", "options": ["選項A: 選項內容", "選項B: 選項內容", "選項C: 選項內容", "選項D: 選項內容"], "correct_answer": "A", "explanation": "詳細解釋", "key_points": "關鍵知識點"}'

    def _validate_question_data(self, question_data: Dict[str, Any]) -> bool:
        """驗證題目數據的完整性和正確性"""
        try:
            logger.info(f"🔍 開始驗證題目數據: {question_data}")

            # 驗證必要字段
            required_fields = ['question_text', 'options', 'correct_answer', 'explanation']
            for field in required_fields:
                if field not in question_data:
                    logger.warning(f"❌ 缺少必要字段: {field}")
                    logger.warning(f"現有字段: {list(question_data.keys())}")
                    return False

            # 檢查是否包含佔位符
            placeholder_patterns = [
                # 通用佔位符
                '題目內容', '選項內容', '詳細解釋', '關鍵知識點', '正確答案',
                # 具體佔位符（您遇到的問題）
                '相關概念A', '相關概念B', '相關概念C', '相關概念D',
                '概念A', '概念B', '概念C', '概念D'
                # 移除對"關於.*的問題"的檢查，因為這可能是正常的解釋內容
            ]

            for field, value in question_data.items():
                if isinstance(value, str):
                    # 檢查佔位符模式
                    for pattern in placeholder_patterns:
                        if re.search(pattern, value, re.IGNORECASE):
                            logger.warning(f"❌ 檢測到佔位符模式 '{pattern}' 在字段 '{field}' 中: {value}")
                            return False

                    # 檢查內容是否過於簡短或模糊
                    if field == 'question_text' and len(value.strip()) < 10:
                        logger.warning(f"❌ 題目內容過於簡短: {value}")
                        return False

                    if field == 'explanation' and len(value.strip()) < 20:
                        logger.warning(f"❌ 解釋內容過於簡短: {value}")
                        return False

                    # 檢查是否包含明顯的佔位符文字（更寬鬆的檢查）
                    if any(placeholder in value for placeholder in ['請寫出', '請創建', '請參考']):
                        logger.warning(f"❌ 檢測到指令性文字在字段 '{field}' 中: {value}")
                        return False

                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            # 檢查選項中的佔位符
                            for pattern in placeholder_patterns:
                                if re.search(pattern, item, re.IGNORECASE):
                                    logger.warning(f"❌ 檢測到佔位符模式 '{pattern}' 在字段 '{field}' 的列表中: {item}")
                                    return False

                            # 檢查選項是否過於簡短（但允許特殊格式如時間複雜度）
                            if field == 'options':
                                option_text = item.strip()
                                # 允許特殊格式：時間複雜度表示法、數學符號等
                                special_patterns = [
                                    r'^選項[A-D]:\s*O\([^)]+\)$',  # 時間複雜度
                                    r'^選項[A-D]:\s*[A-Za-z0-9\s\(\)\+\-\*/=<>≤≥]+$',  # 數學表達式
                                    r'^選項[A-D]:\s*[A-Za-z0-9\s]+$',  # 簡短但有效的答案
                                ]

                                is_special_format = any(re.match(pattern, option_text) for pattern in special_patterns)

                                if len(option_text) < 3 and not is_special_format:
                                    logger.warning(f"❌ 選項內容過於簡短: {item}")
                                    return False
                                elif len(option_text) < 5 and not is_special_format:
                                    logger.warning(f"⚠️ 選項內容較短，但可能是有效格式: {item}")
                                    # 不直接拒絕，而是記錄警告

            # 驗證選項數量
            if len(question_data.get('options', [])) != 4:
                logger.warning("❌ 選項數量必須是4個")
                return False

            # 驗證選項格式（確保每個選項都有標籤）
            options = question_data.get('options', [])
            for i, option in enumerate(options):
                if not option.strip():
                    logger.warning(f"❌ 選項{i+1}不能為空")
                    return False

                # 檢查選項是否包含標籤
                option_text = option.strip()
                if any(option_text.startswith(f"選項{label}") for label in ['A', 'B', 'C', 'D']):
                    logger.info(f"✅ 選項{i+1}標籤正確: {option_text[:20]}...")
                else:
                    logger.warning(f"⚠️ 選項{i+1}缺少標籤: {option_text}")
                    # 自動修復標籤
                    if i < len(['A', 'B', 'C', 'D']):
                        label = ['A', 'B', 'C', 'D'][i]
                        question_data['options'][i] = f"選項{label}: {option_text}"
                        logger.info(f"✅ 已修復選項{i+1}標籤: 選項{label}: {option_text[:20]}...")

            logger.info(f"✅ 題目數據驗證成功: {question_data['question_text'][:50]}...")
            return True

        except Exception as e:
            logger.error(f"❌ 題目數據驗證失敗: {e}")
            return False

    def _generate_content_based_questions(self, requirements: Dict[str, Any]) -> List[Dict[str, Any]]:
        """基於內容生成題目"""
        questions = []
        selected_text = requirements.get('selected_text', '')
        question_count = requirements['question_count']
        difficulty = requirements['difficulty']
        question_types = requirements['question_types']

        logger.info(f"🎯 開始基於內容生成題目，內容長度: {len(selected_text)} 字符")

        # 逐題生成，每題都有重試機制
        for i in range(question_count):
            question_type = random.choice(question_types)
            logger.info(f"🔄 正在生成第 {i + 1}/{question_count} 題，題型: {question_type}")

            # 基於內容生成單題，帶重試機制
            question = self._smart_generate_content_based_question(
                question_number=i + 1,
                selected_text=selected_text,
                difficulty=difficulty,
                question_type=question_type
            )

            if question:
                questions.append(question)
                logger.info(f"✅ 第 {i + 1} 題生成成功")
            else:
                logger.warning(f"⚠️ 第 {i + 1} 題生成失敗，跳過此題")

            # 每題之間稍作延遲，避免API限制
            if i < question_count - 1:
                time.sleep(1)

        logger.info(f"🎯 基於內容的題目生成完成，成功生成 {len(questions)} 題")
        return questions

    def _generate_pastexam_questions(self, requirements: Dict[str, Any]) -> List[Dict[str, Any]]:
        """生成考古題目"""
        questions = []
        school = requirements['school']
        year = requirements['year']
        department = requirements['department']
        question_count = requirements['question_count']

        # 這裡可以從數據庫查詢真實的考古題，目前使用模擬數據
        for i in range(question_count):
            question = self._create_sample_pastexam_question(
                question_number=i + 1,
                school=school,
                year=year,
                department=department
            )
            questions.append(question)

        return questions

    def _create_sample_pastexam_question(self, question_number: int, school: str,
                                        year: str, department: str) -> Dict[str, Any]:
        """創建示例考古題"""
        return {
            'id': question_number,
            'question_text': f"{school} {year}年 {department}考古題 {question_number}：關於程式設計的基本概念",
            'type': 'single-choice',
            'options': [
                "選項A: 程式設計基礎概念A",
                "選項B: 程式設計基礎概念B",
                "選項C: 程式設計基礎概念C",
                "選項D: 程式設計基礎概念D"
            ],
            'correct_answer': 'A',
            'topic': f"{school}考古題",
            'difficulty': 'medium',
            'key_points': f"{school} {year}年考點",
            'explanation': f"這是{school} {year}年的真實考題，考察程式設計的基本概念...",
            'image_file': []
        }

    def _generate_quiz_info(self, requirements: Dict[str, Any], questions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """生成考卷信息"""
        if requirements['exam_type'] == 'pastexam':
            title = f"{requirements['school']} {requirements['year']}年 {requirements['department']}考古題"
        elif requirements['exam_type'] == 'content-based':
            title = f"基於內容的AI生成測驗"
        else:
            title = f"{requirements['topic']}知識點測驗"

        return {
            'title': title,
            'exam_type': requirements['exam_type'],
            'topic': requirements.get('topic', '基於內容生成'),
            'difficulty': requirements['difficulty'],
            'question_count': len(questions),
            'time_limit': 60,  # 默認60分鐘
            'total_score': len(questions) * 5,  # 每題5分
            'created_at': datetime.now().isoformat(),
            'selected_text': requirements.get('selected_text', '') if requirements['exam_type'] == 'content-based' else None
        }

# 創建全局實例
quiz_generator = SmartQuizGenerator()

def generate_quiz_by_ai(requirements: Dict[str, Any]) -> Dict[str, Any]:
    """AI考卷生成的便捷函數"""
    return quiz_generator.generate_quiz(requirements)

def generate_and_save_quiz_by_ai(requirements: Dict[str, Any]) -> Dict[str, Any]:
    """AI考卷生成並保存到數據庫的便捷函數"""
    return quiz_generator.generate_and_save_quiz(requirements)

def get_available_topics() -> List[str]:
    """獲取可用的知識點列表"""
    return [
        "計算機概論", "程式設計", "資料結構", "演算法",
        "作業系統", "資料庫系統", "網路概論", "軟體工程",
        "人工智慧", "機器學習", "資料科學", "資訊安全"
    ]

def get_available_schools() -> List[str]:
    """獲取可用的學校列表"""
    return [
        "台大", "清大", "交大", "成大", "政大",
        "中央", "中興", "中山", "中正", "台科大"
    ]

def get_available_years() -> List[str]:
    """獲取可用的年份列表"""
    current_year = datetime.now().year
    return [str(year) for year in range(current_year - 5, current_year + 1)]

def get_available_departments() -> List[str]:
    """獲取可用的科系列表"""
    return [
        "資訊工程學系", "資訊管理學系", "資訊科學學系",
        "電機工程學系", "電子工程學系", "通訊工程學系"
    ]


def _parse_quiz_requirements(text: str) -> dict:
    """從文本中解析考卷需求"""
    requirements = {
        'topic': '計算機概論',
        'question_types': ['single-choice', 'multiple-choice'],
        'difficulty': 'medium',
        'question_count': 1,  # 改為1題默認，避免強制5題
        'exam_type': 'knowledge'
    }

    text_lower = text.lower()

    # 檢測是否為基於內容的生成請求
    content_keywords = ['根據以下內容', '基於以下內容', '根據內容', '基於內容', '以下內容', '內容如下']

    # 智能檢測：如果文本包含具體的技術內容且沒有明確的題目生成指令，則視為基於內容的請求
    technical_content_indicators = [
        '進位系統', '二進制', '八進制', '十六進制', '十進制',
        '數字表示', '數值轉換', '位元', '位元組',
        '演算法', '資料結構', '程式設計', '作業系統',
        '記憶體', 'CPU', '硬體', '軟體'
    ]

    # 明確的題目生成指令
    quiz_generation_keywords = ['生成', '創建', '建立', '製作', '產生', '考卷', '測驗', '題目', '考試']

    # 檢查是否包含明確的題目生成指令
    has_quiz_generation_keyword = any(keyword in text for keyword in quiz_generation_keywords)

    # 檢查是否包含技術內容
    has_technical_content = any(indicator in text for indicator in technical_content_indicators)

    # 如果包含明確的內容關鍵詞，直接視為基於內容的請求
    if any(keyword in text for keyword in content_keywords):
        requirements['exam_type'] = 'content-based'
        requirements['selected_text'] = text
        logger.info(f"🎯 檢測到基於內容的生成請求（明確關鍵詞）")
        return requirements

    # 如果包含技術內容但沒有明確的題目生成指令，視為基於內容的請求
    elif has_technical_content and not has_quiz_generation_keyword:
        requirements['exam_type'] = 'content-based'
        requirements['selected_text'] = text
        logger.info(f"🎯 檢測到基於內容的生成請求（技術內容檢測）")
        return requirements

    # 檢測知識點
    topics = ['計算機概論', '程式設計', '資料結構', '演算法', '作業系統', '資料庫', '網路', '軟體工程', '人工智慧', '機器學習']
    for topic in topics:
        if topic in text:
            requirements['topic'] = topic
            break

    # 檢測題型
    if '單選' in text or '選擇' in text:
        requirements['question_types'] = ['single-choice']
    elif '多選' in text:
        requirements['question_types'] = ['multiple-choice']
    elif '填空' in text:
        requirements['question_types'] = ['fill-in-the-blank']
    elif '是非' in text or '判斷' in text:
        requirements['question_types'] = ['true-false']
    elif '簡答' in text:
        requirements['question_types'] = ['short-answer']
    elif '申論' in text:
        requirements['question_types'] = ['long-answer']

    # 檢測難度
    if '簡單' in text or 'easy' in text_lower:
        requirements['difficulty'] = 'easy'
    elif '困難' in text or 'hard' in text_lower:
        requirements['difficulty'] = 'hard'

    # 檢測題目數量
    import re

    # 先支援中文數字（如「十題」「十五題」「二十題」）
    def chinese_numeral_to_int(s: str) -> int:
        mapping = {"零":0, "〇":0, "一":1, "二":2, "兩":2, "三":3, "四":4, "五":5, "六":6, "七":7, "八":8, "九":9}
        unit = {"十":10, "百":100, "千":1000, "萬":10000}
        total = 0
        num = 0
        last_unit = 1
        i = 0
        while i < len(s):
            ch = s[i]
            if ch in mapping:
                num = mapping[ch]
                i += 1
            elif ch in unit:
                u = unit[ch]
                if num == 0:
                    num = 1  # 如「十」= 10
                total += num * u
                num = 0
                last_unit = u
                i += 1
            else:
                i += 1
        return total + num

    zh_match = re.search(r'([零〇一二兩三四五六七八九十百千萬]+)\s*(題|提|道|個|條|項)', text)
    if zh_match:
        try:
            cnt = chinese_numeral_to_int(zh_match.group(1))
            if cnt > 0:
                requirements['question_count'] = cnt
        except Exception:
            pass
    else:
        # 支援阿拉伯數字表達方式：1題、1提、1道、1個等
        count_patterns = [
            r'(\d+)題',  # 1題
            r'(\d+)提',  # 1提
            r'(\d+)道',  # 1道
            r'(\d+)個',  # 1個
            r'(\d+)條',  # 1條
            r'(\d+)項',  # 1項
        ]
        for pattern in count_patterns:
            count_match = re.search(pattern, text)
            if count_match:
                requirements['question_count'] = int(count_match.group(1))
                break

    # 檢測考古題
    schools = ['台大', '清大', '交大', '成大', '政大', '中央', '中興', '中山', '中正', '台科大']
    for school in schools:
        if school in text:
            requirements['exam_type'] = 'pastexam'
            requirements['school'] = school
            break

    # 檢測年份
    year_match = re.search(r'(\d{4})年', text)
    if year_match:
        requirements['year'] = year_match.group(1)

    return requirements

def _is_quiz_generation_request(text: str) -> bool:
    """檢查是否為考卷生成請求"""
    quiz_keywords = [
        '創建', '生成', '建立', '製作', '產生',
        '考卷', '測驗', '題目', '考試', '練習',
        '單選題', '多選題', '填空題', '是非題', '簡答題', '申論題'
    ]

    text_lower = text.lower()
    return any(keyword in text_lower for keyword in quiz_keywords)

def create_quiz_generator_tool():
    """創建考卷生成工具"""
    from langchain_core.tools import tool

    @tool
    def quiz_generator_tool(requirements: str) -> str:
        """考卷生成工具，根據用戶需求自動創建考卷並保存到數據庫"""
        try:
            # 解析用戶需求
            try:
                # 嘗試解析JSON格式的需求
                req_dict = json.loads(requirements)
            except:
                # 如果不是JSON，嘗試從文本中提取信息
                req_dict = _parse_quiz_requirements(requirements)

            # 生成考卷並保存到數據庫
            result = generate_and_save_quiz_by_ai(req_dict)

            if result['success']:
                quiz_info = result['quiz_info']
                questions = result['questions']
                database_ids = result.get('database_ids', [])

                # 返回可跳轉的考卷數據
                quiz_data = {
                    'quiz_id': f"ai_generated_{int(time.time())}",
                    'template_id': f"ai_template_{int(time.time())}",
                    'questions': questions,
                    'time_limit': quiz_info['time_limit'],
                    'quiz_info': quiz_info,
                    'database_ids': database_ids
                }

                # 簡化回應格式，只返回考卷 ID
                response = f"✅ 考卷生成成功！\n\n"
                response += f"📝 **{quiz_info['title']}**\n"
                response += f"📚 主題: {quiz_info['topic']}\n"
                response += f"🔢 題目數量: {quiz_info['question_count']} 題\n"
                response += f"⏱️ 時間限制: {quiz_info['time_limit']} 分鐘\n\n"

                # 顯示第一題預覽
                if questions:
                    first_question = questions[0]
                    response += "📋 題目預覽:\n"
                    response += f"1. {first_question['question_text'][:80]}...\n\n"

                # 使用第一個數據庫 ID 作為考卷 ID
                quiz_id = database_ids[0] if database_ids else f"ai_generated_{int(time.time())}"

                response += "🚀 **開始測驗**\n\n"
                response += f"📋 考卷ID: `{quiz_id}`"

                return response
            else:
                return f"❌ 考卷生成失敗: {result.get('error', '未知錯誤')}"

        except Exception as e:
            logger.error(f"❌ 考卷生成工具執行失敗: {e}")
            return f"❌ 考卷生成失敗，請稍後再試。錯誤: {str(e)}"

    return quiz_generator_tool

def execute_quiz_generation(requirements: str) -> str:
    """
    執行考卷生成的主要函數 - 供外部調用

    Args:
        requirements: 用戶需求字符串（JSON格式或自然語言）

    Returns:
        格式化的回應字符串
    """
    try:
        logger.info(f"🔍 開始執行考卷生成，需求: {requirements[:100]}...")

        # 解析用戶需求
        try:
            # 嘗試解析JSON格式的需求
            req_dict = json.loads(requirements)
            logger.info("🔍 成功解析JSON格式需求")
        except:
            # 如果不是JSON，嘗試從文本中提取信息
            logger.info("🔍 嘗試從文本中提取需求信息")
            req_dict = _parse_quiz_requirements(requirements)
            logger.info(f"🔍 解析後的需求: {req_dict}")

        # 生成考卷並保存到數據庫
        logger.info("🔍 開始生成考卷並保存到數據庫...")
        result = generate_and_save_quiz_by_ai(req_dict)
        logger.info(f"🔍 考卷生成結果: success={result.get('success', False)}")

        if result['success']:
            quiz_info = result['quiz_info']
            questions = result['questions']
            database_ids = result.get('database_ids', [])

            # 返回可跳轉的考卷數據
            quiz_data = {
                'quiz_id': f"ai_generated_{int(time.time())}",
                'template_id': f"ai_template_{int(time.time())}",
                'questions': questions,
                'time_limit': quiz_info['time_limit'],
                'quiz_info': quiz_info,
                'database_ids': database_ids
            }

            # 簡化回應格式，只返回考卷 ID
            response = f"✅ 考卷生成成功！\n\n"
            response += f"📝 **{quiz_info['title']}**\n"
            response += f"📚 主題: {quiz_info['topic']}\n"
            response += f"🔢 題目數量: {quiz_info['question_count']} 題\n"
            response += f"⏱️ 時間限制: {quiz_info['time_limit']} 分鐘\n\n"

            # 顯示第一題預覽
            if questions:
                first_question = questions[0]
                response += "📋 題目預覽:\n"
                response += f"1. {first_question['question_text'][:80]}...\n\n"

            # 使用第一個數據庫 ID 作為考卷 ID
            quiz_id = database_ids[0] if database_ids else f"ai_generated_{int(time.time())}"

            response += "🚀 **開始測驗**\n\n"
            response += f"📋 考卷ID: `{quiz_id}`"

            return response
        else:
            return f"❌ 考卷生成失敗: {result.get('error', '未知錯誤')}"

    except Exception as e:
        logger.error(f"❌ 考卷生成執行失敗: {e}")
        return f"❌ 考卷生成失敗，請稍後再試。錯誤: {str(e)}"

def execute_content_based_quiz_generation(content: str) -> str:
    """
    執行基於內容的考卷生成 - 供外部調用

    Args:
        content: 用戶提供的內容字符串

    Returns:
        格式化的回應字符串
    """
    try:
        logger.info(f"🎯 開始基於內容的考卷生成，內容長度: {len(content)} 字符")

        # 構建基於內容的需求
        requirements = {
            'exam_type': 'content-based',
            'selected_text': content,
            'topic': '基於內容生成',
            'difficulty': 'medium',
            'question_count': 1,
            'question_types': ['single-choice', 'multiple-choice']
        }

        # 生成考卷並保存到數據庫
        result = generate_and_save_quiz_by_ai(requirements)

        if result['success']:
            quiz_info = result['quiz_info']
            questions = result['questions']
            database_ids = result.get('database_ids', [])

            # 簡化回應格式，只返回考卷 ID
            response = f"✅ 基於內容的考卷生成成功！\n\n"
            response += f"📝 **{quiz_info['title']}**\n"
            response += f"📚 基於內容: {content[:50]}...\n"
            response += f"🎯 主題: {quiz_info['topic']}\n"
            response += f"🔢 題目數量: {quiz_info['question_count']} 題\n"
            response += f"⏱️ 時間限制: {quiz_info['time_limit']} 分鐘\n"
            response += f"🏷️ 生成類型: 基於內容\n\n"

            # 顯示第一題預覽
            if questions:
                first_question = questions[0]
                response += "📋 題目預覽:\n"
                response += f"1. {first_question['question_text'][:80]}...\n\n"

            # 使用第一個數據庫 ID 作為考卷 ID
            quiz_id = database_ids[0] if database_ids else f"content_based_{int(time.time())}"

            response += "🚀 **開始測驗**\n\n"
            response += f"📋 考卷ID: `{quiz_id}`"

            return response
        else:
            return f"❌ 基於內容的考卷生成失敗: {result.get('error', '未知錯誤')}"

    except Exception as e:
        logger.error(f"❌ 基於內容的考卷生成執行失敗: {e}")
        return f"❌ 基於內容的考卷生成失敗，請稍後再試。錯誤: {str(e)}"

class SimilarQuizGenerator:
    """相似題目生成器 - 專門生成與選中文字相似的題目"""

    def __init__(self):
        self.question_types = {
            'single-choice': '單選題',
            'multiple-choice': '多選題',
            'fill-in-the-blank': '填空題',
            'true-false': '是非題',
            'short-answer': '簡答題'
        }

        self.difficulty_levels = {
            'easy': '簡單',
            'medium': '中等',
            'hard': '困難'
        }

        # 重試配置
        self.max_retries = 3
        self.retry_delay = 2  # 秒

    def generate_similar_quiz(self, selected_text: str) -> Dict[str, Any]:
        """
        根據選中的文字生成相似的題目

        Args:
            selected_text: 用戶選中的文字內容

        Returns:
            生成的考卷結果字典
        """
        try:
            logger.info(f"🎯 開始生成相似題目，選中文字: {selected_text[:50]}...")

            # 分析選中文字的內容
            topic = self._extract_topic_from_text(selected_text)
            difficulty = self._determine_difficulty_from_text(selected_text)
            question_type = self._select_appropriate_question_type(selected_text)

            logger.info(f"📝 分析結果 - 主題: {topic}, 難度: {difficulty}, 題型: {question_type}")

            # 生成相似題目
            question = self._generate_similar_question(selected_text, topic, difficulty, question_type)

            if not question:
                return {
                    'success': False,
                    'error': '相似題目生成失敗'
                }

            # 構建考卷信息
            quiz_info = {
                'title': f"基於「{topic}」的相似題目測驗",
                'topic': topic,
                'difficulty': difficulty,
                'question_count': 1,
                'time_limit': 60,
                'selected_text': selected_text,
                'generation_type': 'similar'  # 標記為相似題目生成
            }

            # 保存到數據庫
            database_ids = self._save_similar_question_to_database([question], quiz_info)

            return {
                'success': True,
                'questions': [question],
                'quiz_info': quiz_info,
                'database_ids': database_ids,
                'generation_type': 'similar'
            }

        except Exception as e:
            logger.error(f"❌ 相似題目生成失敗: {e}")
            return {
                'success': False,
                'error': f'相似題目生成失敗: {str(e)}'
            }

    def _generate_similar_question(self, selected_text: str, topic: str, difficulty: str, question_type: str) -> Optional[Dict[str, Any]]:
        """生成單一相似題目"""

        for attempt in range(self.max_retries):
            try:
                logger.info(f"🔄 相似題目生成，第 {attempt + 1} 次嘗試")

                # 初始化LLM
                from langchain_google_genai import ChatGoogleGenerativeAI
                import sys
                import os

                # 添加tool目錄到路徑
                tool_path = os.path.join(os.path.dirname(__file__), '..', 'tool')
                if tool_path not in sys.path:
                    sys.path.append(tool_path)

                from api_keys import get_api_key

                # 初始化LLM
                from accessories import init_ai

                # 預設使用 Ollama Qwen
                llm = init_ai(ai_type='ollama')

                # 構建相似題目專用的提示詞
                prompt = self._build_similar_question_prompt(selected_text, topic, difficulty, question_type)

                # 調用AI生成
                response = llm.invoke(prompt)
                response_text = response.content if hasattr(response, 'content') else str(response)

                logger.info(f"📝 相似題目AI回應長度: {len(response_text)} 字符")

                if not response_text or len(response_text.strip()) == 0:
                    logger.error("❌ 相似題目AI回應為空！")
                    if attempt < self.max_retries - 1:
                        time.sleep(self.retry_delay)
                        continue
                    else:
                        return None

                # 提取和驗證JSON
                question_data = self._extract_and_validate_similar_question(response_text)

                if question_data:
                    logger.info(f"✅ 相似題目生成成功")
                    # 添加題目信息
                    question_data['id'] = 1
                    question_data['type'] = question_type
                    question_data['topic'] = topic
                    question_data['difficulty'] = difficulty
                    question_data['image_file'] = []
                    question_data['generation_type'] = 'similar'  # 標記為相似題目

                    return question_data
                else:
                    logger.warning(f"⚠️ 相似題目JSON提取失敗")
                    if attempt < self.max_retries - 1:
                        time.sleep(self.retry_delay)
                        continue
                    else:
                        return None

            except Exception as e:
                logger.error(f"❌ 相似題目生成錯誤: {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                    continue
                else:
                    return None

        return None

    def _build_similar_question_prompt(self, selected_text: str, topic: str, difficulty: str, question_type: str) -> str:
        """構建相似題目專用的提示詞"""

        # 根據題型調整提示詞
        if question_type == 'single-choice':
            option_instruction = "提供4個選項，只有1個正確答案"
            answer_format = '"A"'
        elif question_type == 'multiple-choice':
            option_instruction = "提供4個選項，正確答案可以是1-3個，用逗號分隔（如：'A,C'）"
            answer_format = '"A,C"'
        else:
            option_instruction = "提供4個選項"
            answer_format = '"A"'

        prompt = f"""請基於以下選中的文字內容，創建一道與之相關且相似的{self.difficulty_levels[difficulty]}程度{self.question_types[question_type]}。

選中的文字內容：
「{selected_text}」

要求：
1. 題目必須與選中文字的內容主題相關
2. 題目應該測試對選中文字內容的理解和應用
3. 可以擴展、深化或變換選中文字的知識點
4. 題目要真實、有教育意義，符合大學課程標準
5. 選項要合理且具有迷惑性，避免明顯錯誤的選項
6. 答案要正確且有詳細解釋，解釋要清晰易懂
7. 題目內容要符合{self.difficulty_levels[difficulty]}程度
8. {option_instruction}
9. 題目應該與選中文字有相似性，但不要完全相同

請務必以以下 JSON Schema 格式回傳：

{{
  "question_text": "基於選中文字內容的相似題目",
  "options": [
    "選項A: 選項內容",
    "選項B: 選項內容",
    "選項C: 選項內容",
    "選項D: 選項內容"
  ],
  "correct_answer": {answer_format},
  "explanation": "詳細的解釋說明，包含與選中文字的關聯性",
  "key_points": "關鍵知識點, 與選中文字的關聯, 相似概念"
}}

重要提醒：
- 請確保JSON格式完整，不要中途截斷
- 所有字符串都要用雙引號包圍，不要使用單引號
- 選項數組必須包含4個元素，每個選項都要有標籤（A、B、C、D）
- 題目內容要專業且準確，與選中文字相關
- 請使用繁體中文撰寫所有內容
- 請嚴格按照上述JSON Schema格式，不要添加任何其他文字或格式
- 必須生成真實的題目內容，不要使用佔位符
- 題目應該與選中文字有相似性，測試相關的知識點
- 正確答案格式：{answer_format}"""

        return prompt

    def _extract_and_validate_similar_question(self, response_text: str) -> Optional[Dict[str, Any]]:
        """提取和驗證相似題目的JSON"""
        try:
            logger.info(f"🔍 開始提取相似題目JSON，回應文本長度: {len(response_text)}")

            # 方法1: 尋找 ```json ... ``` 格式
            if '```json' in response_text:
                start = response_text.find('```json') + 7
                end = response_text.find('```', start)
                if end != -1:
                    json_text = response_text[start:end].strip()
                    logger.info(f"🔍 找到```json```格式，JSON長度: {len(json_text)}")
                else:
                    logger.warning("⚠️ 找到```json開始但沒有結束標記")
                    return None
            # 方法2: 尋找 { ... } 格式
            elif '{' in response_text and '}' in response_text:
                start = response_text.find('{')
                end = response_text.rfind('}') + 1
                json_text = response_text[start:end]
                logger.info(f"🔍 找到{{}}格式，JSON長度: {len(json_text)}")
            else:
                logger.warning("⚠️ 沒有找到有效的JSON格式")
                return None

            # 解析JSON
            question_data = json.loads(json_text)
            logger.info(f"✅ 相似題目JSON解析成功")

            # 驗證必要字段
            required_fields = ['question_text', 'options', 'correct_answer', 'explanation']
            for field in required_fields:
                if field not in question_data:
                    logger.warning(f"⚠️ 缺少必要字段: {field}")
                    return None

            # 驗證選項數量
            if len(question_data['options']) != 4:
                logger.warning(f"⚠️ 選項數量不正確: {len(question_data['options'])}")
                return None

            logger.info(f"✅ 相似題目驗證通過")
            return question_data

        except json.JSONDecodeError as e:
            logger.error(f"❌ 相似題目JSON解析失敗: {e}")
            logger.error(f"❌ 失敗的JSON文本: {json_text[:200]}...")
            return None
        except Exception as e:
            logger.error(f"❌ 相似題目提取過程發生錯誤: {e}")
            return None

    def _extract_topic_from_text(self, text: str) -> str:
        """從選中的文字中提取主題"""
        # 定義常見的計算機概論主題關鍵詞
        topic_keywords = {
            '作業系統': ['作業系統', '操作系統', 'OS', '進程', '執行緒', '記憶體管理', '檔案系統'],
            '資料結構': ['資料結構', '數據結構', '陣列', '鏈表', '堆疊', '佇列', '樹', '圖'],
            '演算法': ['演算法', '算法', '排序', '搜尋', '遞迴', '動態規劃', '貪心'],
            '程式設計': ['程式設計', '編程', '程式語言', 'C++', 'Java', 'Python', '函數', '變數'],
            '資料庫': ['資料庫', '數據庫', 'SQL', '關聯式', '正規化', '索引', '交易'],
            '網路': ['網路', '網絡', 'TCP', 'IP', 'HTTP', '協定', '路由', '防火牆'],
            '數位邏輯': ['數位邏輯', '數位電路', '邏輯閘', '布林', 'AND', 'OR', 'NOT', '0', '1'],
            '計算機概論': ['計算機概論', '電腦概論', '資訊概論', '硬體', '軟體', 'CPU', '記憶體']
        }

        text_lower = text.lower()

        # 檢查每個主題的關鍵詞
        for topic, keywords in topic_keywords.items():
            for keyword in keywords:
                if keyword.lower() in text_lower:
                    return topic

        # 如果沒有找到特定主題，返回通用主題
        return '計算機概論'

    def _determine_difficulty_from_text(self, text: str) -> str:
        """根據選中文字的複雜度確定難度"""
        text_length = len(text)

        # 定義難度關鍵詞
        easy_keywords = ['基本', '簡單', '基礎', '入門', '介紹']
        hard_keywords = ['複雜', '進階', '高級', '深度', '詳細', '分析', '設計', '實作']

        text_lower = text.lower()

        # 檢查難度關鍵詞
        for keyword in hard_keywords:
            if keyword in text_lower:
                return 'hard'

        for keyword in easy_keywords:
            if keyword in text_lower:
                return 'easy'

        # 根據文字長度判斷
        if text_length < 50:
            return 'easy'
        elif text_length < 150:
            return 'medium'
        else:
            return 'hard'

    def _select_appropriate_question_type(self, text: str) -> str:
        """根據選中文字的內容選擇合適的題型"""
        text_lower = text.lower()

        # 根據內容特徵選擇題型
        if any(keyword in text_lower for keyword in ['比較', '對比', '差異', '相同', '不同']):
            return 'multiple-choice'
        elif any(keyword in text_lower for keyword in ['定義', '什麼是', '何謂', '概念']):
            return 'single-choice'
        elif any(keyword in text_lower for keyword in ['步驟', '過程', '流程', '方法']):
            return 'single-choice'
        else:
            return 'single-choice'  # 默認單選題

    def _save_similar_question_to_database(self, questions: List[Dict], quiz_info: Dict) -> List[str]:
        """將相似題目保存到MongoDB數據庫"""
        try:
            from accessories import mongo

            # 檢查 mongo 對象是否可用
            if mongo is None or mongo.db is None:
                logger.warning("⚠️ MongoDB 連接不可用")
                return []

            # 創建完整的考卷文檔
            quiz_id = f"similar_quiz_{int(time.time())}"
            quiz_doc = {
                "_id": quiz_id,  # 直接使用quiz_id作為_id
                "quiz_id": quiz_id,
                "title": quiz_info['title'],
                "type": "similar_quiz",  # 標記為相似題目
                "creator_email": "ai_system@mis_teach.com",
                "create_time": datetime.now().isoformat(),
                "time_limit": quiz_info['time_limit'],
                "questions": questions,
                "metadata": {
                    "topic": quiz_info['topic'],
                    "difficulty": quiz_info['difficulty'],
                    "question_count": len(questions),
                    "selected_text": quiz_info['selected_text'],
                    "generation_type": "similar"
                }
            }

            # 保存到數據庫
            result = mongo.db.exam.insert_one(quiz_doc)

            if result.inserted_id:
                logger.info(f"✅ 相似題目已保存到數據庫，ID: {result.inserted_id}")

                # 創建SQL template
                template_id = create_sql_template_for_quiz(quiz_id, quiz_doc)
                logger.info(f"📋 SQL template已創建: {template_id}")

                return [quiz_id]  # 返回我們設置的quiz_id
            else:
                logger.error("❌ 保存相似題目到數據庫失敗")
                return []

        except Exception as e:
            logger.error(f"❌ 保存相似題目到數據庫失敗: {e}")
            return []

def generate_similar_quiz_from_text(selected_text: str) -> str:
    """
    根據選中的文字生成相似的題目 - 使用新的SimilarQuizGenerator

    Args:
        selected_text: 用戶選中的文字內容

    Returns:
        生成的考卷信息字符串
    """
    try:
        logger.info(f"🎯 開始使用SimilarQuizGenerator生成相似題目: {selected_text[:50]}...")

        # 創建相似題目生成器
        similar_generator = SimilarQuizGenerator()

        # 生成相似題目
        result = similar_generator.generate_similar_quiz(selected_text)

        if result['success']:
            questions = result['questions']
            quiz_info = result['quiz_info']
            database_ids = result.get('database_ids', [])

            # 簡化回應格式，只返回考卷 ID
            response = f"✅ 相似題目生成成功！\n\n"
            response += f"📝 **{quiz_info['title']}**\n"
            response += f"📚 基於內容: {selected_text[:50]}...\n"
            response += f"🎯 主題: {quiz_info['topic']}\n"
            response += f"🔢 題目數量: {quiz_info['question_count']} 題\n"
            response += f"⏱️ 時間限制: {quiz_info['time_limit']} 分鐘\n"
            response += f"🏷️ 生成類型: 相似題目\n\n"

            # 顯示第一題預覽
            if questions:
                first_question = questions[0]
                response += "📋 題目預覽:\n"
                response += f"1. {first_question['question_text'][:80]}...\n\n"

            # 使用第一個數據庫 ID 作為考卷 ID
            quiz_id = database_ids[0] if database_ids else f"similar_quiz_{int(time.time())}"

            response += "🚀 **開始測驗**\n\n"
            response += f"📋 考卷ID: `{quiz_id}`"

            return response
        else:
            return f"❌ 相似題目生成失敗: {result.get('error', '未知錯誤')}"

    except Exception as e:
        logger.error(f"❌ 相似題目生成執行失敗: {e}")
        return f"❌ 相似題目生成失敗，請稍後再試。錯誤: {str(e)}"

def _extract_topic_from_text(text: str) -> str:
    """
    從選中的文字中提取主題

    Args:
        text: 選中的文字

    Returns:
        提取的主題
    """
    # 定義常見的計算機概論主題關鍵詞
    topic_keywords = {
        '作業系統': ['作業系統', '操作系統', 'OS', '進程', '執行緒', '記憶體管理', '檔案系統'],
        '資料結構': ['資料結構', '數據結構', '陣列', '鏈表', '堆疊', '佇列', '樹', '圖'],
        '演算法': ['演算法', '算法', '排序', '搜尋', '遞迴', '動態規劃', '貪心'],
        '程式設計': ['程式設計', '編程', '程式語言', 'C++', 'Java', 'Python', '函數', '變數'],
        '資料庫': ['資料庫', '數據庫', 'SQL', '關聯式', '正規化', '索引', '交易'],
        '網路': ['網路', '網絡', 'TCP', 'IP', 'HTTP', '協定', '路由', '防火牆'],
        '數位邏輯': ['數位邏輯', '數位電路', '邏輯閘', '布林', 'AND', 'OR', 'NOT', '0', '1'],
        '計算機概論': ['計算機概論', '電腦概論', '資訊概論', '硬體', '軟體', 'CPU', '記憶體']
    }

    text_lower = text.lower()

    # 檢查每個主題的關鍵詞
    for topic, keywords in topic_keywords.items():
        for keyword in keywords:
            if keyword.lower() in text_lower:
                return topic

    # 如果沒有找到特定主題，返回通用主題
    return '計算機概論'

def _determine_difficulty_from_text(text: str) -> str:
    """
    根據選中文字的複雜度確定難度

    Args:
        text: 選中的文字

    Returns:
        難度等級 ('easy', 'medium', 'hard')
    """
    # 簡單的難度判斷邏輯
    text_length = len(text)

    # 定義難度關鍵詞
    easy_keywords = ['基本', '簡單', '基礎', '入門', '介紹']
    hard_keywords = ['複雜', '進階', '高級', '深度', '詳細', '分析', '設計', '實作']

    text_lower = text.lower()

    # 檢查難度關鍵詞
    for keyword in hard_keywords:
        if keyword in text_lower:
            return 'hard'

    for keyword in easy_keywords:
        if keyword in text_lower:
            return 'easy'

    # 根據文字長度判斷
    if text_length < 50:
        return 'easy'
    elif text_length < 150:
        return 'medium'
    else:
        return 'hard'

def create_sql_template_for_quiz(question_ids: List[str], quiz_info: Dict[str, Any], user_email: str = 'ai_system@mis_teach.com') -> str:
    """為quiz_generator生成的測驗創建SQL template，參考學校考古題的創建方式"""
    try:
        from accessories import sqldb
        from sqlalchemy import text
        import json

        # 創建SQL template記錄
        template_query = text("""
            INSERT INTO quiz_templates (
                user_email,
                template_type,
                question_ids,
                school,
                department,
                year
            ) VALUES (
                :user_email,
                :template_type,
                :question_ids,
                :school,
                :department,
                :year
            )
        """)

        # 準備數據
        template_data = {
            'user_email': user_email,
            'template_type': 'knowledge',
            'question_ids': json.dumps(question_ids),  # 使用傳入的question_ids
            'school': '',
            'department': '',
            'year': ''
        }

        # 執行SQL並獲取lastrowid作為template_id
        with sqldb.engine.connect() as conn:
            result = conn.execute(template_query, template_data)
            conn.commit()
            template_id = result.lastrowid

        logger.info(f"SQL template已創建: {template_id}")
        return str(template_id)

    except Exception as e:
        logger.error(f"創建SQL template失敗: {e}")
        return f"temp_template_{int(time.time())}"
