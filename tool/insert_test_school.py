#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
插入測試學校資料到 MongoDB
用於測試全題型測驗功能
"""

import sys
import os
import random
from datetime import datetime
from pymongo import MongoClient

# 添加父目錄到路徑
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 定義有效的 answer_type 值
VALID_ANSWER_TYPES = {
    'single-choice',
    'multiple-choice',
    'fill-in-the-blank',
    'true-false',
    'short-answer',
    'long-answer',
    'choice-answer',
    'draw-answer',
    'coding-answer',
    'group'
}

# 定義要使用的題型（確保包含所有題型）
TARGET_ANSWER_TYPES = {
    'single-choice': 'single-choice',
    'multiple-choice': 'multiple-choice',
    'fill-in-the-blank': 'fill-in-the-blank',
    'true-false': 'true-false',
    'short-answer': 'short-answer',
    'long-answer': 'long-answer',
    'choice-answer': 'choice-answer',
    'draw-answer': 'draw-answer',
    'coding-answer': 'coding-answer',
    'group': 'group'
}

def normalize_answer_type(answer_type: str) -> str:
    """
    標準化 answer_type，確保符合系統定義的格式

    Args:
        answer_type: 原始 answer_type 值

    Returns:
        標準化的 answer_type 值
    """
    if not answer_type:
        return 'single-choice'  # 預設值

    # 移除前後空格
    answer_type = answer_type.strip()

    # 轉換為小寫
    answer_type = answer_type.lower()

    # 處理常見的變體
    type_mapping = {
        'single': 'single-choice',
        'single-choice': 'single-choice',
        'multiple': 'multiple-choice',
        'multiple-choice': 'multiple-choice',
        'fill': 'fill-in-the-blank',
        'fill_in_the_blank': 'fill-in-the-blank',
        'fill-in-blank': 'fill-in-the-blank',
        'fill-in-the-blank': 'fill-in-the-blank',
        'truefalse': 'true-false',
        'true_false': 'true-false',
        'true-false': 'true-false',
        'short': 'short-answer',
        'short-answer': 'short-answer',
        'long': 'long-answer',
        'long-answer': 'long-answer',
        'choice': 'choice-answer',
        'choice-answer': 'choice-answer',
        'draw': 'draw-answer',
        'drawing': 'draw-answer',
        'draw-answer': 'draw-answer',
        'code': 'coding-answer',
        'coding': 'coding-answer',
        'coding-answer': 'coding-answer',
        'programming': 'coding-answer',
        'group': 'group',
    }

    # 先檢查映射表
    if answer_type in type_mapping:
        answer_type = type_mapping[answer_type]

    # 驗證是否為有效的 answer_type
    if answer_type not in VALID_ANSWER_TYPES:
        print(f"⚠️ 警告：無效的 answer_type '{answer_type}'，將使用預設值 'single-choice'")
        return 'single-choice'

    return answer_type

def get_mongo_connection():
    """使用 config.py 獲取 MongoDB 連接"""
    try:
        from config import DevelopmentConfig

        config = DevelopmentConfig()
        mongo_uri = config.MONGO_URI
        db_name = config.MONGO_DB_NAME

        print("MongoDB configuration loaded for test-school initialization.")
        client = MongoClient(mongo_uri)
        db = client[db_name]

        # 測試連接
        client.admin.command('ping')
        print("MongoDB 連接成功")

        return db

    except Exception as e:
        print(f"MongoDB 連接失敗: {str(e)}")
        return None

def insert_test_school_data(auto_mode=False):
    """從資料庫選擇各題型題目插入測試學校"""
    try:
        # 獲取資料庫連接
        db = get_mongo_connection()
        if db is None:
            return False

        if auto_mode:
            print("自動模式：從資料庫選擇題目插入測試學校...")
        else:
            print("開始從資料庫選擇題目插入測試學校...")

        # 檢查是否已存在測試學校資料
        existing_count = db.exam.count_documents({
            'school': '測試學校(全題型)',
            'department': '資訊管理學系',
            'year': '114'
        })

        if existing_count > 0:
            if auto_mode:
                print(f"測試學校資料已存在 ({existing_count} 題)，跳過插入")
                return True
            else:
                print(f"已存在 {existing_count} 筆測試學校資料")
                choice = input("是否要重新插入？(y/N): ").strip().lower()
                if choice != 'y':
                    print("取消插入操作")
                    return True

        # 使用所有有效的題型
        target_types = list(VALID_ANSWER_TYPES)
        print(f"目標題型: {sorted(target_types)}\n")

        # 檢查哪些題型在資料庫中存在
        available_types = []
        for answer_type in target_types:
            # 查詢資料庫（同時查詢標準化和原始格式，以處理資料不一致的情況）
            count = db.exam.count_documents({
                '$or': [
                    {'answer_type': answer_type},
                    {'answer_type': answer_type.strip()},  # 處理空格
                    {'answer_type': answer_type.lower()},  # 處理大小寫
                ],
                'school': {'$ne': '測試學校(全題型)'}
            })

            if count > 0:
                available_types.append(answer_type)
                print(f"  {answer_type}: {count} 題可用")
            else:
                print(f"  {answer_type}: 0 題可用（跳過）")

        if not available_types:
            print("沒有找到任何可用的題型")
            return False

        print(f"\n將使用 {len(available_types)} 種題型\n")

        selected_questions = []
        used_answer_types = set()  # 記錄已使用的題型

        # 第一階段：確保每種題型至少有一題
        print("第一階段：確保每種題型至少有一題")
        print("-" * 60)

        for answer_type in available_types:
            # 從該 answer_type 中隨機選擇 1 題（排除測試學校）
            # 使用 $in 查詢以處理可能的變體格式
            questions = list(db.exam.find({
                '$or': [
                    {'answer_type': answer_type},
                    {'answer_type': answer_type.strip()},  # 處理空格
                    {'answer_type': answer_type.lower()},  # 處理大小寫
                ],
                'school': {'$ne': '測試學校(全題型)'}
            }).limit(50))

            if questions:
                selected = random.choice(questions)
                selected_questions.append(selected)
                used_answer_types.add(answer_type)
                print(f"題目 {len(selected_questions)}: {answer_type}")

        # 第二階段：補充到目標數量（20題）
        print(f"\n第二階段：補充到目標數量（20題）")
        print("-" * 60)
        remaining_questions = []
        for answer_type in available_types:
            # 使用 $in 查詢以處理可能的變體格式
            questions = list(db.exam.find({
                '$or': [
                    {'answer_type': answer_type},
                    {'answer_type': answer_type.strip()},  # 處理空格
                    {'answer_type': answer_type.lower()},  # 處理大小寫
                ],
                'school': {'$ne': '測試學校(全題型)'}
            }).limit(100))
            remaining_questions.extend(questions)

        # 隨機選擇題目補充到20題
        target_count = 20
        remaining_count = target_count - len(selected_questions)

        if remaining_count > 0 and len(remaining_questions) >= remaining_count:
            random_selected = random.sample(remaining_questions, remaining_count)
        elif remaining_count > 0:
            random_selected = remaining_questions
        else:
            random_selected = []

        for question in random_selected:
            if len(selected_questions) >= target_count:
                break
            # 確保只選擇指定題型（標準化後比較）
            q_type = question.get('answer_type', '')
            normalized_q_type = normalize_answer_type(q_type)
            if normalized_q_type in available_types:
                selected_questions.append(question)
                used_answer_types.add(normalized_q_type)
                if q_type != normalized_q_type:
                    print(f"題目 {len(selected_questions)}: {normalized_q_type} (原始: {q_type})")
                else:
                    print(f"題目 {len(selected_questions)}: {normalized_q_type}")

        if not selected_questions:
            print("沒有找到任何題目")
            return False

        print(f"\n總共選擇了 {len(selected_questions)} 題")
        print(f"涵蓋了 {len(used_answer_types)} 種不同題型: {sorted(used_answer_types)}")

        # 轉換為測試學校格式
        test_questions = []
        for i, question in enumerate(selected_questions):
            # 獲取並標準化 answer_type
            raw_answer_type = question.get('answer_type', 'single-choice')
            normalized_answer_type = normalize_answer_type(raw_answer_type)
            options = question.get('options', [])
            question_text = question.get('question_text', '')

            # 檢查題目文字是否包含「題目檔」，如果是單選題應該改為簡答題
            if '題目檔' in question_text and normalized_answer_type == 'single-choice':
                normalized_answer_type = 'short-answer'

            # 檢查多選題是否有選項，如果沒有選項應該改為簡答題
            if normalized_answer_type == 'multiple-choice':
                has_options = False
                if isinstance(options, list) and len(options) > 0:
                    has_options = True
                elif isinstance(options, str) and options.strip():
                    has_options = True

                if not has_options:
                    normalized_answer_type = 'short-answer'

            # 確保 question_number 是正確的（從 1 開始）
            question_number = str(i + 1)

            # 創建測試學校格式的題目
            test_question = {
                'type': question.get('type', 'single'),
                'school': '測試學校(全題型)',
                'department': '資訊管理學系',
                'year': '114',
                'question_number': question_number,  # 確保從 1 開始
                'question_text': question_text,
                'options': options,
                'answer': question.get('answer', ''),
                'answer_type': normalized_answer_type,  # 使用標準化的 answer_type
                'image_file': question.get('image_file', ''),
                'detail-answer': question.get('detail-answer', ''),
                'key-points': question.get('key-points', ''),
                'difficulty level': question.get('difficulty level', 'medium'),
                'created_at': datetime.now()
            }

            # 如果 answer_type 被修改，記錄警告
            if raw_answer_type != normalized_answer_type:
                reason = []
                if '題目檔' in question_text and raw_answer_type == 'single-choice':
                    reason.append("包含「題目檔」")
                if raw_answer_type == 'multiple-choice' and not (isinstance(options, list) and len(options) > 0 or (isinstance(options, str) and options.strip())):
                    reason.append("多選題沒有選項")
                reason_str = f" ({', '.join(reason)})" if reason else ""
                print(f"⚠️ 題目 {question_number}: answer_type 從 '{raw_answer_type}' 標準化為 '{normalized_answer_type}'{reason_str}")

            test_questions.append(test_question)

        # 所有測試資料都從資料庫選擇，不包含硬編碼資料

        # 插入資料
        inserted_count = 0
        for question in test_questions:
            try:
                result = db.exam.insert_one(question)
                print(f"插入題目 {question['question_number']}: {question['question_text'][:30]}...")
                inserted_count += 1
            except Exception as e:
                print(f"插入題目 {question['question_number']} 失敗: {str(e)}")

        print(f"\n成功插入 {inserted_count} 筆測試學校資料！")
        print(f"涵蓋了 {len(used_answer_types)} 種不同題型")

        return True

    except Exception as e:
        print(f"插入資料失敗: {str(e)}")
        return False

def list_all_answer_types():
    """列出所有 answer_type 及其數量"""
    try:
        db = get_mongo_connection()
        if db is None:
            return None

        # 獲取所有不同的 answer_type
        answer_types = db.exam.distinct('answer_type')

        # 過濾掉 None 和空字串
        answer_types = [at for at in answer_types if at]

        print("\n" + "=" * 60)
        print("資料庫中所有的 answer_type:")
        print("=" * 60)

        if not answer_types:
            print("沒有找到任何 answer_type")
            return None

        # 列出每個 answer_type 及其數量
        for i, answer_type in enumerate(answer_types, 1):
            count = db.exam.count_documents({'answer_type': answer_type})
            print(f"{i:2d}. {answer_type:30s} - {count:4d} 題")

        print("=" * 60)
        print(f"總共找到 {len(answer_types)} 種不同的題型")
        print("=" * 60 + "\n")

        return answer_types

    except Exception as e:
        print(f"查詢失敗: {str(e)}")
        return None

def show_database_stats():
    """顯示資料庫統計資訊"""
    try:
        db = get_mongo_connection()
        if db is None:
            return

        print("\n資料庫統計資訊:")
        print("=" * 50)

        # 總題目數
        total_count = db.exam.count_documents({})
        print(f"總題目數: {total_count}")

        # 測試學校題目數
        test_count = db.exam.count_documents({
            'school': '測試學校(全題型)',
            'department': '資訊管理學系',
            'year': '114'
        })
        print(f"測試學校題目數: {test_count}")

        # 各 answer_type 題目數
        answer_types = db.exam.distinct('answer_type')
        print(f"\n各題型題目數:")
        for answer_type in answer_types:
            count = db.exam.count_documents({'answer_type': answer_type})
            print(f"  {answer_type}: {count} 題")

    except Exception as e:
        print(f"獲取統計資訊失敗: {str(e)}")

def delete_test_school_data(auto_confirm=False):
    """刪除測試學校資料"""
    try:
        db = get_mongo_connection()
        if db is None:
            return False

        # 檢查是否已存在測試學校資料
        existing_count = db.exam.count_documents({
            'school': '測試學校(全題型)',
            'department': '資訊管理學系',
            'year': '114'
        })

        if existing_count == 0:
            print("沒有找到測試學校資料，無需刪除")
            return True

        print(f"找到 {existing_count} 筆測試學校資料")

        if not auto_confirm:
            choice = input("確定要刪除嗎？(y/N): ").strip().lower()
            if choice != 'y':
                print("取消刪除操作")
                return False
        else:
            print("自動確認刪除...")

        # 刪除測試學校資料
        result = db.exam.delete_many({
            'school': '測試學校(全題型)',
            'department': '資訊管理學系',
            'year': '114'
        })

        print(f"成功刪除 {result.deleted_count} 筆測試學校資料！")
        return True

    except Exception as e:
        print(f"刪除資料失敗: {str(e)}")
        return False

def check_and_insert_test_school():
    """自動檢查並插入測試學校資料（用於 app.py 啟動時調用）"""
    try:
        # 獲取資料庫連接
        db = get_mongo_connection()
        if db is None:
            print("MongoDB 連接失敗，跳過測試學校資料檢查")
            return False

        # 檢查是否已存在測試學校資料
        existing_count = db.exam.count_documents({
            'school': '測試學校(全題型)',
            'department': '資訊管理學系',
            'year': '114'
        })

        if existing_count > 0:
            print(f"測試學校資料已存在 ({existing_count} 題)，跳過插入")
            return True

        print("測試學校資料不存在，開始自動插入...")

        # 自動插入測試學校資料
        success = insert_test_school_data(auto_mode=True)

        if success:
            print("測試學校資料自動插入完成！")
        else:
            print("測試學校資料自動插入失敗！")

        return success

    except Exception as e:
        print(f"檢查測試學校資料時發生錯誤: {str(e)}")
        return False

def main():
    """主函數"""
    print("=" * 60)
    print("測試學校資料插入工具")
    print("=" * 60)

    # 顯示選單
    print("\n請選擇操作：")
    print("1. 列出所有題型")
    print("2. 插入測試學校資料（使用指定7種題型）")
    print("3. 刪除測試學校資料")
    print("4. 顯示資料庫統計")
    print("0. 退出")

    choice = input("\n請輸入選項 (0-4): ").strip()

    if choice == '1':
        list_all_answer_types()
    elif choice == '2':
        # 先列出所有題型
        answer_types = list_all_answer_types()
        if answer_types is None:
            print("無法獲取題型列表，程式結束")
            return

        # 插入測試學校資料
        success = insert_test_school_data()

        if success:
            print("\n測試學校資料插入完成！")
            print("現在可以在測驗中心選擇「測試學校(全題型)」進行全題型測驗測試")
            print("使用的題型: " + ", ".join(TARGET_ANSWER_TYPES.keys()))
        else:
            print("\n測試學校資料插入失敗！")
    elif choice == '3':
        delete_test_school_data()
    elif choice == '4':
        show_database_stats()
    elif choice == '0':
        print("退出程式")
    else:
        print("無效的選項")

if __name__ == "__main__":
    main()
