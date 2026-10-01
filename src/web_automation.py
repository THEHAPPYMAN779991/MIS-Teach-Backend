#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UI 操作工具 - 返回標準化的操作指令供前端執行
"""

import json
from typing import Dict, Any

def create_university_quiz(university: str, department: str, year: int) -> str:
    """
    創建大學考古題測驗
    返回標準化的操作指令 JSON
    """
    try:
        return json.dumps({
            "action": "create_university_quiz",
            "params": {
                "university": university,
                "department": department,
                "year": str(year)
            },
            "message": f"已為您準備 {university} {department} {year} 學年度的考古題測驗"
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "action": None,
            "error": f"創建測驗失敗: {str(e)}"
        }, ensure_ascii=False)

def create_knowledge_quiz(knowledge_point: str, difficulty: str, question_count: int) -> str:
    """
    創建知識點測驗
    返回標準化的操作指令 JSON
    """
    try:
        return json.dumps({
            "action": "create_knowledge_quiz",
            "params": {
                "knowledge_point": knowledge_point,
                "difficulty": difficulty,
                "question_count": question_count
            },
            "message": f"已為您準備 {question_count} 題 {difficulty} 難度的「{knowledge_point}」測驗"
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "action": None,
            "error": f"創建測驗失敗: {str(e)}"
        }, ensure_ascii=False)

def create_navigate_action(route: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    創建導航操作指令
    """
    return {
        "action": "navigate" if not params else "navigate_with_params",
        "params": {
            "route": route,
            **(params or {})
        }
    }