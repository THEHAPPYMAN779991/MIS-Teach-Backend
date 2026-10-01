
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAG智能教學系統 - 配置檔案 (Backend整合版本)
包含所有系統配置參數，方便統一管理和調整
"""

import os
from pathlib import Path

# ``app.py`` normally loads the backend's local ``api.env`` before importing
# application modules. Keep this idempotent fallback for standalone RAG
# scripts that import this configuration without starting Flask.
try:
    from dotenv import load_dotenv

    _BACKEND_ROOT = Path(__file__).resolve().parents[2]
    load_dotenv(_BACKEND_ROOT / "api.env", override=False)
except ImportError:
    # Environment variables may still be supplied by the shell or deployment
    # platform when python-dotenv is not installed.
    pass

# =============================================================================
# 基本路徑配置
# =============================================================================

# 專案根目錄 (rag_sys目錄)
PROJECT_ROOT = Path(__file__).parent

# 資料目錄
DATA_DIR = PROJECT_ROOT / "data"
PDF_DIR = DATA_DIR / "pdfs"
KNOWLEDGE_DB_DIR = DATA_DIR / "knowledge_db"
OUTPUT_DIR = DATA_DIR / "outputs"

# 確保目錄存在
for directory in [DATA_DIR, PDF_DIR, KNOWLEDGE_DB_DIR, OUTPUT_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

# =============================================================================
# AI模型配置
# =============================================================================


# Gemini API 配置
def _first_configured_gemini_api_key():
    """Return one configured AI Studio key without embedding credentials.

    ``GEMINI_API_KEYS`` is the active multi-key-group format used by
    ``tool.api_keys``. The singular and legacy names remain supported for
    backwards compatibility with existing local deployments.
    """
    for variable_name in ("GEMINI_API_KEY", "GEMINI_API_KEYS", "AI_API_KEYS"):
        raw_value = (os.getenv(variable_name) or "").strip()
        if raw_value:
            return raw_value.split(",", 1)[0].strip().strip("[]").strip() or None
    return None


GEMINI_CONFIG = {
    "api_key": _first_configured_gemini_api_key(),
    "model": "gemini-2.5-flash",
    "temperature": 0.7,
    "top_p": 0.8,
    "top_k": 40,
    "max_output_tokens": 2048,
}

# 可用的AI模型選項
AVAILABLE_AI_MODELS = {
    "gemini": {
        "name": "Gemini (API)",
        "description": "Google的Gemini模型，功能強大，需要網路",
        "type": "api",
        "config": GEMINI_CONFIG
    }
}

# 預設AI模型
DEFAULT_AI_MODEL = "gemini"

# 向量化模型設定 - GPU優化
EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"

# 向量化GPU優化設定
EMBEDDING_CONFIG = {
    "model_name": EMBEDDING_MODEL,
    "device": "cuda",
    "normalize_embeddings": True,
    "convert_to_tensor": True,
    "show_progress_bar": True,
    "batch_size": 64,
    "max_seq_length": 512,
    "precision": "float32"
}

# GPU設定 - RTX 4060 Ti 16GB 優化配置
GPU_CONFIG = {
    "enable_gpu": True,
    "device": "cuda",
    "gpu_memory_fraction": 0.8,
    "mixed_precision": True,
    "batch_size_gpu": 64,
    "cpu_fallback": True
}

# =============================================================================
# 資料庫配置
# =============================================================================

# ChromaDB設定
CHROMA_DB_PATH = str(KNOWLEDGE_DB_DIR / "chroma_db")
COLLECTION_NAME = "textbook_knowledge"

# FAISS設定 (備選)
FAISS_INDEX_PATH = str(KNOWLEDGE_DB_DIR / "faiss_index")

# =============================================================================
# PDF處理配置
# =============================================================================

# PDF處理參數
PDF_PROCESSING = {
    "strategy": "hi_res",
    "infer_table_structure": True,
    "chunking_strategy": "by_title",
    "max_characters": 500,
    "new_after_n_chars": 100,
    "languages": ["eng", "zho"],
    "min_content_length": 100
}

# =============================================================================
# 知識點處理配置
# =============================================================================

# 知識點生成參數
KNOWLEDGE_POINT_CONFIG = {
    "min_content_length": 100,
    "max_title_length": 100,
    "max_summary_length": 200,
    "max_keywords": 10,
    "batch_size": 100
}

# =============================================================================
# 搜索和檢索配置
# =============================================================================

# 搜索參數 - 極度寬鬆的搜索設定
SEARCH_CONFIG = {
    "default_top_k": 15,
    "max_top_k": 25,
    "similarity_threshold": 0.09,
    "enable_reranking": True,
    "min_results": 5,
    "force_return_results": True
}

# =============================================================================
# 語言配置
# =============================================================================

# 支援的語言
SUPPORTED_LANGUAGES = {
    "chinese": {
        "name": "中文",
        "code": "zh",
        "prompt_template": "chinese_prompt.txt"
    },
    "english": {
        "name": "English",
        "code": "en",
        "prompt_template": "english_prompt.txt"
    }
}

# 預設語言
DEFAULT_LANGUAGE = "chinese"

# =============================================================================
# 回答生成配置
# =============================================================================

# AI回答參數
AI_RESPONSE_CONFIG = {
    "temperature": 0.7,
    "max_tokens": 2000,
    "timeout": 30,
    "retry_attempts": 3,
    "include_metadata": True,
    "structured_output": True
}

# =============================================================================
# 系統配置
# =============================================================================

# 日誌配置
LOGGING_CONFIG = {
    "level": "INFO",
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    "file": str(OUTPUT_DIR / "system.log")
}

# 效能配置 - RTX 4060 Ti 16GB 優化
PERFORMANCE_CONFIG = {
    "max_workers": 4,
    "chunk_size": 1000,
    "memory_limit_mb": 4096,
    "enable_gpu": True,
    "gpu_batch_size": 64,
    "cpu_batch_size": 16
}

# =============================================================================
# 科目資訊配置
# =============================================================================

# 預設科目資訊
DEFAULT_SUBJECT_INFO = {
    "科目名稱": "計算機概論",
    "英文名稱": "Introduction to Computer Science",
}

# =============================================================================
# 輸出檔案配置
# =============================================================================

# 輸出檔案名稱
OUTPUT_FILES = {
    "structured_content": "structured_content.json",
    "knowledge_points": "knowledge_points.json",
    "conversation_history": "conversation_history.json",
    "processing_log": "processing.log"
}

# =============================================================================
# 開發和調試配置
# =============================================================================

# 調試模式
DEBUG_MODE = False

# 是否顯示詳細進度
VERBOSE_MODE = True

# 是否保存中間結果
SAVE_INTERMEDIATE_RESULTS = True

# =============================================================================
# 配置驗證函數
# =============================================================================

def check_gpu_availability():
    """檢查GPU可用性"""
    gpu_info = {
        "available": False,
        "device_count": 0,
        "device_name": None,
        "memory_total": 0,
        "memory_free": 0,
        "cuda_version": None
    }

    try:
        import torch
        if torch.cuda.is_available():
            gpu_info["available"] = True
            gpu_info["device_count"] = torch.cuda.device_count()
            gpu_info["device_name"] = torch.cuda.get_device_name(0)
            gpu_info["cuda_version"] = torch.version.cuda

            if gpu_info["device_count"] > 0:
                memory_total = torch.cuda.get_device_properties(0).total_memory
                memory_reserved = torch.cuda.memory_reserved(0)
                memory_allocated = torch.cuda.memory_allocated(0)

                gpu_info["memory_total"] = memory_total // (1024**3)
                gpu_info["memory_free"] = (memory_total - memory_reserved) // (1024**3)
                gpu_info["memory_allocated"] = memory_allocated // (1024**3)

    except ImportError:
        pass
    except Exception as e:
        print(f"⚠️ GPU檢查時發生錯誤: {e}")

    return gpu_info

def validate_config():
    """驗證配置是否正確"""
    try:
        # 檢查必要目錄
        for directory in [DATA_DIR, PDF_DIR, KNOWLEDGE_DB_DIR, OUTPUT_DIR]:
            if not directory.exists():
                print(f"❌ 目錄不存在: {directory}")
                return False

        if not EMBEDDING_MODEL:
            print("❌ 未設定向量化模型")
            return False

        print("✅ 配置驗證通過")
        return True

    except Exception as e:
        print(f"❌ 配置驗證失敗: {e}")
        return False

# =============================================================================
# Config 類別 - 為了向後兼容性
# =============================================================================

class Config:
    """配置類別，包含所有系統配置"""

    # 路徑配置
    PROJECT_ROOT = PROJECT_ROOT
    DATA_DIR = DATA_DIR
    PDF_DIR = PDF_DIR
    KNOWLEDGE_DB_DIR = KNOWLEDGE_DB_DIR
    OUTPUT_DIR = OUTPUT_DIR

    # AI 模型配置
    GEMINI_CONFIG = GEMINI_CONFIG
    GEMINI_API_KEY = GEMINI_CONFIG.get('api_key')
    DEFAULT_AI_MODEL = DEFAULT_AI_MODEL

    # 向量化配置
    EMBEDDING_MODEL_NAME = EMBEDDING_MODEL
    EMBEDDING_CONFIG = EMBEDDING_CONFIG
    DEVICE = GPU_CONFIG.get('device', 'cpu')

    # 資料庫配置
    CHROMA_DB_PATH = CHROMA_DB_PATH
    COLLECTION_NAME = COLLECTION_NAME

    # 搜索配置
    SEARCH_CONFIG = SEARCH_CONFIG

    # 其他配置
    DEBUG_MODE = DEBUG_MODE
    VERBOSE_MODE = VERBOSE_MODE

    @classmethod
    def get_gpu_info(cls):
        """獲取 GPU 資訊"""
        return check_gpu_availability()

    @classmethod
    def validate(cls):
        """驗證配置"""
        return validate_config()
