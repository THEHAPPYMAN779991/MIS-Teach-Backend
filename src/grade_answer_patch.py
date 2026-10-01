"""grade_answer 批改容錯增強 patch

使用方式（在 grade_answer.py 最底下加一行）：
    from grade_answer_patch import patch_grader
    patch_grader(AnswerGrader)

或直接在 import grade_answer 之後 monkey patch：
    from src import grade_answer
    from src.grade_answer_patch import patch_grader
    patch_grader(grade_answer.AnswerGrader)
"""
import json
import re
import time
import random
from typing import Any, Dict, Optional


# ------------------------------------------------------------------
# A. 更強健的 JSON 解析
# ------------------------------------------------------------------
def _robust_parse_ai_response(self, response_text: str) -> Optional[Dict[str, Any]]:
    """容錯版本的 AI 回應解析。
    支援：
    - markdown 圍欄 ```json ... ```
    - 多個 JSON 物件（取第一個有效的）
    - 鍵名大小寫變化
    - 字串型分數（"85"）自動轉 float
    """
    if not response_text:
        return None

    raw = response_text.strip()

    # 1. 剝除 markdown 圍欄
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.S)
    if m:
        raw = m.group(1).strip()

    # 2. 嘗試多種策略找出 JSON
    candidates = []
    # 完整字串
    candidates.append(raw)
    # 第一個完整大括號區塊（greedy）
    bm = re.search(r"\{.*\}", raw, re.S)
    if bm:
        candidates.append(bm.group())
    # 第一個 non-greedy 區塊
    bm2 = re.search(r"\{.*?\}", raw, re.S)
    if bm2:
        candidates.append(bm2.group())

    result = None
    for cand in candidates:
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, dict):
                result = parsed
                break
        except json.JSONDecodeError:
            continue

    if result is None:
        # 最後嘗試：簡單修補常見錯誤（如尾隨逗號）
        try:
            fixed = re.sub(r",\s*([}\]])", r"\1", raw)
            result = json.loads(fixed)
        except Exception:
            print(f"⚠️ JSON 完全無法解析，原始回應 (前 200 字): {response_text[:200]}")
            return None

    # 3. 鍵名正規化（小寫）
    result = {k.lower().replace(" ", "_"): v for k, v in result.items()}

    # 4. score 強制轉 float
    if "score" in result:
        try:
            result["score"] = float(result["score"])
        except (TypeError, ValueError):
            result["score"] = 0.0

    # 5. is_correct 強制轉 bool
    if "is_correct" in result:
        v = result["is_correct"]
        if isinstance(v, str):
            result["is_correct"] = v.lower() in ("true", "yes", "正確", "對", "1")
        else:
            result["is_correct"] = bool(v)

    # 6. 必要欄位補齊
    if "is_correct" not in result:
        result["is_correct"] = (result.get("score", 0) or 0) >= 60
    if "score" not in result:
        result["score"] = 100 if result.get("is_correct") else 0
    if "feedback" not in result or not isinstance(result["feedback"], dict):
        result["feedback"] = {}

    fb = result["feedback"]
    fb.setdefault("strengths", "勇於嘗試，認真作答")
    fb.setdefault("weaknesses", "需要加強對相關概念的理解")
    fb.setdefault("suggestions", "建議複習相關章節，多做練習題")

    return result


# ------------------------------------------------------------------
# B. 帶 retry + exponential backoff 的 LLM 呼叫
# ------------------------------------------------------------------
def _call_with_retry(model, prompt, max_retries: int = 3, base_delay: float = 2.0):
    """以指數退避重試呼叫 LLM。處理 429、timeout、空回應。"""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            if hasattr(model, "generate_content"):
                resp = model.generate_content(prompt)
                text = getattr(resp, "text", None) or ""
            elif hasattr(model, "invoke"):
                resp = model.invoke(prompt)
                text = getattr(resp, "content", None) or str(resp)
            else:
                raise RuntimeError("模型既沒有 generate_content 也沒有 invoke 方法")

            if not text.strip():
                raise RuntimeError("LLM 回傳空字串")
            return text
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            # 429 / quota / timeout 才退避重試
            should_retry = any(s in msg for s in [
                "429", "quota", "rate", "timeout", "deadline",
                "exceeded", "unavailable", "internal error",
            ])
            if attempt < max_retries and should_retry:
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                print(f"⚠️ LLM 呼叫失敗 (第 {attempt} 次): {e} - {delay:.1f} 秒後重試")
                time.sleep(delay)
                continue
            # 非可重試錯誤或已用完次數
            print(f"❌ LLM 呼叫最終失敗 (第 {attempt} 次): {e}")
            break
    raise last_err if last_err else RuntimeError("Unknown LLM error")


# ------------------------------------------------------------------
# C. 套用 patch
# ------------------------------------------------------------------
def patch_grader(grader_cls):
    """把上面的容錯版本注入到 AnswerGrader。"""
    grader_cls._parse_ai_response = _robust_parse_ai_response
    grader_cls._call_with_retry = staticmethod(_call_with_retry)
    print("✅ AnswerGrader 已套用容錯 patch")
    return grader_cls
