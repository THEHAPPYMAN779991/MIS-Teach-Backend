#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAG系統包初始化文件 (Backend整合版本)
"""

# 版本信息
__version__ = "1.0.0"
__author__ = "RAG Team"

# 導出主要函數與配置
__all__ = []

# 匯出 config
try:
    from .config import *  # noqa: F401,F403
    __all__.extend(['config'])
except ImportError as e:
    print(f"⚠️ config導入失敗: {e}")

# 從 rag_ai_role 匯出函數式 API
try:
    from .rag_ai_role import (
        handle_tutoring_conversation,
        should_search_database,
        enhance_prompt_with_knowledge,
        search_knowledge,
        get_stage_guidance,
        get_stage_display_name,
    )
    __all__.extend([
        'handle_tutoring_conversation',
        'should_search_database',
        'enhance_prompt_with_knowledge',
        'search_knowledge',
        'get_stage_guidance',
        'get_stage_display_name',
    ])
except ImportError as e:
    print(f"⚠️ rag_ai_role 函數導入失敗: {e}")
