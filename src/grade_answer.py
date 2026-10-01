import json
import re
import concurrent.futures
from typing import List, Dict, Any, Tuple
from tool.api_keys import get_api_key, get_api_keys_count
from accessories import init_ai, init_ollama, init_gemini

class AnswerGrader:
    """答案批改器 - 簡化版本"""

    def __init__(self):
        # 初始化Gemini API
        # 預設使用 Ollama
        self.model = init_ai(ai_type='ollama')

    def batch_grade_ai_questions(self, questions_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """批量評分AI題目 - 並行處理版本"""
        if not questions_data:
            return []

        print(f"\n{'='*80}")
        print(f"🚀 [批量評分] 開始批量評分")
        print(f"{'='*80}")

        # 獲取可用的API金鑰數量
        api_keys_count = get_api_keys_count()
        total_questions = len(questions_data)

        print(f"📊 評分資訊:")
        print(f"   總題目數: {total_questions}")
        print(f"   可用 API 金鑰數: {api_keys_count}")

        # 如果使用 Ollama（不需要 API keys），使用單一模型實例處理
        if api_keys_count == 0:
            print(f"\n🔧 使用 Ollama 單一模型實例處理所有題目...")
            all_results = []
            for idx, question_data in enumerate(questions_data):
                try:
                    print(f"\n📝 處理第 {idx + 1}/{total_questions} 題...")
                    question_id = question_data.get('question_id', 'Unknown')
                    user_answer = question_data['user_answer']
                    question_type = question_data['question_type']

                    is_correct, score, feedback = self._ai_grade_answer_with_model(
                        self.model,
                        user_answer,
                        question_data.get('question_text', ''),
                        question_data.get('correct_answer', ''),
                        question_data.get('options', []),
                        question_type
                    )

                    result = {
                        'question_id': question_id,
                        'is_correct': is_correct,
                        'score': score,
                        'feedback': feedback
                    }
                    all_results.append(result)
                    print(f"✅ 第 {idx + 1} 題評分完成: {score} 分 ({'正確' if is_correct else '錯誤'})")
                except Exception as e:
                    print(f"❌ 評分題目 {idx + 1} 失敗: {e}")
                    import traceback
                    traceback.print_exc()
                    all_results.append({
                        'question_id': question_data.get('question_id', ''),
                        'is_correct': False,
                        'score': 0,
                        'feedback': {'error': f'評分失敗: {str(e)}'}
                    })

            print(f"\n{'='*80}")
            print(f"✅ [批量評分] 全部完成！共處理 {len(all_results)} 題")
            print(f"{'='*80}\n")
            return all_results

        # 計算每個API金鑰處理的題目數量（Gemini 多 API key 模式）
        questions_per_key = total_questions // api_keys_count
        remainder = total_questions % api_keys_count

        print(f"   每個金鑰處理: {questions_per_key} 題")
        print(f"   剩餘題目: {remainder} 題")

        # 分配題目給不同的API金鑰
        all_results = [None] * total_questions  # 預分配結果陣列

        print(f"\n🔧 建立並行處理執行緒池 (max_workers={api_keys_count})...")

        with concurrent.futures.ThreadPoolExecutor(max_workers=api_keys_count) as executor:
            futures = []
            start_index = 0

            print(f"\n📦 分配任務給各個 API 金鑰:")
            for i in range(api_keys_count):
                # 計算這個金鑰要處理的題目數量
                batch_size = questions_per_key + (1 if i < remainder else 0)
                end_index = start_index + batch_size

                # 提取這批題目
                questions_batch = questions_data[start_index:end_index]
                batch_indices = list(range(start_index, end_index))  # 記錄原始索引

                print(f"   API 金鑰 {i+1}: 題目 {start_index+1}-{end_index} (共 {batch_size} 題)")

                # 提交任務
                future = executor.submit(
                    self._process_questions_batch,
                    questions_batch,
                    batch_indices,
                    i
                )
                futures.append(future)

                start_index = end_index

            print(f"\n⏳ 等待所有批次完成...")
            completed_count = 0

            # 收集結果
            for future in concurrent.futures.as_completed(futures):
                try:
                    batch_results = future.result()
                    completed_count += 1
                    print(f"✅ 批次 {completed_count}/{api_keys_count} 完成 (處理了 {len(batch_results)} 題)")

                    # 將結果放入正確的位置
                    for result in batch_results:
                        if result and 'original_index' in result:
                            original_idx = result.pop('original_index')
                            all_results[original_idx] = result
                except Exception as e:
                    print(f"❌ 並行處理批次失敗: {e}")
                    import traceback
                    traceback.print_exc()

        # 過濾掉None值（如果有錯誤的話）
        final_results = [result for result in all_results if result is not None]

        print(f"\n{'='*80}")
        print(f"✅ 批量評分完成！")
        print(f"   成功評分: {len(final_results)}/{total_questions} 題")
        print(f"   失敗: {total_questions - len(final_results)} 題")
        print(f"{'='*80}\n")

        return final_results

    def _process_questions_batch(self, questions_batch: List[Dict], batch_indices: List[int], api_key_index: int) -> List[Dict]:
        """處理一批題目（單個API金鑰）"""
        results = []

        # 如果沒有題目，直接返回空結果
        if not questions_batch or not batch_indices:

            return results




        for i, question_data in enumerate(questions_batch):
            try:
                original_index = batch_indices[i]
                question_id = question_data.get('question_id', 'Unknown')

                print(f"\n🔹 [API金鑰 {api_key_index+1}] 正在評分第 {original_index+1} 題 (ID: {question_id})")

                # 為這個批次創建專用的Gemini模型實例
                batch_model = self._create_batch_model(api_key_index)

                user_answer = question_data['user_answer']
                question_type = question_data['question_type']

                is_correct, score, feedback = self._ai_grade_answer_with_model(
                    batch_model,
                    user_answer,
                    question_data.get('question_text', ''),
                    question_data.get('correct_answer', ''),
                    question_data.get('options', []),
                    question_type
                )

                result = {
                    'question_id': question_data['question_id'],
                    'is_correct': is_correct,
                    'score': score,
                    'feedback': feedback,
                    'original_index': original_index,  # 保持原始順序
                    'api_key_used': api_key_index + 1  # 記錄使用的API金鑰
                }
                results.append(result)

                print(f"   ✅ 題目 {original_index+1} 評分完成: {score} 分 ({'正確' if is_correct else '錯誤'})")

            except Exception as e:
                print(f"   ❌ API金鑰 {api_key_index+1} 評分題目 {batch_indices[i]+1} 失敗: {e}")
                import traceback
                traceback.print_exc()

                # 創建錯誤結果
                error_result = {
                    'question_id': question_data.get('question_id', ''),
                    'is_correct': False,
                    'score': 0,
                    'feedback': {'error': f'評分失敗: {str(e)}'},
                    'original_index': batch_indices[i],
                    'api_key_used': api_key_index + 1
                }
                results.append(error_result)

        print(f"\n{'─'*80}")
        print(f"✅ [API 金鑰 {api_key_index+1}] 批次處理完成！成功 {len(results)} 題")
        print(f"{'─'*80}\n")

        return results

    def _create_batch_model(self, api_key_index: int):
        """為批次創建專用的Gemini模型實例"""
        try:
            # 使用指定的API金鑰索引
            api_key = self._get_api_key_by_index(api_key_index)
            # 使用 accessories 中的 init_gemini 函數
            # 預設使用 Ollama
            model = init_ai(ai_type='ollama')
            return model
        except Exception as e:
            print(f"❌ 創建批次模型失敗: {e}")
            return self.model  # 回退到主模型

    def _get_api_key_by_index(self, index: int) -> str:
        """根據索引獲取特定的API金鑰"""
        try:
            from tool.api_keys import api_key_manager
            # 獲取所有可用的API金鑰
            all_keys = api_key_manager.api_keys
            if 0 <= index < len(all_keys):
                return all_keys[index]
            else:
                # 如果索引超出範圍，使用隨機選擇
                return get_api_key()
        except Exception as e:
            print(f"⚠️ 獲取指定索引API金鑰失敗，使用隨機選擇: {e}")
            return get_api_key()

    def _describe_image(self, model, image_parts: List, question_type: str = "") -> Dict[str, str]:
        """把圖片作答轉成可保存、可在結果頁展示的文字資料。

        draw-answer 會轉成圖形內容描述；其他圖片型作答（目前主要是
        fill-in-the-blank 手寫數學題）會優先轉成 LaTeX。回傳結構會保存
        在 feedback.answer_conversion，避免結果頁只能看到 base64 圖片。
        """
        try:
            print(f"\n{'='*80}")
            print(f"🖼️  [圖片描述階段] 開始分析圖片")
            print(f"{'='*80}")
            print(f"📊 圖片數量: {len(image_parts)}")

            normalized_type = str(question_type or "").strip().lower()
            is_drawing_question = normalized_type == 'draw-answer' or 'draw' in normalized_type
            if is_drawing_question:
                conversion_kind = 'drawing_description'
                conversion_label = 'AI 圖形內容描述'
                describe_prompt = """
請辨識學生在圖片中實際畫出的答案，並以繁體中文客觀描述，供教師批改與學生核對。
請依序描述：
1. 圖中的節點、物件、標籤與文字；
2. 線條、箭頭、方向、連接或階層關係；
3. 圖形的整體結構與配置；
4. 看不清楚或無法確定的部分，必須明確標示「無法辨識」，不可自行補畫。

只描述學生實際畫出的內容，不要判分、不要代替學生補上缺少元素，也不要重述標準答案。
如果有多張圖片，請分別描述。
"""
            else:
                conversion_kind = 'math_latex'
                conversion_label = '手寫公式辨識（LaTeX）'
                describe_prompt = r"""
請將學生圖片中的手寫數學作答忠實轉錄為 LaTeX，供教師批改與學生核對。
要求：
1. 保留等號、上下標、分數、根號、矩陣、括號及每一步計算；
2. 多行推導請逐行輸出，每一行使用 \[ ... \] 包住；
3. 單一公式也使用 \[ ... \] 包住；
4. 看不清楚的符號使用 \text{[無法辨識]}，不可猜測或修正學生答案；
5. 只輸出轉錄結果，不要判分、不要說明正確答案、不要使用 Markdown 程式碼區塊。
"""

            # 檢測模型類型並使用正確的方法
            # Gemini 模型有 generate_content 方法，Ollama 模型有 invoke 方法
            if hasattr(model, 'generate_content'):
                # 使用 Gemini 模型（支持圖片輸入）
                print(f"🔄 正在發送圖片給 Gemini 進行描述...")
                print(f"📦 傳送內容: 1 個提示詞 + {len(image_parts)} 張圖片")

                # 組合提示詞和圖片
                contents = [describe_prompt] + image_parts
                response = model.generate_content(contents)

                # 提取回應文字
                if hasattr(response, 'text'):
                    description = response.text.strip()
                elif hasattr(response, 'content'):
                    description = response.content.strip()
                else:
                    description = str(response).strip()

            elif hasattr(model, 'invoke'):
                # 使用 Ollama 模型（不支持圖片輸入，只能返回提示）
                print(f"⚠️  Ollama 模型不支持圖片輸入，跳過圖片描述")
                return {
                    'kind': conversion_kind,
                    'label': conversion_label,
                    'content': '無法轉換圖片內容（目前模型不支持圖片輸入）',
                    'source': 'image_preprocess'
                }
            else:
                print(f"⚠️  未知的模型類型，無法描述圖片")
                return {
                    'kind': conversion_kind,
                    'label': conversion_label,
                    'content': '無法轉換圖片內容',
                    'source': 'image_preprocess'
                }

            # 避免模型偶爾把 LaTeX 包進 Markdown fence，造成前端 KaTeX 無法渲染。
            description = description.strip()
            if description.startswith('```') and description.endswith('```'):
                description = re.sub(r'^```(?:latex|tex)?\s*', '', description, flags=re.IGNORECASE)
                description = re.sub(r'\s*```$', '', description).strip()

            print(f"\n✅ 圖片描述完成！")
            print(f"{'─'*80}")
            print(f"📝 AI 描述內容:")
            print(f"{'─'*80}")
            print(f"{description}")
            print(f"{'─'*80}")
            print(f"📏 描述長度: {len(description)} 字元\n")

            return {
                'kind': conversion_kind,
                'label': conversion_label,
                'content': description,
                'source': 'image_preprocess'
            }

        except Exception as e:
            print(f"❌ 圖片描述失敗: {e}")
            import traceback
            traceback.print_exc()
            return {
                'kind': 'image_description',
                'label': 'AI 圖片內容辨識',
                'content': '無法轉換圖片內容',
                'source': 'image_preprocess'
            }

    def _ai_grade_answer_with_model(self, model, user_answer: Any, question_text: str, correct_answer: str,
                                    options: List[str], question_type: str) -> Tuple[bool, float, Dict[str, Any]]:
        """使用指定的模型進行AI評分 - 使用舊版 SDK"""
        try:
            import base64
            from PIL import Image
            import io

            print(f"\n{'='*80}")
            print(f"🎯 [評分階段] 開始處理答案")
            print(f"{'='*80}")
            print(f"📝 題目類型: {question_type}")
            print(f"📄 答案類型: {type(user_answer).__name__}")

            # 強制使用新版 Google GenAI SDK 方式處理圖片
            def _is_data_image(s: str) -> bool:
                try:
                    result = isinstance(s, str) and s.startswith('data:image/')
                    if result:
                        return result
                except Exception:
                    return False

            image_parts = []
            text_parts = []
            image_description = ""
            answer_conversion = {}

            # 收集所有圖片，使用 PIL Image 方式（兼容舊版 SDK）
            if isinstance(user_answer, list):
                print(f"📋 檢測到陣列答案，共 {len(user_answer)} 項")
                # 多圖片：收集所有 data:image/*
                for i, ua in enumerate(user_answer):
                    if _is_data_image(ua):
                        try:
                            print(f"🖼️  項目 {i+1}: 檢測到圖片資料")

                            header, b64 = ua.split(',', 1)
                            mime = header.split(':', 1)[1].split(';', 1)[0]
                            print(f"   📎 MIME 類型: {mime}")
                            print(f"   📊 Base64 資料長度: {len(b64)} 字元")

                            image_data = base64.b64decode(b64)
                            print(f"   ✅ Base64 解碼成功，位元組大小: {len(image_data)} bytes ({len(image_data)/1024:.2f} KB)")

                            # 使用 PIL Image 轉換（兼容舊版 SDK）
                            image = Image.open(io.BytesIO(image_data))
                            print(f"   🔍 原始圖片格式: {image.format}, 模式: {image.mode}, 尺寸: {image.size}")

                            # 確保圖片是 RGB 模式（舊版 SDK 兼容性更好）
                            if image.mode != 'RGB':
                                print(f"   🔄 轉換圖片模式: {image.mode} -> RGB")
                                image = image.convert('RGB')

                            image_parts.append(image)
                            print(f"   ✅ 成功創建圖片物件 (尺寸: {image.size}, 模式: {image.mode})")
                        except Exception as e:
                            print(f"   ❌ 處理圖片失敗: {e}")
                            import traceback
                            traceback.print_exc()
                            continue
                    else:
                        print(f"📝 項目 {i+1}: 文字資料 - {str(ua)[:50]}...")
                        text_parts.append(str(ua))
            elif _is_data_image(user_answer):
                print(f"🖼️  檢測到單張圖片")
                try:
                    header, b64 = user_answer.split(',', 1)
                    mime = header.split(':', 1)[1].split(';', 1)[0]
                    print(f"   📎 MIME 類型: {mime}")
                    print(f"   📊 Base64 資料長度: {len(b64)} 字元")

                    image_data = base64.b64decode(b64)
                    print(f"   ✅ Base64 解碼成功，位元組大小: {len(image_data)} bytes ({len(image_data)/1024:.2f} KB)")

                    # 使用 PIL Image 轉換（兼容舊版 SDK）
                    image = Image.open(io.BytesIO(image_data))
                    print(f"   🔍 原始圖片格式: {image.format}, 模式: {image.mode}, 尺寸: {image.size}")

                    # 確保圖片是 RGB 模式（舊版 SDK 兼容性更好）
                    if image.mode != 'RGB':
                        print(f"   🔄 轉換圖片模式: {image.mode} -> RGB")
                        image = image.convert('RGB')

                    # 驗證圖片數據（檢查是否為空白或純色）
                    import numpy as np
                    img_array = np.array(image)
                    unique_colors = len(np.unique(img_array.reshape(-1, img_array.shape[2]), axis=0))
                    print(f"   🎨 圖片包含 {unique_colors} 種不同顏色")

                    if unique_colors == 1:
                        print(f"   ⚠️  警告：圖片只有單一顏色，可能是空白圖片！")
                        # 顯示該顏色的 RGB 值
                        single_color = img_array[0, 0]
                        print(f"   🎨 唯一顏色 RGB: {single_color}")

                    image_parts.append(image)
                    print(f"   ✅ 成功創建圖片物件 (尺寸: {image.size}, 模式: {image.mode})")
                except Exception as e:
                    print(f"   ❌ 處理圖片失敗: {e}")
                    import traceback
                    traceback.print_exc()
            else:
                print(f"📝 檢測到文字答案: {str(user_answer)[:100]}...")
                text_parts.append(str(user_answer))

            print(f"\n📊 收集結果:")
            print(f"   🖼️  圖片數量: {len(image_parts)}")
            print(f"   📝 文字片段: {len(text_parts)}")

            # 如果有圖片，先創建一個新的 Gemini 來描述圖片
            if image_parts and model:
                try:
                    print(f"\n🔧 創建專門的圖片描述模型...")
                    # 創建專門用於描述圖片的模型實例
                    description_model = init_gemini('gemini-2.5-flash')
                    print(f"✅ 描述模型創建成功")
                    answer_conversion = self._describe_image(
                        description_model, image_parts, question_type
                    )
                except Exception as e:
                    print(f"⚠️ 創建描述模型失敗，使用原模型: {e}")
                    answer_conversion = self._describe_image(
                        model, image_parts, question_type
                    )
                image_description = str(answer_conversion.get('content', '')).strip()

            # 構建評分提示（包含圖片描述）
            print(f"\n{'='*80}")
            print(f"📝 [評分提示] 構建評分提示")
            print(f"{'='*80}")
            if image_description:
                print(f"✅ 包含圖片描述 (長度: {len(image_description)} 字元)")
            else:
                print(f"ℹ️  無圖片描述")

            prompt = self._build_grading_prompt(
                user_answer,
                question_text,
                correct_answer,
                options,
                question_type,
                image_description  # 傳入圖片描述
            )
            print(f"✅ 評分提示構建完成 (總長度: {len(prompt)} 字元)")

            if model:
                # 檢測模型類型並使用正確的方法
                if hasattr(model, 'generate_content'):
                    # 使用 Gemini 模型
                    if image_parts:
                        try:
                            print(f"\n{'='*80}")
                            print(f"🚀 [發送請求] 使用圖片模式評分 (Gemini)")
                            print(f"{'='*80}")
                            # 組合內容：先放提示詞，後放圖片
                            contents = [prompt] + image_parts

                            # 如果還有文字內容，也加入
                            if text_parts:
                                contents.append(f"額外文字內容: {' '.join(text_parts)}")

                            print(f"📦 傳送內容組成:")
                            for i, item in enumerate(contents):
                                if hasattr(item, '__class__') and 'Part' in str(type(item)):
                                    print(f"   項目 {i+1}: {item.__class__.__name__} (圖片 Part 物件)")
                                else:
                                    preview = str(item)[:80].replace('\n', ' ')
                                    print(f"   項目 {i+1}: 文字 - {preview}...")

                            print(f"\n🔄 正在發送請求給 Gemini 進行評分...")
                            response = model.generate_content(contents)
                            print(f"✅ 收到 Gemini 回應")

                        except Exception as e:
                            print(f"❌ 評分失敗: {e}")
                            import traceback
                            traceback.print_exc()
                            print(f"\n🔄 使用純文字模式重試...")
                            response = model.generate_content(prompt)
                    else:
                        print(f"\n{'='*80}")
                        print(f"🚀 [發送請求] 使用純文字模式評分 (Gemini)")
                        print(f"{'='*80}")
                        print(f"🔄 正在發送請求給 Gemini...")
                        response = model.generate_content(prompt)
                        print(f"✅ 收到 Gemini 回應")

                elif hasattr(model, 'invoke'):
                    # 使用 Ollama 模型（不支持圖片輸入，只能使用文字）
                    print(f"\n{'='*80}")
                    print(f"🚀 [發送請求] 使用純文字模式評分 (Ollama)")
                    print(f"{'='*80}")
                    if image_parts:
                        print(f"⚠️  Ollama 不支持圖片輸入，將使用文字提示（包含圖片描述）進行評分")
                    print(f"🔄 正在發送請求給 Ollama...")
                    response = model.invoke(prompt)
                    print(f"✅ 收到 Ollama 回應")
                else:
                    raise ValueError(f"未知的模型類型，無法進行評分")

                print(f"\n{'='*80}")
                print(f"🔍 [解析回應] AI 評分結果")
                print(f"{'='*80}")
                # 正確提取回應文字：優先使用 content，如果沒有則嘗試 text 方法或屬性
                if hasattr(response, 'content'):
                    response_text = response.content
                elif hasattr(response, 'text'):
                    # text 可能是方法或屬性
                    if callable(response.text):
                        response_text = response.text()
                    else:
                        response_text = response.text
                else:
                    response_text = str(response)

                print(f"📄 原始回應長度: {len(response_text)} 字元")
                print(f"{'─'*80}")
                print(f"原始回應內容:")
                print(f"{response_text}")
                print(f"{'─'*80}")

                result = self._parse_ai_response(response_text)
                if result:
                    # 確保評分邏輯一致性：分數 ≥ 85 的答案被標記為正確
                    score = result.get('score', 0)
                    is_correct = score >= 85

                    feedback = result.get('feedback', {})
                    if not isinstance(feedback, dict):
                        feedback = {'explanation': str(feedback)}
                    if answer_conversion and answer_conversion.get('content'):
                        feedback['answer_conversion'] = answer_conversion
                    result['feedback'] = feedback

                    print(f"\n✅ 解析成功！")
                    print(f"   是否正確: {is_correct}")
                    print(f"   分數: {score}")
                    print(f"   反饋: {result.get('feedback', {})}")

                    # 如果AI的判斷與我們的標準不一致，進行修正
                    if result.get('is_correct') != is_correct:
                        print(f"⚠️  修正 is_correct: {result.get('is_correct')} -> {is_correct}")
                        result['is_correct'] = is_correct

                    print(f"{'='*80}\n")
                    return result['is_correct'], result['score'], result['feedback']
                else:
                    print(f"❌ 解析失敗！無法從回應中提取評分資訊")
                    print(f"{'='*80}\n")
            failed_feedback = {'error': 'AI評分失敗'}
            if answer_conversion and answer_conversion.get('content'):
                failed_feedback['answer_conversion'] = answer_conversion
            return False, 0, failed_feedback

        except Exception as e:
            print(f"❌ AI評分異常: {str(e)}")
            import traceback
            traceback.print_exc()
            failed_feedback = {'error': f'評分失敗: {str(e)}'}
            if 'answer_conversion' in locals() and answer_conversion.get('content'):
                failed_feedback['answer_conversion'] = answer_conversion
            return False, 0, failed_feedback

    def _build_grading_prompt(self, user_answer: str, question_text: str, correct_answer: str,
                             options: List[str], question_type: str, image_description: str = "") -> str:
        """構建AI評分提示"""
        # 根據題目類型添加特定的評分指導
        type_guidance = self._get_type_specific_guidance(question_type)

        # 如果有圖片描述，加入到提示中
        image_info = ""
        if image_description:
            image_info = f"""

**圖片內容描述（由AI預先分析）**：
{image_description}

**請注意**：上述圖片描述是AI預先分析的結果，請結合圖片本身和這個描述來進行評分。
"""

        prompt = f"""
請作為一位專業的MIS課程教師，對以下學生答案進行評分。

**評分任務說明**：
請記住你只需要評分學生的答案，不要評分正確答案。正確答案只是用來參考比較的標準。
若學生以「圖片」作答：
- 我們已經預先用AI分析過圖片內容，請參考圖片描述和圖片本身進行評分。
- 只要圖片中有與題目相關的計算/公式/圖形元素，應給予相對應的分數（可給部分分）。

{image_info}

**題目資訊**：
題目類型：{question_type}
題目內容：{question_text}

**需要評分的內容**：
學生答案：{user_answer}

**參考標準（不要評分這個）**：
正確答案：{correct_answer}
選項：{options if options else '無'}

**重要說明**：
- 如果學生答案是圖片（data:image/... 或多張圖），請結合上方的圖片描述和圖片本身進行評分。
- 對於繪圖或手寫題，依據圖片內容與題目要求的匹配度評分，可給部分分。
- 圖片中若同時包含簽名與作答，簽名須被忽略，不得因此判0分。
- **對於選擇題**：比較學生答案與正確答案是否一致，不考慮大小寫差異
- **對於多選題**：比較學生答案與正確答案的選項組合是否一致
- **對於是非題**：比較學生答案與正確答案是否一致

{type_guidance}

**通用評分重點**：
1. **只評分學生答案的內容**，與正確答案進行比較
2. **學生答案必須與題目內容相關**，不能是無意義的數字或符號
3. 如果學生答案與題目要求完全無關，必須給0分
4. **對於選擇題**：如果學生選擇的選項與正確答案相同，給100分；否則給0分

**評分要求**：
1. 仔細分析學生答案的內容和邏輯
2. 判斷答案是否正確或部分正確
3. 給出0-100的分數
4. 提供具體的評分理由和改進建議
5. 必須填寫優點、需要改進的地方和學習建議，不能留空

**正確性判斷**：
- 分數 ≥ 85分：答案被認為是正確的 (is_correct: true)
- 分數 < 85分：答案被認為是不正確的 (is_correct: false)

請務必以嚴格規範的JSON格式返回評分結果，不要有任何其他文字：
{{
    "is_correct": true/false,
    "score": 分數(0-100),
    "feedback": {{
        "explanation": "評分說明(如果有圖描述一下圖片內容)",
        "strengths": "優點",
        "weaknesses": "需要改進的地方",
        "suggestions": "學習建議"
    }}
}}

**評分示例**：
題目：說明CPU中Instruction Register及Program Counter的用途
學生答案：10
正確答案：Instruction Register (IR)：存放目前正在執行的指令。Program Counter (PC)：存放下一個要執行的指令的記憶體位址
評分：0分，is_correct: false（因為"10"只是數字，與題目完全無關，不包含任何CPU概念）

題目：Linux檔案系統中，"rwx"代表何意義？
學生答案：100
正確答案：r代表讀取權限，w代表寫入權限，x代表執行權限
評分：0分，is_correct: false（因為"100"只是數字，與題目完全無關，不包含任何權限概念）

題目：說明CPU中Instruction Register及Program Counter的用途
學生答案：Instruction Register存放指令，Program Counter存放下一個指令的位址
正確答案：Instruction Register (IR)：存放目前正在執行的指令。Program Counter (PC)：存放下一個要執行的指令的記憶體位址
評分：85分，is_correct: true（因為學生答案包含了正確的核心概念）
"""
        return prompt

    def _get_type_specific_guidance(self, question_type: str) -> str:
        """根據題目類型返回特定的評分指導"""

        if question_type == 'draw-answer' or 'draw' in question_type.lower():
            return """
**繪圖題評分標準**：
- 90-100分：完全正確 - 繪圖完全正確，包含所有必要元素，結構清晰，符合題目要求
- 70-89分：接近正確 - 繪圖基本正確，主要元素齊全，結構合理，有輕微錯誤
- 50-69分：答案對一半 - 繪圖包含部分必要元素，但結構不完整或有明顯錯誤
- 0-49分：答案錯誤 - 繪圖與題目要求無關，或只是隨意塗鴉，沒有實質內容

**繪圖題特別注意**：
- 必須檢查繪圖是否與題目內容相關
- 如果只是隨意畫線、塗鴉或與題目無關的圖形，必須給0分
- 繪圖必須包含題目要求的核心元素和結構
- 不能因為學生有畫圖就給分，必須看內容是否正確
- 如果繪圖內容與正確答案完全不符，必須給低分（0-39分）
- 對於空白或幾乎空白的圖片，必須給0分
- 對於只有簡單線條或無意義圖形的圖片，最多給30分
- 評分要客觀公正，嚴格按照繪圖內容與題目要求的匹配度給分
- 對於複雜的繪圖題目，要仔細分析每個必要元素是否正確呈現

**嚴格評分要求**：
- 對於數學計算題的繪圖，必須包含具體的計算過程和結果
- 如果只是畫了幾條線或簡單圖形，沒有數學內容，最多給20分
- 必須檢查繪圖是否真的回答了題目的問題
- 對於隨意塗鴉、無意義線條、或與題目完全無關的內容，必須給0分
- 評分時要非常嚴格，寧可給低分也不要給高分
"""

        elif question_type == 'coding-answer' or 'code' in question_type.lower():
            return """
**程式撰寫題評分標準**：
- 90-100分：完全正確 - 程式碼完全正確，邏輯清晰，語法正確，能正常運行
- 70-89分：接近正確 - 程式碼基本正確，邏輯合理，有輕微語法錯誤但不影響功能
- 50-69分：答案對一半 - 程式碼部分正確，邏輯有問題但基本結構正確
- 0-49分：答案錯誤 - 程式碼與題目要求無關，或完全無法運行

**程式題特別注意**：
- 必須檢查程式碼是否與題目要求相關
- 如果只是隨意輸入文字或無關代碼，必須給0分
- 程式碼必須能解決題目提出的問題
- 語法正確性和邏輯正確性都要考慮
- 對於"hello world"等無關文字，必須給0分
- 對於沒有函數定義、沒有邏輯結構的代碼，最多給10分
"""

        elif question_type in ['short-answer', 'long-answer', 'fill-in-the-blank']:
            return """
**問答題評分標準**：
- 90-100分：完全正確 - 答案完全正確，內容完整且準確，包含所有關鍵概念
- 70-89分：接近正確 - 答案基本正確，主要概念正確但有小錯誤或遺漏
- 50-69分：答案對一半 - 答案包含部分關鍵概念但理解不夠深入
- 0-49分：答案錯誤 - 答案錯誤，主要概念錯誤或與題目無關

**問答題特別注意**：
- 學生答案必須包含正確答案的核心概念和關鍵信息
- 如果學生只回答數字、符號或與題目無關的內容，必須給0分
- 答案必須與題目內容有實質關聯
- 不能因為學生努力就給高分，必須看內容正確性
- 對於"測試"、"不知道"、"隨便"等無關回答，必須給0分
- 對於只有一個數字或符號的回答，必須給0分
"""

        elif question_type in ['single-choice', 'multiple-choice', 'true-false']:
            return """
**選擇題評分標準**：
- 100分：答案完全正確
- 0分：答案錯誤或未作答

**選擇題特別注意**：
- 選擇題只有對錯，沒有部分分數
- 必須與正確答案完全一致才算正確
- 如果答案格式不正確或無法識別，視為錯誤
- 對於空白答案或無關回答，必須給0分
- 嚴格按照正確答案進行評分，不允許任何偏差
- 答案必須完全匹配，包括大小寫、格式、順序等

**嚴格評分規則**：
- 單選題：學生答案必須與正確答案完全相同（如正確答案是"B"，學生答"C"則0分）
- 多選題：學生答案集合必須與正確答案集合完全相同（如正確答案是"ABD"，學生答"ABE"則0分）
- 順序無關：多選題中答案順序不影響評分（"ABD"與"DBA"視為相同）
- 任何偏差都視為錯誤，給0分"""

        else:
            return """
**通用評分標準**：
- 90-100分：完全正確 - 答案完全正確，內容完整且準確
- 70-89分：接近正確 - 答案基本正確，主要概念正確但有小錯誤
- 50-69分：答案對一半 - 答案包含部分正確概念但理解不夠深入
- 0-49分：答案錯誤 - 答案錯誤，主要概念錯誤或與題目無關

**特別注意**：
- 評分要客觀公正，不能因為學生努力就給高分
- 如果學生答案與題目內容完全無關，必須給0分
- 只有當學生答案在內容上與題目有實質關聯時，才能給分數
- 對於無意義的數字、符號、重複字符，必須給0分
- 對於"不知道"、"隨便"、"測試"等無關回答，必須給0分
"""

    def _parse_ai_response(self, response_text: str) -> Dict[str, Any]:
        """解析AI回應"""
        try:
            # 嘗試提取JSON部分
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                json_str = json_match.group()
                result = json.loads(json_str)

                # 驗證必要字段
                if all(key in result for key in ['is_correct', 'score', 'feedback']):
                    # 確保 feedback 字段完整
                    feedback = result.get('feedback', {})
                    if not feedback.get('strengths') or feedback.get('strengths') == '無':
                        feedback['strengths'] = '勇於嘗試，認真作答'
                    if not feedback.get('weaknesses') or feedback.get('weaknesses') == '無':
                        feedback['weaknesses'] = '需要加強對相關概念的理解'
                    if not feedback.get('suggestions') or feedback.get('suggestions') == '無':
                        feedback['suggestions'] = '建議複習相關章節，多做練習題'

                    result['feedback'] = feedback
                    return result
                else:
                    return None
            else:
                return None

        except Exception as e:
            print(f"⚠️ 解析AI回應失敗: {e}")
            return None

# 創建全局實例
grader = AnswerGrader()

def batch_grade_ai_questions(questions_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """批量批改AI題目的便捷函數"""
    return grader.batch_grade_ai_questions(questions_data)
