#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAG AI 教學系統 - 重構版本
簡化函數結構，真正實現 RAG 功能
"""

from tool.api_keys import get_api_key
import hashlib
import json
import re
import threading
import time
from typing import Dict, Any, List, Optional
from datetime import datetime
import logging
# 註：chromadb / Settings 已於 GraphRAG 改造時移除
# 教材檢索全部走 src.graphrag_client，向量存在 Neo4j
from accessories import init_ai, init_ollama, init_gemini

# 設置日誌
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== 全局變數 ====================

# 學習會話管理
learning_sessions = {}
_graphrag_usage_local = threading.local()

# 會話持久化文件路徑
import os
SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learning_sessions.json")


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read a tunable integer without letting a bad env value break chat."""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


# Research-oriented defaults: keep textbook chunks complete and leave enough room
# for GraphRAG's structured concepts/paths.  The total prompt is still bounded so
# a pathological graph expansion cannot exceed the model context indefinitely.
GRAPHRAG_TOP_K = _bounded_env_int("GRAPHRAG_TOP_K", 10, 1, 30)
GRAPHRAG_CONTEXT_CHAR_BUDGET = _bounded_env_int(
    "GRAPHRAG_CONTEXT_CHAR_BUDGET", 100000, 2000, 2000000
)
GRAPHRAG_TEXT_ITEM_MAX_CHARS = _bounded_env_int(
    "GRAPHRAG_TEXT_ITEM_MAX_CHARS", 10000, 200, 500000
)
GRAPHRAG_PREREQ_CHUNK_DEPTH = _bounded_env_int(
    "GRAPHRAG_PREREQ_CHUNK_DEPTH", 1, 1, 3
)
GRAPHRAG_PREREQ_CHUNKS_PER_CONCEPT = _bounded_env_int(
    "GRAPHRAG_PREREQ_CHUNKS_PER_CONCEPT", 1, 1, 3
)
GRAPHRAG_MAX_PREREQ_CONCEPTS = _bounded_env_int(
    "GRAPHRAG_MAX_PREREQ_CONCEPTS", 5, 1, 5
)
GRAPHRAG_SOURCE_METADATA_LOOKUP_MAX = _bounded_env_int(
    "GRAPHRAG_SOURCE_METADATA_LOOKUP_MAX", 500, 10, 2000
)
LEARNING_CONCEPT_READY_THRESHOLD = _bounded_env_int(
    "LEARNING_CONCEPT_READY_THRESHOLD", 90, 50, 100
)
LEARNING_COMPLETION_MARKER_SCORE = _bounded_env_int(
    "LEARNING_COMPLETION_MARKER_SCORE", 99, 90, 100
)
LEARNING_NEXT_CONCEPT_CANDIDATE_LIMIT = _bounded_env_int(
    "LEARNING_NEXT_CONCEPT_CANDIDATE_LIMIT", 8, 1, 20
)
# Tutoring policy is deliberately fixed for the production learning flow.
#
# The first GraphRAG call injects the complete eligible candidate-evidence set:
# top-five Seed concepts, their complete selected textbook chunks, and the
# system-filtered distance-1 prerequisite concepts/chunks.  Every follow-up
# reuses that exact cached evidence.  The LLM decides which candidate explains
# the learner's current gap; the backend never promotes Seed #1 to a "current
# target" and never changes evidence according to a score threshold.
#
# ``stateful`` used to implement a five-low-score prerequisite queue.  Keeping
# it configurable allowed a deployment to silently re-enable a contradictory
# teaching policy, so it is intentionally disabled rather than merely defaulted
# off.  The requested value is logged for migration diagnostics only.
_requested_graphrag_tutoring_mode = str(
    os.getenv("GRAPHRAG_TUTORING_MODE", "one_shot")
).strip().lower()
GRAPHRAG_TUTORING_MODE = "one_shot"
if _requested_graphrag_tutoring_mode not in {"", "one_shot"}:
    logger.warning(
        "GRAPHRAG_TUTORING_MODE=%s was ignored; tutoring is fixed to one_shot "
        "candidate-evidence mode.",
        _requested_graphrag_tutoring_mode,
    )

def _empty_graphrag_usage(reason: str = "not_used") -> Dict[str, Any]:
    return {
        "used": False,
        "reason": reason,
        "seed_count": 0,
        "expanded_count": 0,
        "snippet_count": 0,
        "prereq_material_count": 0,
        "prereq_chunk_count": 0,
        "prereq_chain_count": 0,
        "prereq_node_count": 0,
        "descendant_chain_count": 0,
        "descendant_node_count": 0,
        "total_items": 0,
        "seed_concepts": [],
        "candidate_concepts": [],
        "candidate_prerequisite_concepts": [],
        "concept_selection_policy": "llm_diagnosis_over_all_candidates",
        "primary_concept_selected": False,
        "retrieved_seed_count": 0,
        "retrieved_expanded_count": 0,
        "retrieved_snippet_count": 0,
        "retrieved_prereq_material_count": 0,
        "retrieved_prereq_chunk_count": 0,
        "retrieved_prereq_chain_count": 0,
        "retrieved_prereq_node_count": 0,
        "retrieved_descendant_chain_count": 0,
        "retrieved_descendant_node_count": 0,
        "retrieved_chunk_count": 0,
        "unique_retrieved_chunk_count": 0,
        "selected_chunk_count": 0,
        "rejected_chunk_count": 0,
        "duplicate_chunk_count": 0,
        "noise_filtered_chunk_count": 0,
        "injected_chunk_count": 0,
        "complete_injected_chunk_count": 0,
        "shortened_chunk_count": 0,
        "omitted_chunk_count": 0,
        "budget_omitted_chunk_count": 0,
        "chunk_rerank": {},
        "prereq_chunk_depth": GRAPHRAG_PREREQ_CHUNK_DEPTH,
        "prereq_chunks_per_concept": GRAPHRAG_PREREQ_CHUNKS_PER_CONCEPT,
        "prereq_chunk_concepts": [],
        "context_char_budget": GRAPHRAG_CONTEXT_CHAR_BUDGET,
        "context_chars": 0,
        "context_utf8_bytes": 0,
        "context_estimated_tokens": 0,
        "prompt_before_chars": 0,
        "prompt_after_chars": 0,
        "context_truncated": False,
        "graph_context_mode": "seed_only",
        "selection_policy": "initial_seed_order",
        "seed_chunk_metadata_total": 0,
        "seed_chunk_metadata_resolved": 0,
        "seed_chunk_metadata_unresolved": 0,
        "retrieved_prerequisite_concepts": [],
        "injected_prerequisite_concepts": [],
        "prerequisite_decisions": [],
    }

def reset_graphrag_usage() -> None:
    """清空本次請求的 GraphRAG 使用統計。"""
    _graphrag_usage_local.last_usage = _empty_graphrag_usage()

def get_last_graphrag_usage() -> Dict[str, Any]:
    """取得本次請求最後一次 GraphRAG 增強使用量。"""
    usage = getattr(_graphrag_usage_local, "last_usage", None)
    return dict(usage) if usage else _empty_graphrag_usage()

def _set_last_graphrag_usage(usage: Dict[str, Any]) -> None:
    _graphrag_usage_local.last_usage = usage

def _concept_key(name: str) -> str:
    """Normalize a concept name only for session-level duplicate checks."""
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())

def _ensure_learning_path_fields(session: dict) -> dict:
    """Keep old sessions compatible with GraphRAG learning-path state."""
    session.setdefault('current_target', None)
    session.setdefault('current_target_started_user_count', 0)
    session.setdefault('last_seed_concepts', [])
    session.setdefault('candidate_concepts', [])
    session.setdefault('candidate_prerequisite_concepts', [])
    session.setdefault('available_graphrag_concepts', [])
    session.setdefault('llm_selected_focus_concepts', [])
    session.setdefault('concept_selection_policy', 'llm_diagnosis_over_all_candidates')
    session.setdefault('mastered_concepts', [])
    session.setdefault('last_completed_concept', None)
    session.setdefault('recommended_next_concept', None)
    session.setdefault('next_concept_candidates', [])
    session.setdefault('next_concept_recommendation', None)
    session.setdefault('target_transition_pending', False)

    # A saved conversation may originate from the former stateful policy.  Once
    # the application runs in one-shot mode, that legacy state must never leak
    # into a response or the UI as a selected teaching target.  Keep the stored
    # candidate/evidence lists for audit, but remove only planning state.
    if GRAPHRAG_TUTORING_MODE == "one_shot":
        session['current_target'] = None
        session['current_target_started_user_count'] = 0
        session['active_core_concept'] = None
        session['active_teaching_concept'] = None
        session['teaching_mode'] = 'llm_candidate_diagnosis'
        session['prerequisite_queue'] = []
        session['prerequisite_candidates'] = []
        session['prerequisite_rejections'] = []
        session['recommended_next_concept'] = None
        session['next_concept_candidates'] = []
        session['next_concept_recommendation'] = None
        session['target_transition_pending'] = False
        session['last_completed_concept'] = None
    # Do not initialise the former score-triggered prerequisite planner.  Its
    # fields remain cleared above only so old persisted sessions stay readable.
    if GRAPHRAG_TUTORING_MODE != "one_shot":
        try:
            from src.remedial_learning_state import ensure_state
            ensure_state(session)
        except Exception:
            pass
    return session

def _sync_session_retrieval_state(
    session: dict,
    graphrag_usage: Optional[Dict[str, Any]],
    question: str = "",
) -> None:
    """Freeze the first GraphRAG seed set instead of replacing it each turn."""
    _ensure_learning_path_fields(session)
    seed_concepts = list((graphrag_usage or {}).get('seed_concepts') or [])
    if seed_concepts:
        if not session.get('last_seed_concepts'):
            session['last_seed_concepts'] = seed_concepts
        if not session.get('current_target'):
            session['current_target'] = seed_concepts[0]
        try:
            from src.remedial_learning_state import freeze_initial_retrieval
            freeze_initial_retrieval(session, question, graphrag_usage)
        except Exception as e:
            logger.warning(f"⚠️ 保存第一輪 GraphRAG 狀態失敗: {e}")


def _unique_concept_names(values: List[Any]) -> List[str]:
    """Return clean concept names while preserving retrieval order."""
    result: List[str] = []
    seen = set()
    for value in values or []:
        if isinstance(value, dict):
            value = value.get("name") or value.get("concept")
        name = str(value or "").strip()
        key = _concept_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(name)
    return result


def _sync_one_shot_candidate_state(
    session: dict,
    graphrag_usage: Optional[Dict[str, Any]],
) -> None:
    """Persist the complete candidate set without choosing a primary concept.

    Seed order is retained only as retrieval metadata and prompt-budget priority.
    It must not become ``current_target`` or an active/core teaching concept.
    """
    _ensure_learning_path_fields(session)
    usage = graphrag_usage or {}
    seeds = _unique_concept_names(
        usage.get("seed_concepts") or usage.get("candidate_concepts") or []
    )
    prerequisites = _unique_concept_names(
        usage.get("injected_prerequisite_concepts")
        or usage.get("candidate_prerequisite_concepts")
        or usage.get("prereq_chunk_concepts")
        or []
    )
    available = _unique_concept_names([*seeds, *prerequisites])

    if seeds:
        session["last_seed_concepts"] = seeds
        session["candidate_concepts"] = seeds
    if prerequisites:
        session["candidate_prerequisite_concepts"] = prerequisites
    if available:
        session["available_graphrag_concepts"] = available

    session["concept_selection_policy"] = "llm_diagnosis_over_all_candidates"

    # Clear legacy state so an old persisted session cannot silently turn Seed #1
    # into a key/core concept when the active policy is one-shot candidate-set.
    session["current_target"] = None
    session["current_target_started_user_count"] = 0
    session["active_core_concept"] = None
    session["active_teaching_concept"] = None
    session["teaching_mode"] = "llm_candidate_diagnosis"
    session["prerequisite_queue"] = []
    session["prerequisite_candidates"] = []
    session["prerequisite_rejections"] = []
    session["recommended_next_concept"] = None
    session["next_concept_candidates"] = []
    session["next_concept_recommendation"] = None
    session["target_transition_pending"] = False
    session["last_completed_concept"] = None
    session["mastered_concepts"] = []


def _cache_one_shot_context(
    session: dict,
    prompt_before_injection: str,
    prompt_after_injection: str,
    usage: Optional[Dict[str, Any]],
) -> str:
    """Persist the exact first-turn GraphRAG evidence for later dialogue turns.

    This is deliberately a cache, not a second retrieval.  Keeping the exact
    appended text means every follow-up sees the same complete Seed and direct
    prerequisite evidence that was available on the initial tutoring turn.
    """
    before = str(prompt_before_injection or "")
    after = str(prompt_after_injection or "")
    context_text = after[len(before):] if after.startswith(before) else ""
    if not context_text.strip():
        session.pop("one_shot_graphrag_context", None)
        return ""

    source_usage = dict(usage or {})
    session["one_shot_graphrag_context"] = {
        "text": context_text,
        "sha256": hashlib.sha256(context_text.encode("utf-8")).hexdigest(),
        "cached_at": datetime.now().isoformat(),
        # The original usage is retained for audit; it also lets follow-up
        # traces report the actual number of cached, injected chunks.
        "usage": source_usage,
    }
    return context_text


def _reuse_one_shot_context(
    session: dict,
    prompt: str,
) -> tuple[str, Dict[str, Any]]:
    """Append first-turn cached GraphRAG evidence without querying Neo4j again."""
    cache = session.get("one_shot_graphrag_context") or {}
    context_text = str(cache.get("text") or "")
    if not context_text.strip():
        usage = _empty_graphrag_usage("one_shot_context_cache_missing")
        usage.update({
            "backend": "graphrag",
            "retrieval_mode": "one_shot_context_cache_missing",
            "candidate_concepts": list(session.get("candidate_concepts") or []),
            "candidate_prerequisite_concepts": list(
                session.get("candidate_prerequisite_concepts") or []
            ),
            "concept_selection_policy": "llm_diagnosis_over_all_candidates",
            "primary_concept_selected": False,
        })
        return prompt, usage

    source_usage = dict(cache.get("usage") or {})
    # Cached GraphRAG evidence must not outlive the selection policy that
    # created it.  The caller will rebuild this context once from the original
    # question, then cache the replacement for later turns.
    if source_usage.get("selection_policy") != "strict_research_policy_v2":
        session.pop("one_shot_graphrag_context", None)
        usage = _empty_graphrag_usage("one_shot_context_policy_outdated")
        usage.update({
            "backend": "graphrag",
            "retrieval_mode": "one_shot_context_policy_outdated",
            "candidate_concepts": list(session.get("candidate_concepts") or []),
            "candidate_prerequisite_concepts": list(
                session.get("candidate_prerequisite_concepts") or []
            ),
            "concept_selection_policy": "llm_diagnosis_over_all_candidates",
            "primary_concept_selected": False,
        })
        return prompt, usage

    enhanced_prompt = f"{prompt}{context_text}"
    usage = _empty_graphrag_usage("one_shot_cached_context_reused")
    # Copy retrieval/audit quantities from the first retrieval, then overwrite
    # prompt-specific quantities for the current dialogue turn.
    for key in (
        "top_k", "graph_context_mode", "seed_count", "expanded_count",
        "snippet_count", "prereq_material_count", "prereq_chunk_count",
        "prereq_chain_count", "prereq_node_count", "descendant_chain_count",
        "descendant_node_count", "total_items", "seed_concepts",
        "retrieved_seed_count", "retrieved_expanded_count",
        "retrieved_snippet_count", "retrieved_prereq_material_count",
        "retrieved_prereq_chunk_count", "retrieved_prereq_chain_count",
        "retrieved_prereq_node_count", "retrieved_descendant_chain_count",
        "retrieved_descendant_node_count", "retrieved_chunk_count",
        "unique_retrieved_chunk_count", "selected_chunk_count",
        "rejected_chunk_count", "duplicate_chunk_count",
        "noise_filtered_chunk_count", "injected_chunk_count",
        "complete_injected_chunk_count", "shortened_chunk_count",
        "omitted_chunk_count", "budget_omitted_chunk_count", "chunk_rerank",
        "prereq_chunk_depth", "prereq_chunks_per_concept",
        "prereq_chunk_concepts", "retrieved_prerequisite_concepts",
        "injected_prerequisite_concepts", "prerequisite_decisions",
    ):
        if key in source_usage:
            usage[key] = source_usage[key]

    usage.update({
        "used": True,
        "reason": "one_shot_cached_context_reused",
        "backend": "graphrag",
        "retrieval_mode": "one_shot_cached_context_reuse",
        "cache_hit": True,
        "context_reused": True,
        "context_cache_sha256": str(cache.get("sha256") or ""),
        "candidate_concepts": list(session.get("candidate_concepts") or []),
        "candidate_prerequisite_concepts": list(
            session.get("candidate_prerequisite_concepts") or []
        ),
        "concept_selection_policy": "llm_diagnosis_over_all_candidates",
        "primary_concept_selected": False,
        "context_chars": len(context_text),
        "context_utf8_bytes": len(context_text.encode("utf-8")),
        "context_estimated_tokens": max(0, round(len(context_text) / 4)),
        "prompt_before_chars": len(prompt),
        "prompt_after_chars": len(enhanced_prompt),
        "context_truncated": False,
        "one_shot_context_previously_injected": True,
    })
    return enhanced_prompt, usage


def extract_llm_selected_focus_concepts(
    ai_response: str,
    allowed_concepts: Optional[List[str]] = None,
) -> List[str]:
    """Parse and validate the LLM's own diagnosed knowledge-gap concepts.

    The prompt requires a line beginning with ``本輪判斷的知識缺口：``. Only
    concepts present in the injected GraphRAG candidate set are persisted, so
    this audit field cannot silently record an invented graph concept.
    """
    text = str(ai_response or "")
    match = re.search(
        r"本輪判斷的知識缺口\s*[:：]\s*([^\n\r]+)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return []

    raw_line = re.sub(r"[*`#]", "", match.group(1)).strip()
    if not raw_line or raw_line in {"待確認", "證據不足", "無"}:
        return []

    allowed = _unique_concept_names(allowed_concepts or [])
    if not allowed:
        return []

    selected: List[str] = []
    selected_keys = set()
    line_key = _concept_key(raw_line)

    # Prefer exact names already present in the injected candidate set. This is
    # robust when concept names themselves contain spaces or punctuation.
    for concept in allowed:
        concept_key = _concept_key(concept)
        if concept_key and concept_key in line_key and concept_key not in selected_keys:
            selected.append(concept)
            selected_keys.add(concept_key)

    return selected[:5]


def _enhance_prompt_with_learning_state(prompt: str, session: dict, question: str) -> str:
    """Inject exact active-concept context without another global GraphRAG query."""
    try:
        from src.graphrag_client import GRAPHRAG_API_BASE
        from src.remedial_learning_state import build_active_context, public_state

        state_context = build_active_context(session)
        injected = str(state_context.get("text") or "")
        records = list(state_context.get("chunk_records") or [])
        if not injected:
            usage = _empty_graphrag_usage("stateful_context_empty")
            usage.update({
                "backend": "graphrag",
                "retrieval_mode": "stateful_exact_concept",
                "seed_concepts": list(session.get("last_seed_concepts") or []),
            })
            _set_last_graphrag_usage(usage)
            return prompt

        enhanced = prompt + injected
        seed_names = [
            item.get("name")
            for item in (session.get("initial_retrieval") or {}).get("seed_concepts") or []
            if item.get("name")
        ]
        usage = _empty_graphrag_usage("stateful_context_used")
        usage.update({
            "used": bool(records),
            "reason": "stateful_cache_reuse" if state_context.get("cache_hit") else "stateful_exact_concept_fetch",
            "backend": "graphrag",
            "retrieval_mode": "stateful_exact_concept",
            "cache_hit": bool(state_context.get("cache_hit")),
            "top_k": len(records),
            "seed_count": len(seed_names),
            "seed_concepts": seed_names,
            "retrieved_seed_count": len(seed_names),
            "retrieved_chunk_count": len(records),
            "unique_retrieved_chunk_count": len({record.get("content_sha256") for record in records}),
            "selected_chunk_count": len(records),
            "rejected_chunk_count": 0,
            "injected_chunk_count": len(records),
            "complete_injected_chunk_count": len(records),
            "shortened_chunk_count": 0,
            "omitted_chunk_count": 0,
            "context_chars": len(injected),
            "context_utf8_bytes": len(injected.encode("utf-8")),
            "context_estimated_tokens": max(0, round(len(injected) / 4)),
            "prompt_before_chars": len(prompt),
            "prompt_after_chars": len(enhanced),
            "active_core_concept": state_context.get("core_concept"),
            "active_teaching_concept": state_context.get("concept"),
            "teaching_mode": state_context.get("teaching_mode"),
            "chunk_rerank": {
                "enabled": True,
                "backend": "stateful_cache" if state_context.get("cache_hit") else "exact_concept_profile",
                "scoring_mode": "stateful_exact_concept",
                "selected_count": len(records),
                "prerequisite_first": state_context.get("role") == "prerequisite",
            },
        })
        _set_last_graphrag_usage(usage)
        try:
            from src.graphrag_trace import record_query
            record_query(
                question=question,
                top_k=len(records),
                api_base=GRAPHRAG_API_BASE,
                result={
                    "seed_concepts": seed_names,
                    "expanded": [],
                    "prereq_chains": [],
                    "descendant_chains": [],
                    "stateful_remediation": public_state(session),
                },
                usage=usage,
                latency_ms=0.0,
                prompt_before_chars=len(prompt),
                prompt_after_chars=len(enhanced),
                prompt_before_text=prompt,
                prompt_after_text=enhanced,
                injected_context_text=injected,
                chunk_records=records,
            )
        except Exception as e:
            logger.debug(f"Stateful GraphRAG trace skipped: {e}")
        return enhanced
    except Exception as e:
        logger.error(f"❌ 有狀態 GraphRAG context 失敗: {e}", exc_info=True)
        usage = _empty_graphrag_usage("stateful_context_error")
        usage["backend"] = "graphrag"
        _set_last_graphrag_usage(usage)
        return prompt

def _activate_pending_next_concept(session: dict) -> None:
    """Start the recommended next concept on the user's next follow-up turn."""
    _ensure_learning_path_fields(session)
    if not session.get('target_transition_pending'):
        return

    selected = session.get('recommended_next_concept') or {}
    next_name = selected.get('name') if isinstance(selected, dict) else None
    if not next_name:
        session['target_transition_pending'] = False
        return

    session['current_target'] = next_name
    # Keep the stateful GraphRAG planner synchronized with the legacy learning
    # progress fields.  The downstream concept is activated only after the
    # previous core reached the 90-point mastery gate.
    session['active_core_concept'] = next_name
    session['active_teaching_concept'] = next_name
    session['teaching_mode'] = 'diagnose_core'
    session['prerequisite_queue'] = []
    session['prerequisite_candidates'] = []
    session['prerequisite_rejections'] = []
    session['last_knowledge_gap'] = None
    session.setdefault('concept_states', {}).setdefault(next_name, {
        'concept': next_name,
        'role': 'core',
        'seed_rank': None,
        'mastery': 'undetermined',
        'last_score': None,
        'evidence': None,
        'updated_at': datetime.now().isoformat(),
    })
    history = session.get('conversation_history') or []
    session['current_target_started_user_count'] = sum(1 for msg in history if msg.get('role') == 'user')
    session['understanding_level'] = 0
    session['learning_stage'] = 'core_concept_confirmation'
    session['target_transition_pending'] = False
    session.setdefault('concept_progress', []).append({
        'stage': 'next_concept_started',
        'understanding_level': 0,
        'score': None,
        'concept': next_name,
        'timestamp': datetime.now().isoformat()
    })

def maybe_recommend_next_concept(
    session: dict,
    question: str,
    graphrag_usage: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """After a concept is mastered, select one downstream concept for the next step."""
    _ensure_learning_path_fields(session)
    seeds = list((graphrag_usage or {}).get('seed_concepts') or session.get('last_seed_concepts') or [])
    source = session.get('current_target') or (seeds[0] if seeds else None)
    if not source:
        session['next_concept_recommendation'] = {
            'source_concept': None,
            'selected': None,
            'candidates': [],
            'reason': 'no_source_concept',
        }
        return session['next_concept_recommendation']

    mastered = list(session.get('mastered_concepts') or [])
    mastered_keys = {_concept_key(name) for name in mastered}
    if _concept_key(source) not in mastered_keys:
        mastered.append(source)
        session['mastered_concepts'] = mastered

    try:
        from src.graphrag_client import select_next_concept
        recommendation = select_next_concept(
            source,
            mastered_concepts=mastered,
            question_text=question,
            max_candidates=LEARNING_NEXT_CONCEPT_CANDIDATE_LIMIT,
        )
    except Exception as e:
        logger.warning(f"⚠️ GraphRAG 下一概念推薦失敗: {e}")
        recommendation = {
            'source_concept': source,
            'selected': None,
            'candidates': [],
            'reason': f'error: {e}',
        }

    selected = recommendation.get('selected') if isinstance(recommendation, dict) else None
    session['last_completed_concept'] = source
    session['next_concept_recommendation'] = recommendation
    session['next_concept_candidates'] = (recommendation or {}).get('candidates') or []
    session['recommended_next_concept'] = selected
    session['target_transition_pending'] = bool(selected)
    if selected:
        session['current_target'] = selected.get('name') or session.get('current_target')

    session.setdefault('concept_progress', []).append({
        'stage': 'next_concept_recommended',
        'understanding_level': session.get('understanding_level', 0),
        'score': None,
        'completed_concept': source,
        'recommended_concept': selected.get('name') if selected else None,
        'recommendation_reason': (recommendation or {}).get('reason'),
        'timestamp': datetime.now().isoformat()
    })
    return recommendation

def save_sessions_to_file():
    """將會話保存到文件目前先註解掉之後我再看看是不是要用"""
    # 創建可序列化的會話副本
    serializable_sessions = {}
    for key, session in learning_sessions.items():
        serializable_session = session.copy()
        # 確保 datetime 對象被轉換為字符串
        if 'created_at' in serializable_session and isinstance(serializable_session['created_at'], datetime):
            serializable_session['created_at'] = serializable_session['created_at'].isoformat()
        serializable_sessions[key] = serializable_session

    with open(SESSION_FILE, 'w', encoding='utf-8') as f:
        json.dump(serializable_sessions, f, ensure_ascii=False, indent=2)

def load_sessions_from_file():
    """從文件載入會話"""
    if not os.path.exists(SESSION_FILE):
        return

    with open(SESSION_FILE, 'r', encoding='utf-8') as f:
        sessions = json.load(f)
        # 轉換回字典
        for key, value in sessions.items():
            # 確保 datetime 字符串被正確處理
            if 'created_at' in value and isinstance(value['created_at'], str):
                try:
                    value['created_at'] = datetime.fromisoformat(value['created_at'])
                except ValueError:
                    # 如果解析失敗，使用當前時間
                    value['created_at'] = datetime.now()
            learning_sessions[key] = value

# 在模組載入時載入會話
load_sessions_from_file()

def cleanup_old_sessions(max_age_hours: int = 24):
    """清理過期的會話，避免記憶體洩漏"""
    current_time = datetime.now()
    expired_sessions = []

    for session_key, session_data in learning_sessions.items():
        if 'created_at' in session_data:
            try:
                created_time = (datetime.fromisoformat(session_data['created_at'])
                              if isinstance(session_data['created_at'], str)
                              else session_data['created_at'])

                age_hours = (current_time - created_time).total_seconds() / 3600
                if age_hours > max_age_hours:
                    expired_sessions.append(session_key)
            except:
                # 如果時間解析失敗，保留會話
                pass

    # 刪除過期會話
    for session_key in expired_sessions:
        del learning_sessions[session_key]

    return len(expired_sessions)

# 定期清理會話（每小時清理一次）
import threading
import time

def auto_cleanup_sessions():
    """自動清理會話的後台任務"""
    while True:
        try:
            time.sleep(3600)  # 每小時執行一次
            cleanup_old_sessions()
        except Exception as e:
            print(f"⚠️ 自動清理失敗：{e}")

# 啟動自動清理（在後台執行）
cleanup_thread = threading.Thread(target=auto_cleanup_sessions, daemon=True)
cleanup_thread.start()

# 教學風格提示詞
TEACHER_STYLE = """你是一位經驗豐富的資管系教授，正在一對一輔導學生。系統會一次提供與題目相關的多個 GraphRAG Seed、其直接先備概念，以及相對應的完整教材 Chunk。這些資料是候選證據集合，並不預先指定唯一的核心或關鍵知識點。

**你的教學原則**：
- **由證據診斷**：綜合題目、學生答案、批改回饋及全部候選教材，自行判斷學生缺少的一個或多個概念
- **不以排名代替診斷**：Seed 排序只代表檢索相關程度，不得直接把第一名視為唯一知識缺口
- **限制於已提供證據**：只能使用 Prompt 中已注入的概念、圖關係與完整教材 Chunk，不得補造未提供的圖譜關係
- **選擇性補救**：不必依序教授所有候選概念，只處理能直接解釋學生錯誤的內容
- **概念連貫性**：每個問題都要與原題及本輪診斷出的知識缺口有明確關聯
- **蘇格拉底式提問**：透過具體引導問題，讓學生理解推理過程，而不是背誦答案
- **精確評分**：每次學生回答後，給出0-100分的具體評分，評估學生對原題所需知識的整體理解程度

**評分標準**：
- **0-30分**：完全不理解或回答錯誤，需要解釋主要知識缺口
- **31-60分**：有部分概念但理解不完整，需要補充關鍵關係或步驟
- **61-80分**：理解較好，能回答相關問題，可以進入應用層面
- **81-89分**：理解很好，需要最後確認與整合
- **90-100分**：已能整合題目所需概念並說明答案邏輯

**教學流程**：
1. **知識缺口診斷階段**：根據所有候選概念與學生證據，判斷本輪最需要補救的概念
2. **相關概念引導階段**：說明被選概念與原題、其他候選概念或直接先備之間的關係
3. **應用理解階段**：讓學生將理解應用到題目情境中
4. **理解驗證階段**：要求學生用自己的話整合題目所需概念與答案邏輯
5. **完成階段**：學生已能正確解釋原題，不需要由系統切換到另一個預設核心概念

**回應要求**：
- 每次回應第一段都要輸出「本輪判斷的知識缺口：概念A、概念B」；只能使用已注入的正式概念名稱，無法判斷時寫「待確認」
- 語氣親切自然，如同真正的老師
- 學生回答後必須給出0-100分的具體評分，格式為「評分：[分數]分」
- 根據評分提出一個與目前知識缺口直接相關的引導問題
- 使用 **粗體**、換行與必要的步驟說明增強可讀性

**學習評估標準**：
- 學生能指出原題涉及哪些必要概念
- 學生能說明這些概念與答案之間的邏輯
- 學生能將概念應用到相近情境
- 學生能辨別直接先備背景與原題直接作答證據的差異

現在，請根據完整候選證據集合進行補救教學。
"""

# ==================== 核心功能 ====================

def handle_direct_answer(question: str, user_email: str = None) -> str:
    """
    直接解答問題 - 使用RAG檢索相關知識，直接給出答案和解釋
    不使用引導式教學，不進行評分，不管理學習進度

    Args:
        question: 用戶的問題
        user_email: 用戶email（可選，用於日誌記錄）

    Returns:
        str: 直接給出的答案和詳細解釋
    """
    try:
        logger.info(f"📝 開始直接解答問題: {question[:50]}...")

        # 構建直接解答的提示詞
        direct_answer_prompt = f"""你是一位資管系教授，負責直接解答學生的問題。

**你的任務**：
- 直接回答問題，不需要引導式提問
- 提供清晰、完整的解釋
- 如果問題涉及計算或步驟，詳細說明過程
- 語氣親切自然，但要直接明確
- 可以使用Markdown格式來增強可讀性（粗體、換行等）

**問題**：
{question}

請直接給出答案和詳細解釋："""

        # 使用RAG增強提示詞（檢索相關知識）
        enhanced_prompt = enhance_prompt_with_knowledge(direct_answer_prompt, question)
        logger.info(f"📚 RAG增強後的提示詞長度: {len(enhanced_prompt)} 字符")

        # 調用AI獲取回應
        ai_response = call_gemini_api(enhanced_prompt)

        # 檢查回應是否有效
        if not ai_response or not ai_response.strip():
            logger.warning(f"⚠️ AI回應為空，問題: {question[:50]}...")
            return "抱歉，AI無法生成回答。請重新提問或稍後再試。"

        logger.info(f"✅ 成功生成直接解答，回應長度: {len(ai_response)} 字符")

        # 直接返回回應（不需要清理評分等，因為直接解答不會有評分）
        return ai_response.strip()

    except Exception as e:
        logger.error(f"❌ 直接解答失敗: {e}", exc_info=True)
        return f"抱歉，處理問題時發生錯誤：{str(e)}"

def _guess_domain_from_seeds(seeds: list) -> str:
    """從 seed 概念名稱猜測所屬領域（給 tutoring_progress.domain 用）。

    這是簡單啟發式；未來可改成查 Neo4j Concept.category 更精確。
    """
    if not seeds:
        return '未知領域'
    joined = ' '.join(str(s) for s in seeds).lower()
    rules = [
        (['行程', 'process', '排程', 'schedul', '記憶體', 'memory', '死結', 'deadlock',
          '虛擬記憶體', 'kernel', '核心', '同步', 'semaphore', '中斷', 'interrupt',
          '磁碟', 'disk'], '作業系統'),
        (['tree', '樹', 'bst', 'avl', '堆疊', 'stack', 'queue', '佇列',
          'sort', '排序', 'graph', '鏈結串列', 'linked list', 'heap', '堆積',
          'hash', '雜湊', '複雜度', 'complexity'], '資料結構'),
        (['sql', '正規化', 'normal', '資料庫', 'database', 'schema', 'index'], '資料庫'),
        (['tcp', 'ip', 'osi', '網路', 'network', 'routing', 'protocol', '拓樸'], '電腦網路'),
        (['布林', 'boolean', 'karnaugh', '邏輯閘', 'digital'], '數位邏輯'),
        (['encrypt', 'aes', 'rsa', '加密', '安全', 'security', '防火牆'], '資訊安全'),
        (['neural', '神經', 'cnn', 'rnn', 'transformer', 'machine learn', '機器學習'], 'AI與機器學習'),
    ]
    for kws, domain in rules:
        if any(kw in joined for kw in kws):
            return domain
    return '未知領域'


def handle_tutoring_conversation(
    user_email: str,
    question: str,
    user_answer: str,
    correct_answer: str,
    user_input: str = None,
    grading_feedback: dict = None,
    question_context: str = None,
    retrieval_context: str = None,
) -> dict:
    """
    處理AI教學對話 - 重構版本
    整合了會話管理、知識檢索、AI回應和學習進度更新
    新增：支援AI批改的評分反饋
    新增：把 GraphRAG usage 附進回傳，讓前端能顯示徽章
    """
    try:
        # 每次 request 開始清 GraphRAG usage，避免上一次污染
        try:
            reset_graphrag_usage()
        except Exception:
            pass

        # 開啟 GraphRAG trace（跟 web_ai_assistant 那條路徑同格式）
        try:
            from src.graphrag_trace import start_trace
            start_trace(
                question=question,
                user_id=user_email,
                platform="ai_tutoring",
                route="ai-teacher/tutoring",
            )
        except Exception as e:
            logger.debug(f"AI tutoring trace start skipped: {e}")

        # 1. 獲取或創建會話
        session = get_or_create_session(user_email, question)
        # This endpoint deliberately has one GraphRAG tutoring policy.  Do not
        # activate any old score-driven target transition for a persisted
        # session, even if that session was created before this migration.
        one_shot_graphrag = True
        conversation_history = session.get('conversation_history', [])

        # 2. 判斷是否為初始化（基於更新前的對話歷史）
        original_history_length = len(conversation_history)
        is_initial = original_history_length == 0

        # 3. 構建AI提示詞
        # question_context 是給教學模型看的完整題目脈絡（例如所有選項）。
        # GraphRAG 檢索、session key 與 trace 仍使用原始 question，避免被無關選項帶偏。
        teaching_question = question_context or question
        if is_initial:
            # 初始化：分析學生答案，提出引導問題
            prompt = build_initial_prompt(teaching_question, user_answer, correct_answer, grading_feedback)
        else:
            # 後續對話：基於學生回答進行教學
            prompt = build_followup_prompt(
                teaching_question,
                user_answer,
                correct_answer,
                user_input,
                conversation_history,
                grading_feedback,
                # one-shot 模式不把舊的核心/先備佇列寫回 Prompt。
                session=None if one_shot_graphrag else session,
            )

        # 4. GraphRAG 提示詞增強。
        #
        # one_shot（預設，固定論文檢索政策）：
        #   題目 -> Top-5 Seed；每個 Seed 的所有直接 Chunk 依原題各取 Top-3，
        #   合併後去重。五個 Seed 的所有 distance=1 PREREQUISITE_OF 先備 Concept
        #   全域排序取 Top-5；先備 Chunk 則從「所有 D1 Concept」的全部 Chunk
        #   先去重、再依原題排序取 Top-5。後續輪次只重用首輪證據。
        #
        # stateful（舊實驗相容）：
        #   第一輪只注入 Seed，後續依分數解鎖先備並沿用 active concept。
        frozen_seeds = (
            (session.get("initial_retrieval") or {}).get("seed_concepts") or []
        )
        if one_shot_graphrag and is_initial:
            # Initial retrieval must be based on the question context that is
            # safe for retrieval: question text plus options when available.
            # Student/correct answers and grading feedback remain in the teaching
            # prompt, but including them here leaks the expected answer and makes
            # both retrieval quality and the frozen top-five path invalid.
            # For short choice questions such as "Which statement is false?",
            # options are necessary to locate the real academic concept.
            retrieval_source = retrieval_context or question
            search_query = str(retrieval_source or "").strip()
            logger.info(
                f"🔍 [初始全域 GraphRAG] search_query={len(search_query)} 字，top_k=5"
            )
            # Only the first successful turn performs global question retrieval.
            enhanced_prompt = enhance_prompt_with_knowledge(
                prompt,
                search_query,
                top_k=5,
                rerank_question=search_query,
                graph_context_mode="prerequisite_d1",
                max_prereq_concepts=GRAPHRAG_MAX_PREREQ_CONCEPTS,
            )
            graphrag_usage_for_progress = get_last_graphrag_usage()
            graphrag_usage_for_progress.update({
                "candidate_concepts": list(
                    graphrag_usage_for_progress.get("seed_concepts") or []
                ),
                "candidate_prerequisite_concepts": list(
                    graphrag_usage_for_progress.get("injected_prerequisite_concepts")
                    or graphrag_usage_for_progress.get("prereq_chunk_concepts")
                    or []
                ),
                "concept_selection_policy": "llm_diagnosis_over_all_candidates",
                "primary_concept_selected": False,
            })
            _cache_one_shot_context(
                session,
                prompt,
                enhanced_prompt,
                graphrag_usage_for_progress,
            )
            _set_last_graphrag_usage(graphrag_usage_for_progress)
            _sync_one_shot_candidate_state(session, graphrag_usage_for_progress)
            session["one_shot_graphrag_completed"] = True
            session["one_shot_graphrag_usage"] = dict(
                graphrag_usage_for_progress or {}
            )
        elif one_shot_graphrag:
            # Follow-up chat deliberately avoids another global GraphRAG search
            # and does not create a core/prerequisite teaching queue.  It does,
            # however, receive the exact initial evidence again so it remains a
            # source-grounded tutoring response rather than silently becoming a
            # pure-LLM answer.
            enhanced_prompt, graphrag_usage_for_progress = _reuse_one_shot_context(
                session,
                prompt,
            )

            if graphrag_usage_for_progress.get("reason") in {
                "one_shot_context_cache_missing",
                "one_shot_context_policy_outdated",
            }:
                # Recover a session once when it predates cached evidence or its
                # evidence was built under an older selection policy.
                retrieval_source = retrieval_context or question
                search_query = str(retrieval_source or "").strip()
                logger.info(
                    "[GraphRAG one-shot] recovering missing legacy evidence cache; top_k=5"
                )
                enhanced_prompt = enhance_prompt_with_knowledge(
                    prompt,
                    search_query,
                    top_k=5,
                    rerank_question=search_query,
                    graph_context_mode="prerequisite_d1",
                    max_prereq_concepts=GRAPHRAG_MAX_PREREQ_CONCEPTS,
                )
                graphrag_usage_for_progress = get_last_graphrag_usage()
                graphrag_usage_for_progress.update({
                    "reason": "one_shot_context_cache_recovered",
                    "retrieval_mode": "one_shot_context_cache_recovery",
                    "candidate_concepts": list(
                        graphrag_usage_for_progress.get("seed_concepts") or []
                    ),
                    "candidate_prerequisite_concepts": list(
                        graphrag_usage_for_progress.get("injected_prerequisite_concepts")
                        or graphrag_usage_for_progress.get("prereq_chunk_concepts")
                        or []
                    ),
                    "concept_selection_policy": "llm_diagnosis_over_all_candidates",
                    "primary_concept_selected": False,
                    "context_reused": False,
                    "cache_recovery": True,
                })
                _cache_one_shot_context(
                    session,
                    prompt,
                    enhanced_prompt,
                    graphrag_usage_for_progress,
                )
                session["one_shot_graphrag_completed"] = True
                session["one_shot_graphrag_usage"] = dict(
                    graphrag_usage_for_progress or {}
                )

            _sync_one_shot_candidate_state(session, graphrag_usage_for_progress)
            _set_last_graphrag_usage(graphrag_usage_for_progress)
        elif is_initial or not frozen_seeds:
            retrieval_source = retrieval_context or question
            search_query = str(retrieval_source or "").strip()
            logger.info(
                f"🔍 [初始全域 GraphRAG] search_query={len(search_query)} 字，top_k=5"
            )
            enhanced_prompt = enhance_prompt_with_knowledge(
                prompt,
                search_query,
                top_k=5,
                rerank_question=search_query,
            )
            graphrag_usage_for_progress = get_last_graphrag_usage()
            _sync_session_retrieval_state(
                session,
                graphrag_usage_for_progress,
                question=question,
            )
            if is_initial:
                try:
                    # Do not choose a prerequisite from the initial quiz answer.
                    # The first tutoring reply must assess the frozen core. Only
                    # a later smart score below 40 can trigger direct-prerequisite
                    # ranking within the already frozen Seed set.
                    pass
                    # The first teaching response keeps the complete chunks
                    # attached to the frozen top-five seed concepts.  Priming may
                    # prepare a direct prerequisite for the *next* student turn,
                    # but it must not replace the initial observation prompt.
                except Exception as e:
                    logger.warning(f"⚠️ 第一輪錯題先輩規劃失敗: {e}")
        else:
            logger.info(
                "🧭 [有狀態 GraphRAG] 跳過全域重搜，使用 active concept=%s",
                session.get("active_teaching_concept") or session.get("active_core_concept"),
            )
            # Follow-up turns use the frozen top-five learning path and fetch
            # only the exact active core/prerequisite concept (or its cache).
            enhanced_prompt = _enhance_prompt_with_learning_state(
                prompt,
                session,
                question,
            )
            graphrag_usage_for_progress = get_last_graphrag_usage()

        # 5. 調用AI獲取回應
        ai_response = call_gemini_api(enhanced_prompt)

        # 6. 清理AI回應（移除評分等內部信息）
        clean_response = clean_ai_response(ai_response)

        # In one-shot mode the model, not Seed rank #1, chooses the concepts that
        # best explain the student's current gap. Persist only validated names
        # from the injected GraphRAG candidate set for audit and UI display.
        llm_selected_focus_concepts: List[str] = []
        if one_shot_graphrag:
            llm_selected_focus_concepts = extract_llm_selected_focus_concepts(
                ai_response,
                session.get("available_graphrag_concepts") or [],
            )
            session["llm_selected_focus_concepts"] = llm_selected_focus_concepts

        # 7. 記錄對話歷史（先記錄，再更新學習進度）
        if user_input:
            conversation_history.append({"role": "user", "content": user_input})
        conversation_history.append({"role": "assistant", "content": clean_response})
        session['conversation_history'] = conversation_history

        # 8. 更新學習進度
        # 判斷邏輯：如果有 user_input，說明這是用戶的回答，應該更新評分
        # 初始化階段（is_initial = True）只有 AI 回應，沒有用戶輸入，所以跳過
        raw_score = None
        if user_input:  # 如果有用戶輸入，說明用戶回答了問題，應該評分
            raw_score = extract_score_from_response(ai_response)
            if raw_score is not None:
                print(f"📊 用戶回答後，提取到AI評分：{raw_score}分，開始更新學習進度")
                update_learning_progress(session, question, ai_response, conversation_history, graphrag_usage=graphrag_usage_for_progress)
                if not one_shot_graphrag:
                    try:
                        from src.remedial_learning_state import update_after_scored_answer
                        # The staged 0--39/40--69/... policy is defined on the
                        # normalised smart score, not the raw model score.
                        remediation_score = int(
                            session.get("understanding_level", raw_score)
                        )
                        state_update = update_after_scored_answer(
                            session,
                            question=question,
                            user_input=user_input,
                            score=remediation_score,
                            grading_feedback=grading_feedback,
                        )
                        if (
                            state_update.get("action") == "core_mastered"
                            and remediation_score >= LEARNING_CONCEPT_READY_THRESHOLD
                        ):
                            # Locked descendants are not present in earlier prompts.
                            # Once the current core is genuinely mastered, select a
                            # single downstream concept for activation next turn.
                            maybe_recommend_next_concept(
                                session,
                                question,
                                graphrag_usage_for_progress,
                            )
                    except Exception as e:
                        logger.warning(f"⚠️ 更新有狀態先輩學習路徑失敗: {e}")
            else:
                print(f"⚠️ 用戶回答後未能提取評分，跳過學習進度更新")
        else:
            print(f"🎯 初始化階段（無用戶輸入），跳過評分更新")

        # 9. 保存會話到全局字典（使用與 get_or_create_session 相同的邏輯）
        from src.remedial_learning_state import stable_session_key
        session_key = stable_session_key(user_email, question)

        # 確保會話被正確保存
        learning_sessions[session_key] = session

        # 保存到文件以確保持久化
        #save_sessions_to_file()

        # 10. 計算對話次數
        conversation_count = sum(
            1 for msg in conversation_history if msg.get('role') == 'user'
        )

        # 收 GraphRAG usage 並關掉 trace（存 .json + .md 到 logs/graphrag_traces/）
        graphrag_usage = get_last_graphrag_usage()
        try:
            from src.graphrag_trace import finalize_trace
            finalize_trace(answer=clean_response, usage=graphrag_usage)
        except Exception:
            pass

        # ✨ 對話歷史持久化到 learning_sessions.json（重啟不消失）
        try:
            save_sessions_to_file()
        except Exception as e:
            logger.warning(f"⚠️ 對話持久化失敗（不影響回應）: {e}")

        current_smart_score = session.get('understanding_level', 0)
        current_stage = session.get('learning_stage', 'core_concept_confirmation')

        # ✨ 把教學對話進度寫入 MySQL tutoring_progress，讓知識診斷中心能看到
        try:
            from src.tutoring_progress import upsert_tutoring_progress
            seed_concepts = list(
                (graphrag_usage or {}).get('seed_concepts')
                or session.get('candidate_concepts')
                or []
            )
            # 所有候選 Seed 只用於領域推測，不選第一名作為教學核心。
            # 更精確的做法是查 Neo4j Concept.category，這裡先用簡單推論
            domain_guess = _guess_domain_from_seeds(seed_concepts)
            upsert_tutoring_progress(
                user_email=user_email,
                question_text=question,
                smart_score=int(current_smart_score),
                learning_stage=current_stage,
                conversation_count=conversation_count,
                concept_names=seed_concepts[:10],
                key_points=[],  # 之後由 ai_teacher 端補（那裡拿得到題目 metadata）
                domain=domain_guess,
                graphrag_usage=graphrag_usage,
            )
        except Exception as e:
            logger.warning(f"⚠️ 寫入 tutoring_progress 失敗（不影響對話）: {e}")

        if one_shot_graphrag:
            remedial_state = {}
        else:
            try:
                from src.remedial_learning_state import public_state
                remedial_state = public_state(session)
            except Exception:
                remedial_state = {}

        # 11. 返回結果 - 優化版本，包含更多信息
        return {
            'response': clean_response,
            'raw_score': raw_score,  # AI 原始評分（可能為 None）
            'smart_score': current_smart_score,  # 智能評分後的結果
            'learning_stage': current_stage,
            'concept_progress': session.get('concept_progress', []),
            'conversation_count': conversation_count,
            'is_initial': is_initial,
            'candidate_concepts': list(session.get('candidate_concepts') or []),
            'candidate_prerequisite_concepts': list(
                session.get('candidate_prerequisite_concepts') or []
            ),
            'llm_selected_focus_concepts': list(
                session.get('llm_selected_focus_concepts') or []
            ),
            'concept_selection_policy': session.get(
                'concept_selection_policy',
                'llm_diagnosis_over_all_candidates',
            ),
            'primary_concept_selected': False if one_shot_graphrag else bool(session.get('current_target')),
            'graphrag_tutoring_mode': GRAPHRAG_TUTORING_MODE,
            # ✨ 新增：讓前端能顯示「本次對話用了 GraphRAG 檢索到 N 個概念、M 條先輩鏈」
            'graphrag_usage': graphrag_usage,
            'remedial_learning_state': remedial_state,
        }

    except Exception as e:
        logger.error(f"❌ 教學對話處理失敗: {e}")
        return {
            'response': '抱歉，系統出現問題，請稍後再試。',
            'learning_stage': 'core_concept_confirmation',
            'understanding_level': 0,
            'concept_progress': []
        }

def update_learning_progress(
    session: dict,
    question: str,
    ai_response: str,
    conversation_history: list,
    graphrag_usage: Optional[Dict[str, Any]] = None,
):
    """
    更新學習進度 - 整合版本
    包含評分提取、智能評分計算和學習階段更新
    """
    try:
        # 1. 提取AI評分
        score = extract_score_from_response(ai_response)
        if score is None:
            print(f"⚠️ 未提取到評分，跳過學習進度更新")
            return

        # 2. Count tutoring turns.  In candidate-evidence mode a turn is never
        # reset by a change of teaching target, because teaching targets do not
        # exist in this policy.
        user_count = sum(1 for msg in conversation_history if msg.get('role') == 'user')
        conversation_count = user_count

        # 3. 獲取當前階段（在計算評分前）
        old_level = session.get('understanding_level', 0)
        old_stage = session.get('learning_stage', 'core_concept_confirmation')

        # 調試信息（在old_level定義後）
        print(f"📊 計算對話次數：對話歷史長度={len(conversation_history)}, user數量={user_count}, conversation_count={conversation_count}")
        print(f"📊 當前分數：{old_level}, AI評分：{score}")

        # 4. 智能評分計算（傳入當前階段和session，確保不跳階段並支援強制完成）
        # 注意：傳入當前的AI原始評分，用於強制完成判斷
        smart_score = calculate_smart_score(old_level, score, conversation_count, old_stage, session)
        session['understanding_level'] = smart_score


        # 5. 更新學習階段（基於新分數）
        new_stage = determine_learning_stage(smart_score)
        session['learning_stage'] = new_stage

        if old_stage != new_stage:
            print(f"🔄 學習階段更新：{old_stage} → {new_stage}")

        # 6. 記錄進度（在計算smart_score之後記錄，這樣下次計算時可以參考）
        record_progress(session, score, smart_score, new_stage)

        # Scores are learning-progress records only.  They never select a Seed,
        # unlock prerequisites, or cause another GraphRAG retrieval.

        # 7. 保存更新後的會話
        #save_sessions_to_file()


    except Exception as e:
        logger.error(f"❌ 學習進度更新失敗: {e}")

def calculate_smart_score(current_score: int, ai_score: int, conversation_count: int = 0, current_stage: str = None, session: dict = None) -> int:
    """
    智能評分計算 - 不限制加分版本，帶強制完成機制
    確保每個階段都被經歷過，避免直接跳階段
    在理解驗證階段，如果持續表現良好，自動提升到99分
    """
    try:
        # 定義階段分數範圍（每個階段的上限 = 下一階段下限 - 1）
        # 最後一個階段（理解驗證）包含99分，因為99分是完成標記
        stage_ranges = {
            'core_concept_confirmation': (0, 39),      # 核心概念確認：0-39分
            'related_concept_guidance': (40, 69),     # 相關概念引導：40-69分
            'application_understanding': (70, 89),    # 應用理解：70-89分
            'understanding_verification': (90, LEARNING_COMPLETION_MARKER_SCORE - 1),
            'completed': (LEARNING_COMPLETION_MARKER_SCORE, LEARNING_COMPLETION_MARKER_SCORE)
        }

        # 初始化階段：不給分數
        if conversation_count == 0:
            return 0

        elif conversation_count == 1:
            # 第一個問題回答：根據AI評分調整為合理範圍（0-30分）
            # 將AI評分映射到0-30分的範圍，作為初始評分
            # 例如：85分 -> 30分，60分 -> 20分，30分 -> 10分
            if ai_score >= 80:
                initial_score = 30  # 高分映射到30分
            elif ai_score >= 60:
                initial_score = 20  # 中等分映射到20分
            elif ai_score >= 40:
                initial_score = 15  # 偏低分映射到15分
            elif ai_score >= 20:
                initial_score = 10  # 低分映射到10分
            else:
                initial_score = 5   # 很低分映射到5分

            print(f"✅ 第一個問題回答（conversation_count=1），AI評分{ai_score}分，調整為初始評分{initial_score}分")
            print(f"📊 當前分數：{current_score} -> 新分數：{initial_score}")
            return initial_score

        # 之後的邏輯完全基於階段，不依賴對話次數
        # 根據當前分數確定當前階段（如果未提供）
        if not current_stage:
            if current_score >= 90:
                current_stage = 'understanding_verification'
            elif current_score >= 70:
                current_stage = 'application_understanding'
            elif current_score >= 40:
                current_stage = 'related_concept_guidance'
            else:
                current_stage = 'core_concept_confirmation'

        # 獲取當前階段的範圍
        stage_min, stage_max = stage_ranges.get(current_stage, (0, 99))

        print(f"📊 當前階段：{current_stage}，階段範圍：{stage_min}-{stage_max}，當前分數：{current_score}，AI評分：{ai_score}")

        # 新設計：理解驗證階段只要 AI 原始評分達到門檻，
        # 就視為目前概念已熟悉，可以進階學習新概念。
        # 這放在加減分之前，避免 90~93 因「沒有比目前分數更高」而卡住。
        if current_stage == 'understanding_verification' and ai_score >= LEARNING_CONCEPT_READY_THRESHOLD:
            print(
                f"🎯 理解驗證階段AI評分{ai_score}分已達概念熟悉門檻"
                f"{LEARNING_CONCEPT_READY_THRESHOLD}分，"
                f"標記為{LEARNING_COMPLETION_MARKER_SCORE}分（可進階）"
            )
            return LEARNING_COMPLETION_MARKER_SCORE

        if ai_score > current_score:
            # AI 評分更高：不限制加分，但不超過當前階段上限
            # 特殊處理：理解驗證階段的強制完成機制
            if current_stage == 'understanding_verification':
                # 新設計：漸進式評分實測常停在 90-93。
                # 因此只要 AI 原始評分達到概念熟悉門檻，就允許進階學習新概念。
                # 仍回傳 completion marker，維持舊有 completed 判斷相容。
                if ai_score >= LEARNING_CONCEPT_READY_THRESHOLD:
                    print(
                        f"🎯 AI評分{ai_score}分已達概念熟悉門檻"
                        f"{LEARNING_CONCEPT_READY_THRESHOLD}分，"
                        f"標記為{LEARNING_COMPLETION_MARKER_SCORE}分（可進階）"
                    )
                    return LEARNING_COMPLETION_MARKER_SCORE

                # 方案1：如果AI直接給99分，允許達到99分
                if ai_score >= LEARNING_COMPLETION_MARKER_SCORE:
                    print(f"🎯 AI評分達完成標記，直接完成")
                    return LEARNING_COMPLETION_MARKER_SCORE

                # 方案2：如果達到階段上限且 AI 評分達門檻，直接完成
                if (
                    current_score >= LEARNING_COMPLETION_MARKER_SCORE - 1
                    and ai_score >= LEARNING_CONCEPT_READY_THRESHOLD
                ):
                    print(
                        f"🎯 達到階段上限且AI評分{ai_score}分達門檻，"
                        f"自動提升到{LEARNING_COMPLETION_MARKER_SCORE}分（可進階）"
                    )
                    return LEARNING_COMPLETION_MARKER_SCORE

                # 方案3：如果當前分數已接近完成且 AI 評分達門檻，自動完成
                if (
                    current_score >= LEARNING_CONCEPT_READY_THRESHOLD
                    and ai_score >= LEARNING_CONCEPT_READY_THRESHOLD
                ):
                    print(
                        f"🎯 理解驗證階段已達概念熟悉（當前{current_score}分，"
                        f"AI評{ai_score}分），自動提升到"
                        f"{LEARNING_COMPLETION_MARKER_SCORE}分（可進階）"
                    )
                    return LEARNING_COMPLETION_MARKER_SCORE

                # 方案4：追蹤高分成績，如果連續多次高分，自動完成
                if session:
                    concept_progress = session.get('concept_progress', [])
                    # 檢查最近在理解驗證階段的原始AI評分
                    recent_scores = [
                        p.get('score', 0) for p in concept_progress
                        if p.get('stage') == 'understanding_verification'
                    ][-2:]  # 最近2次（不包括當前這次，因為還沒記錄）

                    # 如果最近2次AI原始評分都達門檻，且當前也達門檻，自動完成
                    if (
                        len(recent_scores) >= 2
                        and all(s >= LEARNING_CONCEPT_READY_THRESHOLD for s in recent_scores)
                        and ai_score >= LEARNING_CONCEPT_READY_THRESHOLD
                    ):
                        print(
                            f"🎯 理解驗證階段連續達標（歷史{recent_scores}，"
                            f"當前AI評{ai_score}分），自動提升到"
                            f"{LEARNING_COMPLETION_MARKER_SCORE}分（可進階）"
                        )
                        return LEARNING_COMPLETION_MARKER_SCORE

                    # 方案5：如果在理解驗證階段停留時間過長且表現良好，自動完成
                    # 統計在理解驗證階段的對話次數
                    verification_count = len([
                        p for p in concept_progress
                        if p.get('stage') == 'understanding_verification'
                    ])

                    # 如果在理解驗證階段已經有3次以上對話，且分數達門檻，自動完成
                    if (
                        verification_count >= 3
                        and current_score >= LEARNING_CONCEPT_READY_THRESHOLD
                        and ai_score >= LEARNING_CONCEPT_READY_THRESHOLD
                    ):
                        print(
                            f"🎯 理解驗證階段已進行{verification_count}次對話，"
                            f"表現達門檻（當前{current_score}分，AI評{ai_score}分），"
                            f"自動提升到{LEARNING_COMPLETION_MARKER_SCORE}分（可進階）"
                        )
                        return LEARNING_COMPLETION_MARKER_SCORE

            # 一般情況：基於當前階段推進
            # 如果還沒達到當前階段上限，在階段範圍內提升
            if current_score < stage_max:
                new_score = min(stage_max, ai_score)
                new_score = max(current_score, new_score)
                print(f"✅ 當前階段{current_stage}內提升：{current_score} -> {new_score}（階段上限：{stage_max}）")
                return new_score

            # 如果已經達到當前階段上限，且AI評分更高，進入下一個階段（不能跳階段）
            elif current_score >= stage_max and ai_score > stage_max:
                # 已達到階段上限，只允許進入下一個階段（逐步推進）
                stage_order = ['core_concept_confirmation', 'related_concept_guidance', 'application_understanding', 'understanding_verification', 'completed']
                current_index = stage_order.index(current_stage) if current_stage in stage_order else 0

                # 只進入下一個階段，不能跳階段
                if current_index < len(stage_order) - 1:
                    next_stage = stage_order[current_index + 1]
                    # 獲取下一個階段的範圍
                    next_min, next_max = stage_ranges.get(next_stage, (0, 99))

                    # 進入下一個階段時，分數應該是下一個階段的最小值或AI評分（取較高者，但不超過階段上限）
                    # 例如：從39分（核心概念確認上限）進入下一個階段，應該至少40分（相關概念引導最小值）
                    new_score = max(next_min, min(next_max, ai_score))
                    if (
                        next_stage == 'understanding_verification'
                        and new_score >= LEARNING_CONCEPT_READY_THRESHOLD
                    ):
                        print(
                            f"🎯 本輪已推進到{new_score}分，達概念熟悉門檻"
                            f"{LEARNING_CONCEPT_READY_THRESHOLD}分，"
                            f"標記為{LEARNING_COMPLETION_MARKER_SCORE}分（可進階）"
                        )
                        return LEARNING_COMPLETION_MARKER_SCORE
                    print(f"🎯 達到階段上限{stage_max}分（{current_stage}），AI評{ai_score}分，進入下一個階段{next_stage}，新分數：{new_score}分（範圍：{next_min}-{next_max}）")
                    return new_score
                else:
                    # 已經是最後階段，直接返回階段上限
                    print(f"🎯 已達最後階段{current_stage}上限{stage_max}分，AI評{ai_score}分，保持{stage_max}分")
                    return stage_max
            else:
                # 已經達到階段上限，但AI評分沒有更高，保持當前分數
                print(f"⚠️ 已達階段上限{stage_max}分，AI評{ai_score}分 <= 當前{current_score}分，保持當前分數")
                return current_score
        else:
            # AI 評分更低：給予扣分（但扣分幅度較小），確保不低於階段最小值
            penalty = min(2, current_score - ai_score)
            new_score = max(stage_min, current_score - penalty)
            print(f"⚠️ AI評分{ai_score}分 <= 當前{current_score}分，扣分後：{new_score}分（階段範圍：{stage_min}-{stage_max}）")
            return new_score

    except Exception as e:
        logger.error(f"❌ 智能評分計算失敗: {e}")
        return current_score

# ==================== RAG 功能 ====================

def should_search_database(question: str) -> bool:
    """
    智能判斷是否需要查詢向量資料庫
    過濾掉閒聊與網站操作問題，對課程概念與學習型問題進行知識檢索。
    """
    try:
        if not question or not question.strip():
            return False

        question_lower = question.lower()

        # 過濾掉明顯的非學術問題
        non_academic_patterns = [
            '你好', '早安', '晚安', '謝謝', '不客氣', '你是誰', '自我介紹',
            '天氣', '心情', '閒聊', '1+1', '簡單計算'
        ]
        if any(pattern in question_lower for pattern in non_academic_patterns):
            return False

        # 網站導覽 / 操作意圖通常應由網站助手工具處理，不拿去查 GraphRAG。
        site_operation_patterns = [
            '網站功能', '網站導覽', '這個網站', '怎麼使用網站', '如何使用網站',
            '登入', '註冊', '登出', '頁面', '按鈕', '選單', '側邊欄',
            '建立題目', '上傳檔案', '個人資料', '學習歷程'
        ]
        if any(pattern in question_lower for pattern in site_operation_patterns):
            return False

        # 放寬判定：閒聊 / 網站導覽已在前面被過濾掉，剩下的一律走 GraphRAG。
        # 舊版用 keyword whitelist 造成「什麼是 process」等短問題被誤判為非學術。
        # 現改為「白名單制」→「黑名單制」，讓 GraphRAG 側自己用向量檢索判斷相關性。
        # 如果檢索到 0 個 seed，enhance_prompt_with_knowledge 那邊會 fallback，
        # 不會多花什麼成本。
        return True

    except Exception as e:
        logger.error(f"❌ RAG判斷失敗: {e}")
        return False  # 預設不檢索


def _enhance_prompt_with_strict_research_policy(prompt: str, question: str) -> str:
    """Inject exactly the currently approved thesis GraphRAG evidence.

    Retrieval contract:
      - complete original question -> Top-5 Seed Concepts;
      - each Seed: all direct chunks -> semantic rank -> Top-3;
        merge the <=15 selections and deduplicate, without refill;
      - all distance-1 PREREQUISITE_OF Concepts -> semantic rank -> Top-5 Concepts;
      - only those selected Top-5 prerequisite Concepts contribute Chunks;
        their pool -> deduplicate first -> semantic rank -> Top-5 Chunks.

    Every semantic score is anchored to the same complete original question.
    """
    from src.graphrag_client import (
        GRAPHRAG_API_BASE,
        STRICT_RESEARCH_POLICY_VERSION,
        retrieve_research_policy,
    )

    original_question = str(question or "").strip()
    started = time.perf_counter()
    result = retrieve_research_policy(original_question)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)

    seed_hits = result.get("seed_hits") or []
    seed_names = result.get("seed_concepts") or []
    seed_chunks = result.get("seed_chunks") or []
    all_prereqs = result.get("all_prerequisite_concepts") or []
    prereq_top5 = result.get("prerequisite_concepts") or []
    prereq_chunks = result.get("prerequisite_chunks") or []
    retrieval_trace = result.get("retrieval_trace") or {}

    if result.get("error") or not seed_names:
        reason = "strict_policy_error" if result.get("error") else "no_hits"
        usage = _empty_graphrag_usage(reason)
        usage.update({
            "backend": "graphrag",
            "top_k": 5,
            "graph_context_mode": "prerequisite_d1",
            "selection_policy": STRICT_RESEARCH_POLICY_VERSION,
            "retrieval_policy": retrieval_trace,
            "error": result.get("error"),
        })
        _set_last_graphrag_usage(usage)
        try:
            from src.graphrag_trace import record_query
            record_query(
                question=original_question,
                top_k=5,
                api_base=GRAPHRAG_API_BASE,
                result=result,
                usage=usage,
                latency_ms=latency_ms,
                prompt_before_chars=len(prompt or ""),
                prompt_after_chars=len(prompt or ""),
                prompt_before_text=prompt,
                prompt_after_text=prompt,
                injected_context_text="",
            )
        except Exception:
            pass
        return prompt

    ctx_parts: List[str] = []
    context_chars = 0
    context_truncated = False

    def append_block(text: str) -> bool:
        nonlocal context_chars, context_truncated
        value = str(text or "")
        if not value:
            return True
        if context_chars + len(value) > GRAPHRAG_CONTEXT_CHAR_BUDGET:
            context_truncated = True
            return False
        ctx_parts.append(value)
        context_chars += len(value)
        return True

    # Seed Concepts: preserve vector score and definition for auditability.
    seed_lines = []
    hit_by_name = {str(item.get("name") or ""): item for item in seed_hits if isinstance(item, dict)}
    for rank, name in enumerate(seed_names, start=1):
        hit = hit_by_name.get(str(name), {})
        score = hit.get("score")
        score_text = f"{float(score):.6f}" if isinstance(score, (int, float)) else "N/A"
        definition = str(hit.get("definition") or "").strip()
        line = f"{rank}. {name}｜Seed semantic score={score_text}"
        if definition:
            line += f"｜定義：{definition}"
        seed_lines.append(line)
    append_block("\n\n**Top 5 Seed Concepts（完整原始題目語意檢索）：**\n" + "\n".join(seed_lines))

    # First-batch Seed chunks: already selected per Seed Top-3, then globally deduplicated.
    seed_chunk_blocks = []
    for idx, record in enumerate(seed_chunks, start=1):
        concepts = record.get("source_seed_concepts") or [record.get("concept")]
        concepts = [str(v) for v in concepts if v]
        header = (
            f"[{idx}] Seed Chunk｜來源 Seed={', '.join(concepts)}｜"
            f"semantic={float(record.get('semantic_score') or 0.0):.6f}｜"
            f"chunk_id={record.get('chunk_id') or 'N/A'}｜book={record.get('book_id') or 'N/A'}"
        )
        seed_chunk_blocks.append(header + "\n" + str(record.get("text") or ""))
    if seed_chunk_blocks:
        append_block(
            "\n\n**第一批教材 Chunk（每 Seed 各 Top 3，再合併去重；不補回 15）：**\n"
            + "\n\n".join(seed_chunk_blocks)
        )

    # Top-5 prerequisite Concepts are ranked independently from prerequisite chunks.
    prereq_lines = []
    for item in prereq_top5:
        prereq_lines.append(
            f"{item.get('global_prerequisite_rank')}. {item.get('name')}｜"
            f"semantic={float(item.get('semantic_score') or 0.0):.6f}｜"
            f"distance=1｜PREREQUISITE_OF→{', '.join(item.get('source_seed_concepts') or [])}"
            + (f"｜定義：{item.get('definition')}" if item.get("definition") else "")
        )
    if prereq_lines:
        append_block(
            "\n\n**Top 5 直接先備知識點（所有 distance=1 候選全域排名）：**\n"
            + "\n".join(prereq_lines)
        )

    # Prerequisite chunks come only from the selected D1 Top-5 concepts, then
    # are deduplicated before their final global Top-5 ranking.
    prereq_chunk_blocks = []
    for idx, record in enumerate(prereq_chunks, start=1):
        sources = [str(v) for v in (record.get("source_prerequisite_concepts") or []) if v]
        header = (
            f"[{idx}] Prerequisite Chunk｜來源 D1 Concept={', '.join(sources)}｜"
            f"semantic={float(record.get('semantic_score') or 0.0):.6f}｜"
            f"chunk_id={record.get('chunk_id') or 'N/A'}｜book={record.get('book_id') or 'N/A'}"
        )
        prereq_chunk_blocks.append(header + "\n" + str(record.get("text") or ""))
    if prereq_chunk_blocks:
        append_block(
            "\n\n**Top 5 直接先備教材 Chunk（僅由選入的 Top 5 D1 Concept 提供，先去重，再全域語意排名）：**\n"
            + "\n\n".join(prereq_chunk_blocks)
        )

    append_block(
        "\n\n**檢索規則提醒：上述 Seed、Seed Chunk、先備 Concept、先備 Chunk 的語意排名"
        "全部以同一份完整原始題目為基準。先備 Chunk 的候選池僅來自語意排序後選入的"
        "Top 5 distance=1 直接先備 Concept。**"
    )

    injected_context = "".join(ctx_parts)
    enhanced = (prompt or "") + injected_context
    top5_prereq_names = [str(item.get("name") or "") for item in prereq_top5 if item.get("name")]
    prereq_chunk_source_concepts = list(dict.fromkeys(
        str(name)
        for record in prereq_chunks
        for name in (record.get("source_prerequisite_concepts") or [])
        if name
    ))
    all_d1_names = [str(item.get("name") or "") for item in all_prereqs if item.get("name")]
    selected_prereq_relation_edge_count = sum(
        len(item.get("source_seed_concepts") or []) for item in prereq_top5
    )

    usage = _empty_graphrag_usage("used")
    usage.update({
        "used": True,
        "backend": "graphrag",
        "top_k": 5,
        "seed_count": len(seed_names),
        "seed_concepts": list(seed_names),
        "candidate_concepts": list(seed_names),
        "expanded_count": len(result.get("expanded") or []),
        "snippet_count": len(seed_chunks),
        "selected_chunk_count": len(seed_chunks) + len(prereq_chunks),
        "injected_chunk_count": len(seed_chunks) + len(prereq_chunks),
        "complete_injected_chunk_count": len(seed_chunks) + len(prereq_chunks),
        "retrieved_chunk_count": (
            int(retrieval_trace.get("seed_chunk_pre_dedup_count") or 0)
            + int(retrieval_trace.get("prerequisite_chunk_candidate_rows") or 0)
        ),
        "unique_retrieved_chunk_count": (
            int(retrieval_trace.get("seed_chunk_post_dedup_count") or 0)
            + int(retrieval_trace.get("prerequisite_chunk_post_dedup_count") or 0)
        ),
        "duplicate_chunk_count": (
            int(retrieval_trace.get("seed_chunk_pre_dedup_count") or 0)
            - int(retrieval_trace.get("seed_chunk_post_dedup_count") or 0)
            + int(retrieval_trace.get("prerequisite_chunk_candidate_rows") or 0)
            - int(retrieval_trace.get("prerequisite_chunk_post_dedup_count") or 0)
        ),
        "prereq_material_count": len(top5_prereq_names),
        "prereq_chunk_count": len(prereq_chunks),
        "prereq_chain_count": selected_prereq_relation_edge_count,
        "prereq_node_count": len(all_d1_names),
        "retrieved_prereq_material_count": len(all_d1_names),
        "retrieved_prereq_chunk_count": int(retrieval_trace.get("prerequisite_chunk_candidate_rows") or 0),
        "retrieved_prereq_chain_count": sum(
            len(item.get("source_seed_concepts") or []) for item in all_prereqs
        ),
        "retrieved_prereq_node_count": len(all_d1_names),
        "candidate_prerequisite_concepts": top5_prereq_names,
        "retrieved_prerequisite_concepts": all_d1_names,
        "injected_prerequisite_concepts": top5_prereq_names,
        "prereq_chunk_concepts": prereq_chunk_source_concepts,
        "graph_context_mode": "prerequisite_d1",
        "selection_policy": STRICT_RESEARCH_POLICY_VERSION,
        "concept_selection_policy": "top5_semantic_from_all_distance1_candidates",
        "primary_concept_selected": False,
        "prereq_chunk_depth": 1,
        "prerequisite_name_depth": 1,
        "prereq_chunks_per_concept": None,
        "context_char_budget": GRAPHRAG_CONTEXT_CHAR_BUDGET,
        "context_chars": len(injected_context),
        "context_utf8_bytes": len(injected_context.encode("utf-8")),
        "context_estimated_tokens": max(0, round(len(injected_context) / 4)),
        "prompt_before_chars": len(prompt or ""),
        "prompt_after_chars": len(enhanced),
        "context_truncated": context_truncated,
        "retrieval_policy": retrieval_trace,
        "chunk_rerank": {
            "scoring_mode": "complete_original_question_only",
            "seed_chunk_policy": "per_seed_top3_then_global_dedup_no_refill",
            "prerequisite_concept_policy": "all_d1_dedup_rank_top5",
            "prerequisite_chunk_policy": "selected_top5_d1_chunks_dedup_first_then_rank_top5",
        },
        "prerequisite_decisions": [
            {
                "concept": item.get("name"),
                "graph_distance": 1,
                "selected": item.get("name") in top5_prereq_names,
                "semantic_score": item.get("semantic_score"),
                "reason_code": (
                    "top5_semantic_direct_prerequisite"
                    if item.get("name") in top5_prereq_names
                    else "outside_top5_semantic_direct_prerequisite"
                ),
            }
            for item in all_prereqs
        ],
    })
    _set_last_graphrag_usage(usage)

    try:
        from src.graphrag_trace import record_query
        record_query(
            question=original_question,
            top_k=5,
            api_base=GRAPHRAG_API_BASE,
            result=result,
            usage=usage,
            latency_ms=latency_ms,
            prompt_before_chars=len(prompt or ""),
            prompt_after_chars=len(enhanced),
            prompt_before_text=prompt,
            prompt_after_text=enhanced,
            injected_context_text=injected_context,
        )
    except Exception:
        pass
    return enhanced

def enhance_prompt_with_knowledge(
    prompt: str,
    question: str,
    top_k: Optional[int] = None,
    rerank_question: Optional[str] = None,
    *,
    prerequisite_name_depth: int = 1,
    understanding_score: Optional[int] = None,
    graph_context_mode: str = "seed_only",
    max_prereq_concepts: int = 5,
) -> str:
    """依 RAG_BACKEND 分派：預設 GraphRAG，可切換到 ChromaDB。

    切換方式（見 src/rag_backend.py）：
        - env `RAG_BACKEND=chromadb`
        - runtime API：`POST /api/rag/backend {"backend": "chromadb"}`
        - request-scope：`with with_backend('chromadb'): ...`

    兩個 backend 都保留：
        - graphrag → GraphRAG /query（種子概念 + 上下游鏈 + 教材片段）
        - chromadb → 舊 ChromaDB 向量檢索
        - llm_only → 純 LLM baseline（不注入任何 RAG 資料）
    """
    # === 選 backend ===
    try:
        from src.rag_backend import get_active_backend
        backend = get_active_backend()
    except Exception:
        backend = "graphrag"

    # 🆕 llm_only baseline：完全不呼叫 RAG，讓 LLM 純靠自身知識回答
    # 用途：論文對照組。同一題 3 種模式（GraphRAG / ChromaDB / 純 LLM）比效果
    if backend == "llm_only":
        usage = _empty_graphrag_usage("llm_only_baseline")
        usage["used"] = False
        usage["backend"] = "llm_only"
        usage["context_chars"] = 0
        _set_last_graphrag_usage(usage)
        return prompt  # 原 prompt 直接送 LLM，不注入任何檢索資料

    if backend == "chromadb":
        # 走舊 RAG（沒閒聊過濾，因為舊版就沒做）
        try:
            from src.chromadb_rag import enhance_prompt_chromadb
            before_chars = len(prompt or "")
            selected_top_k = max(1, min(int(top_k if top_k is not None else 2), 30))
            enhanced = enhance_prompt_chromadb(prompt, question, top_k=selected_top_k)
            after_chars = len(enhanced or "")
            injected_context = (
                enhanced[len(prompt):]
                if enhanced.startswith(prompt)
                else enhanced
            )
            # 🔧 修 bug：ChromaDB 分支之前完全沒動 GraphRAG usage，
            # 導致前端徽章一直顯示「GraphRAG：未使用（not_used）」
            # 現在標示為「backend=chromadb 已被使用」，避免混淆
            usage = _empty_graphrag_usage("chromadb_backend")
            usage["used"] = after_chars > before_chars  # 有塞東西進去才算 used
            usage["backend"] = "chromadb"
            usage["top_k"] = selected_top_k
            usage["context_chars"] = len(injected_context)
            usage["context_utf8_bytes"] = len(injected_context.encode("utf-8"))
            usage["context_estimated_tokens"] = max(0, round(len(injected_context) / 4))
            usage["prompt_before_chars"] = before_chars
            usage["prompt_after_chars"] = after_chars
            _set_last_graphrag_usage(usage)
            return enhanced
        except Exception as e:
            logger.error(f"❌ ChromaDB backend 失敗，退回原始 prompt: {e}")
            usage = _empty_graphrag_usage("chromadb_error")
            _set_last_graphrag_usage(usage)
            return prompt

    # === 以下為 GraphRAG 流程（預設）===
    try:
        # 1. 判斷是否需要檢索知識
        if not should_search_database(question):
            usage = _empty_graphrag_usage("skipped_non_academic")
            _set_last_graphrag_usage(usage)
            try:
                from src.graphrag_trace import record_skip
                record_skip("skipped_non_academic")
            except Exception:
                pass
            return prompt

        # 2. 走 GraphRAG
        from src.graphrag_client import (
            GRAPHRAG_API_BASE,
            query_natural_language,
        )

        trace_started_at = time.perf_counter()
        selected_top_k = max(1, min(int(top_k if top_k is not None else GRAPHRAG_TOP_K), 30))
        normalized_graph_mode = str(graph_context_mode or "seed_only").strip().lower()
        if normalized_graph_mode not in {"seed_only", "prerequisite_d1"}:
            normalized_graph_mode = "seed_only"
        safe_max_prereq_concepts = max(0, min(int(max_prereq_concepts or 0), 5))

        # The production one-shot prerequisite_d1 path is fixed to the thesis policy.
        # Do not enter the legacy seed-order / per-prerequisite truncation logic below.
        if normalized_graph_mode == "prerequisite_d1":
            return _enhance_prompt_with_strict_research_policy(prompt, question)

        result = query_natural_language(question, top_k=selected_top_k)
        latency_ms = round((time.perf_counter() - trace_started_at) * 1000, 2)
        seeds = result.get("seed_concepts") or []
        expanded = result.get("expanded") or []
        prereq_chains = result.get("prereq_chains") or []
        descendant_chains = result.get("descendant_chains") or []
        # The default chat behavior remains seed-only.  Formal experiments can
        # explicitly request prerequisite_d1, which injects only deterministic
        # distance-one prerequisite evidence and records every decision.
        descendant_context_unlocked = False

        if not seeds and not expanded:
            # 🔧 區分「上游連不到」跟「上游有回但 0 hits」
            # 以前都標 no_hits，讓人以為 Neo4j 沒此概念
            # 其實常見的是 ComputerScienceKG api_server 沒啟動
            reason = "upstream_offline" if result.get("error") else "no_hits"
            if reason == "upstream_offline":
                logger.warning(f"⚠️ [GraphRAG] 上游 api_server 連不到: {result.get('error')}")
            else:
                logger.info("ℹ️ [GraphRAG] 上游有回但 seeds=0，沿用原始 prompt")
            usage = _empty_graphrag_usage(reason)
            _set_last_graphrag_usage(usage)
            try:
                from src.graphrag_trace import record_query
                record_query(
                    question=question,
                    top_k=selected_top_k,
                    api_base=GRAPHRAG_API_BASE,
                    result=result,
                    usage=usage,
                    latency_ms=latency_ms,
                    prompt_before_chars=len(prompt),
                    prompt_after_chars=len(prompt),
                    # ✨ 論文對比用：即使沒 hit 也記錄原始 prompt
                    prompt_before_text=prompt,
                    prompt_after_text=prompt,
                    injected_context_text="",
                )
            except Exception:
                pass
            return prompt

        # 3. 拼結構化 context。先排除空資料與重複資料，再受總字數預算控制。
        ctx_parts: List[str] = []
        context_chars = 0
        context_truncated = False

        def append_context(text: str) -> bool:
            nonlocal context_chars, context_truncated
            if not text:
                return True
            if context_chars + len(text) > GRAPHRAG_CONTEXT_CHAR_BUDGET:
                context_truncated = True
                return False
            ctx_parts.append(text)
            context_chars += len(text)
            return True

        def append_section(title: str, items: List[str]) -> int:
            nonlocal context_truncated
            if not items:
                return 0
            included = 0
            header_pending = f"\n\n**{title}：**"
            for item in items:
                block = f"{header_pending}\n{item}" if header_pending else f"\n{item}"
                if not append_context(block):
                    break
                header_pending = ""
                included += 1
            if included < len(items):
                context_truncated = True
            return included

        def unique_names(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            unique: List[Dict[str, Any]] = []
            seen = set()
            for node in nodes:
                name = clean_concept_name(node.get("name"))
                if not name or name in seen:
                    continue
                seen.add(name)
                node = dict(node)
                node["name"] = name
                unique.append(node)
            return unique

        def clean_concept_name(value: Any) -> str:
            """Normalize concept labels used only for prompt display and snippet lookup."""
            name = str(value or "").strip()
            if len(name) >= 2 and name[0] == name[-1] and name[0] in {"'", '"'}:
                name = name[1:-1].strip()
            return name

        def shorten_text(value: Any, limit: int = GRAPHRAG_TEXT_ITEM_MAX_CHARS) -> str:
            """Bound structured definitions/paths only; textbook chunks stay verbatim."""
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            if len(text) <= limit:
                return text
            return text[:limit].rstrip() + "..."

        chunk_audit_records: List[Dict[str, Any]] = []

        def make_chunk_record(
            *,
            section: str,
            concept: str,
            raw_content: Any,
            ordinal: int,
            source: Any = None,
            source_ids: Any = None,
            chunk_seq_id: Any = None,
            upstream_chunk_id: Any = None,
            chapter_id: Any = None,
            page_start: Any = None,
            page_end: Any = None,
            char_start: Any = None,
            char_end: Any = None,
            upstream_text_hash: Any = None,
        ) -> Optional[Dict[str, Any]]:
            """Create an unabridged retrieval snapshot for thesis/audit use."""
            raw_text = str(raw_content or "")
            if not raw_text.strip():
                return None
            digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
            if isinstance(source_ids, (list, tuple, set)):
                normalized_source_ids = [str(value) for value in source_ids if value]
            elif source_ids:
                normalized_source_ids = [str(source_ids)]
            else:
                normalized_source_ids = []
            return {
                "section": section,
                "concept": concept,
                "concepts": [concept] if concept else [],
                "ordinal": ordinal,
                "chunk_id": str(upstream_chunk_id or digest),
                "content_sha256": digest,
                "source": str(source or ""),
                "source_ids": normalized_source_ids,
                "chunk_seq_id": chunk_seq_id,
                "chapter_id": str(chapter_id or ""),
                "page_start": page_start,
                "page_end": page_end,
                "char_start": char_start,
                "char_end": char_end,
                "upstream_text_hash": str(upstream_text_hash or ""),
                "raw_content": raw_text,
                "raw_chars": len(raw_text),
                "injected_content": "",
                "injected_chars": 0,
                "is_complete": False,
                "omitted": True,
                "omitted_reason": "not_processed",
            }

        def mark_chunk_records(records: List[Dict[str, Any]], injected: bool) -> None:
            for record in records:
                raw_text = record["raw_content"]
                record["injected_content"] = raw_text if injected else ""
                record["injected_chars"] = len(raw_text) if injected else 0
                record["is_complete"] = bool(injected)
                record["omitted"] = not injected
                record["omitted_reason"] = None if injected else "context_budget"

        unique_seeds = list(dict.fromkeys(clean_concept_name(seed) for seed in seeds if seed))
        # Preserve the upstream top-k order exactly and ignore any non-seed
        # expansion returned alongside it.  This makes first-turn Chunk output a
        # direct observation of the original five concepts rather than graph
        # traversal output.
        expanded_by_name: Dict[str, Dict[str, Any]] = {}
        for expanded_item in expanded:
            expanded_name = clean_concept_name(expanded_item.get("name"))
            if expanded_name:
                expanded_by_name.setdefault(expanded_name.casefold(), expanded_item)
        initial_seed_expanded = [
            expanded_by_name[seed.casefold()]
            for seed in unique_seeds
            if seed.casefold() in expanded_by_name
        ]
        append_context("\n\n**【GraphRAG 知識圖譜檢索結果】**")
        included_seed_count = 0
        if unique_seeds and append_context(f"\n命中概念：{', '.join(unique_seeds)}"):
            included_seed_count = len(unique_seeds)

        # 初次檢索只顯示 top-k 核心定義。先輩、後續與子類由後續有狀態
        # 學習流程按學生表現解鎖，不在第一輪 Prompt 提前出現。
        concept_lines: List[str] = []
        seen_concepts = set()
        for item in initial_seed_expanded:
            name = clean_concept_name(item.get("name"))
            if not name or name in seen_concepts:
                continue
            seen_concepts.add(name)
            details: List[str] = []
            definition = str(item.get("definition") or "").strip()
            if definition:
                details.append(f"定義：{shorten_text(definition)}")
            concept_lines.append(
                f"- {name}｜" + "；".join(details) if details else f"- {name}"
            )
        included_expanded_count = append_section("初始 Top-K 概念定義", concept_lines)

        # seed_only 保持原本第一輪行為；prerequisite_d1 僅允許距離一，
        # 並把圖路徑、來源 seed、選擇或拒絕理由完整留在 trace。
        safe_prerequisite_name_depth = (
            1 if normalized_graph_mode == "prerequisite_d1" else 0
        )

        retrieved_prereq_node_count = sum(
            len(unique_names(chain.get("ancestors") or []))
            for chain in prereq_chains
        )
        prereq_line_count = 0
        prereq_node_count = 0
        prereq_material_items: List[Dict[str, Any]] = []
        retrieved_prereq_chunk_count = 0

        retrieved_descendant_node_count = sum(
            len(unique_names(chain.get("descendants") or []))
            for chain in descendant_chains
        )
        descendant_line_count = 0
        descendant_node_count = 0
        prerequisite_decisions: List[Dict[str, Any]] = []
        retrieved_prerequisite_concepts: List[str] = []

        # The external /retrieve response historically returned seed Chunk
        # text without a source/chunk locator.  In the explainable experiment
        # mode, resolve those exact texts against the local Neo4j Chunk nodes.
        # Matching is deliberately exact (SHA-256 + text equality): a fuzzy
        # match would make source traceability look better than the evidence
        # actually supports.
        seed_chunk_metadata_total = 0
        seed_chunk_metadata_resolved = 0
        seed_chunk_metadata_unresolved = 0
        if normalized_graph_mode == "prerequisite_d1":
            from src.graphrag_client import fetch_concept_profile

            def _record_has_precise_locator(value: Dict[str, Any]) -> bool:
                return bool(
                    value.get("source")
                    and (
                        value.get("chunk_seq_id") is not None
                        or value.get("page_start") is not None
                        or value.get("char_start") is not None
                    )
                )

            for item in initial_seed_expanded:
                concept_name = clean_concept_name(item.get("name")) or "?"
                upstream_values = item.get("sample_chunk_records") or [
                    {"text": snippet}
                    for snippet in (item.get("sample_chunks") or [])
                ]
                normalized_values: List[Dict[str, Any]] = []
                needs_lookup = False
                for value in upstream_values:
                    record = dict(value) if isinstance(value, dict) else {"text": str(value or "")}
                    record["text"] = str(record.get("text") or "")
                    if record["text"]:
                        normalized_values.append(record)
                        needs_lookup = needs_lookup or not _record_has_precise_locator(record)

                local_records: List[Dict[str, Any]] = []
                if needs_lookup and normalized_values:
                    profile = fetch_concept_profile(
                        concept_name,
                        max_snippets=GRAPHRAG_SOURCE_METADATA_LOOKUP_MAX,
                        include_leads_to=False,
                        include_context_relations=False,
                    )
                    local_records = [
                        dict(value)
                        for value in (profile.get("sample_chunk_records") or [])
                        if isinstance(value, dict) and str(value.get("text") or "")
                    ]

                records_by_hash: Dict[str, List[Dict[str, Any]]] = {}
                for local_record in local_records:
                    local_text = str(local_record.get("text") or "")
                    digest = hashlib.sha256(local_text.encode("utf-8")).hexdigest()
                    records_by_hash.setdefault(digest, []).append(local_record)

                enriched_values: List[Dict[str, Any]] = []
                for index, record in enumerate(normalized_values, start=1):
                    seed_chunk_metadata_total += 1
                    text = str(record.get("text") or "")
                    if _record_has_precise_locator(record):
                        enriched = dict(record)
                        status = "upstream_precise_locator"
                    else:
                        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                        exact_match = None
                        for candidate in records_by_hash.get(digest, []):
                            if str(candidate.get("text") or "") == text:
                                exact_match = candidate
                                break
                        if exact_match is not None:
                            enriched = dict(exact_match)
                            # Preserve any upstream metadata that is not part of
                            # the local profile while keeping the exact text that
                            # was actually retrieved and injected.
                            for key, value in record.items():
                                if value not in (None, "", [], {}):
                                    enriched[key] = value
                            enriched["text"] = text
                            status = "exact_neo4j_text_match"
                        else:
                            enriched = dict(record)
                            status = "unresolved_exact_text"

                    enriched["metadata_match_status"] = status
                    enriched["metadata_lookup_concept"] = concept_name
                    enriched["upstream_sample_index"] = index
                    if _record_has_precise_locator(enriched):
                        seed_chunk_metadata_resolved += 1
                    else:
                        seed_chunk_metadata_unresolved += 1
                    enriched_values.append(enriched)

                if enriched_values:
                    item["sample_chunk_records"] = enriched_values
                    item["chunk_metadata_enrichment"] = {
                        "method": "exact_neo4j_text_match",
                        "total": len(enriched_values),
                        "resolved": sum(
                            1 for value in enriched_values
                            if _record_has_precise_locator(value)
                        ),
                    }

        # 收集核心概念候選 Chunk。這裡刻意不先去重，讓 trace 可以完整記錄
        # 「同一段教材被多少個概念取回」；全域去重會在第二階段重排時處理。
        snippet_items: List[Dict[str, Any]] = []
        for item in initial_seed_expanded:
            concept_name = clean_concept_name(item.get("name")) or "?"
            raw_core_records = item.get("sample_chunk_records") or [
                {"text": snippet}
                for snippet in (item.get("sample_chunks") or [])
            ]
            for ordinal, chunk_data in enumerate(raw_core_records, start=1):
                if isinstance(chunk_data, dict):
                    text = str(chunk_data.get("text") or "")
                    source = chunk_data.get("source") or item.get("source")
                    source_ids = (
                        chunk_data.get("book_id")
                        or chunk_data.get("source_ids")
                        or item.get("book_ids")
                    )
                    chunk_seq_id = chunk_data.get("chunk_seq_id")
                    upstream_chunk_id = chunk_data.get("chunk_id")
                    chapter_id = chunk_data.get("chapter_id")
                    page_start = chunk_data.get("page_start")
                    page_end = chunk_data.get("page_end")
                    char_start = chunk_data.get("char_start")
                    char_end = chunk_data.get("char_end")
                    upstream_text_hash = chunk_data.get("text_hash")
                else:
                    text = str(chunk_data or "")
                    source = item.get("source")
                    source_ids = item.get("book_ids")
                    chunk_seq_id = None
                    upstream_chunk_id = None
                    chapter_id = None
                    page_start = None
                    page_end = None
                    char_start = None
                    char_end = None
                    upstream_text_hash = None
                if not text.strip():
                    continue
                record = make_chunk_record(
                    section="core",
                    concept=concept_name,
                    raw_content=text,
                    ordinal=ordinal,
                    source=source,
                    source_ids=source_ids,
                    chunk_seq_id=chunk_seq_id,
                    upstream_chunk_id=upstream_chunk_id,
                    chapter_id=chapter_id,
                    page_start=page_start,
                    page_end=page_end,
                    char_start=char_start,
                    char_end=char_end,
                    upstream_text_hash=upstream_text_hash,
                )
                if record is None:
                    continue
                record["concept_definition"] = shorten_text(item.get("definition"))
                record["retrieval_stage"] = "seed"
                record["source_seed_concept"] = concept_name
                record["source_seed_rank"] = unique_seeds.index(concept_name) + 1
                record["graph_distance"] = 0
                record["graph_path"] = []
                if isinstance(chunk_data, dict):
                    record["metadata_match_status"] = chunk_data.get("metadata_match_status")
                    record["metadata_lookup_concept"] = chunk_data.get("metadata_lookup_concept")
                    record["upstream_sample_index"] = chunk_data.get("upstream_sample_index")
                snippet_items.append({"record": record, "concept": concept_name})
                chunk_audit_records.append(record)

        # 初次檢索不做語意排名、分數門檻或章節雜訊篩選。所有直接掛在
        # top-k seed 上的完整 Chunk 都依 seed/上游順序輸出；只有完全相同
        # 的實體 Chunk 合併一次，避免重複注入相同文字。
        from src.graphrag_chunk_reranker import select_initial_seed_chunks

        rerank_result = select_initial_seed_chunks(chunk_audit_records)
        chunk_audit_records = rerank_result["records"]
        selected_chunk_records = rerank_result["selected"]
        rerank_summary = rerank_result["summary"]

        for record in chunk_audit_records:
            if record.get("selected_for_injection"):
                record["selection_reason"] = "initial_seed_direct_chunk"
            elif record.get("omitted_reason") == "duplicate_content":
                record["selection_reason"] = "duplicate_content"

        if normalized_graph_mode == "prerequisite_d1":
            chains_by_seed = {
                clean_concept_name(chain.get("concept")).casefold(): chain
                for chain in prereq_chains
                if clean_concept_name(chain.get("concept"))
            }
            seen_prereq_names = set()
            seen_selected_hashes = {
                str(record.get("content_sha256") or "")
                for record in selected_chunk_records
            }
            selected_prereq_concepts = 0
            next_candidate_index = len(chunk_audit_records) + 1

            for seed_rank, seed_name in enumerate(unique_seeds, start=1):
                chain = chains_by_seed.get(seed_name.casefold()) or {}
                for ancestor in unique_names(chain.get("ancestors") or []):
                    prereq_name = clean_concept_name(ancestor.get("name"))
                    distance = max(1, int(
                        ancestor.get("distance") or ancestor.get("depth") or 1
                    ))
                    decision = {
                        "concept": prereq_name,
                        "retrieval_stage": "prerequisite",
                        "source_seed_concept": seed_name,
                        "source_seed_rank": seed_rank,
                        "graph_distance": distance,
                        "graph_path": [{
                            "from": prereq_name,
                            "relation": "PREREQUISITE_OF",
                            "to": seed_name,
                        }],
                        "selected": False,
                        "reason_code": None,
                        "selected_chunk_ids": [],
                    }
                    if prereq_name:
                        retrieved_prerequisite_concepts.append(prereq_name)
                    if not prereq_name:
                        decision["reason_code"] = "missing_concept_name"
                        prerequisite_decisions.append(decision)
                        continue
                    prereq_key = prereq_name.casefold()
                    if distance != 1:
                        decision["reason_code"] = "graph_distance_not_one"
                        prerequisite_decisions.append(decision)
                        continue
                    if prereq_key in seen_prereq_names:
                        decision["reason_code"] = "duplicate_prerequisite_concept"
                        prerequisite_decisions.append(decision)
                        continue
                    seen_prereq_names.add(prereq_key)
                    if selected_prereq_concepts >= safe_max_prereq_concepts:
                        decision["reason_code"] = "prerequisite_concept_limit"
                        prerequisite_decisions.append(decision)
                        continue

                    profile = fetch_concept_profile(
                        prereq_name,
                        max_snippets=max(GRAPHRAG_PREREQ_CHUNKS_PER_CONCEPT, 1),
                        include_leads_to=False,
                        include_context_relations=True,
                    )
                    resolved_name = clean_concept_name(profile.get("name")) or prereq_name
                    raw_values = profile.get("sample_chunk_records") or [
                        {"text": text}
                        for text in (profile.get("sample_chunks") or [])
                    ]
                    selected_for_concept: List[Dict[str, Any]] = []
                    for ordinal, chunk_data in enumerate(
                        raw_values[:GRAPHRAG_PREREQ_CHUNKS_PER_CONCEPT],
                        start=1,
                    ):
                        if isinstance(chunk_data, dict):
                            text = str(chunk_data.get("text") or "")
                            source = chunk_data.get("source") or profile.get("source")
                            source_ids = (
                                chunk_data.get("book_id")
                                or chunk_data.get("source_ids")
                                or profile.get("book_ids")
                            )
                            chunk_seq_id = chunk_data.get("chunk_seq_id")
                            upstream_chunk_id = chunk_data.get("chunk_id")
                            chapter_id = chunk_data.get("chapter_id")
                            page_start = chunk_data.get("page_start")
                            page_end = chunk_data.get("page_end")
                            char_start = chunk_data.get("char_start")
                            char_end = chunk_data.get("char_end")
                            upstream_text_hash = chunk_data.get("text_hash")
                        else:
                            text = str(chunk_data or "")
                            source = profile.get("source")
                            source_ids = profile.get("book_ids")
                            chunk_seq_id = None
                            upstream_chunk_id = None
                            chapter_id = None
                            page_start = None
                            page_end = None
                            char_start = None
                            char_end = None
                            upstream_text_hash = None
                        record = make_chunk_record(
                            section="prerequisite",
                            concept=resolved_name,
                            raw_content=text,
                            ordinal=ordinal,
                            source=source,
                            source_ids=source_ids,
                            chunk_seq_id=chunk_seq_id,
                            upstream_chunk_id=upstream_chunk_id,
                            chapter_id=chapter_id,
                            page_start=page_start,
                            page_end=page_end,
                            char_start=char_start,
                            char_end=char_end,
                            upstream_text_hash=upstream_text_hash,
                        )
                        if record is None:
                            continue
                        record.update({
                            "candidate_index": next_candidate_index,
                            "retrieval_stage": "prerequisite",
                            "source_seed_concept": seed_name,
                            "source_seed_rank": seed_rank,
                            "prerequisite_targets": [seed_name],
                            "prerequisite_target_ranks": {seed_name: seed_rank},
                            "prerequisite_distance": 1,
                            "graph_distance": 1,
                            "graph_path": list(decision["graph_path"]),
                            "concept_definition": shorten_text(profile.get("definition")),
                            "selection_reason": "direct_prerequisite_distance_1_seed_order",
                            "scoring_basis": "graph_distance_then_seed_order",
                            "selection_status": "selected",
                            "selected_for_injection": True,
                            "selection_priority": len(selected_chunk_records) + 1,
                            "relevance_rank": len(selected_chunk_records) + 1,
                            "omitted_reason": "awaiting_prompt_budget",
                        })
                        next_candidate_index += 1
                        digest = str(record.get("content_sha256") or "")
                        if digest in seen_selected_hashes:
                            record.update({
                                "selection_status": "rejected",
                                "selected_for_injection": False,
                                "selection_reason": "duplicate_content",
                                "omitted_reason": "duplicate_content",
                            })
                            chunk_audit_records.append(record)
                            continue
                        seen_selected_hashes.add(digest)
                        chunk_audit_records.append(record)
                        selected_chunk_records.append(record)
                        selected_for_concept.append(record)

                    if selected_for_concept:
                        selected_prereq_concepts += 1
                        decision["selected"] = True
                        decision["reason_code"] = "direct_prerequisite_with_textbook_evidence"
                        decision["selected_chunk_ids"] = [
                            record.get("chunk_id") for record in selected_for_concept
                        ]
                    else:
                        decision["reason_code"] = "no_unique_complete_textbook_chunk"
                    prerequisite_decisions.append(decision)

            rerank_summary.update({
                "scoring_mode": "seed_chunks_plus_deterministic_prerequisite_d1",
                "max_prereq_chunks": safe_max_prereq_concepts,
                "selected_prereq_count": sum(
                    1 for decision in prerequisite_decisions if decision.get("selected")
                ),
            })

        selected_prompt_items: List[str] = []
        for record in selected_chunk_records:
            concepts = record.get("concepts") or [record.get("concept") or "?"]
            if record.get("section") == "prerequisite":
                path = record.get("graph_path") or []
                path_text = "；".join(
                    f"{edge.get('from')} --{edge.get('relation')}--> {edge.get('to')}"
                    for edge in path
                )
                header_lines = [
                    f"- [距離 1 先備概念｜{', '.join(concepts)}｜{path_text}｜"
                    f"選擇理由：{record.get('selection_reason')}]",
                ]
            else:
                header_lines = [
                    f"- [題目相關候選概念｜{', '.join(concepts)}｜"
                    f"選擇理由：{record.get('selection_reason')}]",
                ]
            definition = str(record.get("concept_definition") or "").strip()
            if definition:
                header_lines.append(f"  定義：{definition}")
            header_lines.extend(["  完整教材 Chunk：", record["raw_content"]])
            selected_prompt_items.append("\n".join(header_lines))

        selected_injected_count = append_section(
            (
                "Top-K 候選概念與距離 1 先備概念的完整教材 Chunk"
                if normalized_graph_mode == "prerequisite_d1"
                else "Top-K 候選概念直接關聯的完整教材 Chunk"
            ),
            selected_prompt_items,
        )
        for index, record in enumerate(selected_chunk_records):
            mark_chunk_records([record], injected=index < selected_injected_count)

        injected_selected_records = selected_chunk_records[:selected_injected_count]
        prereq_material_chunk_count = sum(
            1 for record in injected_selected_records
            if record.get("section") == "prerequisite"
        )
        snippet_count = sum(
            1 for record in injected_selected_records
            if record.get("section") == "core"
        )
        prereq_material_concepts = list(dict.fromkeys(
            str(record.get("concept") or "")
            for record in injected_selected_records
            if record.get("section") == "prerequisite" and record.get("concept")
        ))
        prereq_material_line_count = len(prereq_material_concepts)
        injected_prereq_records = [
            record for record in injected_selected_records
            if record.get("section") == "prerequisite"
        ]
        prereq_line_count = len({
            (
                str(record.get("concept") or ""),
                str(record.get("source_seed_concept") or ""),
            )
            for record in injected_prereq_records
        })
        prereq_node_count = len({
            str(node)
            for record in injected_prereq_records
            for node in (
                record.get("concept"),
                record.get("source_seed_concept"),
            )
            if node
        })
        prereq_material_items = [
            record for record in chunk_audit_records
            if record.get("section") == "prerequisite"
        ]
        retrieved_prereq_chunk_count = len(prereq_material_items)

        append_context(
            "\n\n**請只使用上述已標示來源類型、圖路徑與選擇理由的完整教材 Chunk 作答。"
            + (
                "本輪包含 Top-K 題目相關候選概念 Chunk，以及固定規則選入的距離 1 先備 Chunk。"
                "這些內容共同構成候選證據集合，系統不預先指定唯一核心或關鍵知識點。"
                "請綜合題目、學生答案與批改回饋，自行判斷最能解釋學生知識缺口的一個或多個概念；"
                "不得僅因某 Seed 排名第一就把它當成唯一缺口。只可從已注入的概念名稱中選擇，"
                "並在回應第一段明確輸出「本輪判斷的知識缺口：概念A、概念B」；若證據不足則輸出"
                "「本輪判斷的知識缺口：待確認」。不必逐一教授全部候選或先備，也不得建立後續概念切換"
                "或多輪先備學習計畫。**"
                if normalized_graph_mode == "prerequisite_d1"
                else
                "本輪只包含 Top-K 題目相關候選概念 Chunk；系統不指定唯一核心概念。請由已注入候選中自行判斷知識缺口，先備與後續概念均未注入。**"
            )
        )

        usage = {
            "used": True,
            "reason": "used",
            "backend": "graphrag",
            "top_k": selected_top_k,
            "seed_count": included_seed_count,
            "expanded_count": included_expanded_count,
            "snippet_count": snippet_count,
            "prereq_material_count": prereq_material_line_count,
            "prereq_chunk_count": prereq_material_chunk_count,
            "prereq_chain_count": prereq_line_count,
            "prereq_node_count": prereq_node_count,
            "descendant_chain_count": descendant_line_count,
            "descendant_node_count": descendant_node_count,
            "total_items": (
                included_seed_count
                + included_expanded_count
                + snippet_count
                + prereq_material_line_count
                + prereq_line_count
                + descendant_line_count
            ),
            "seed_concepts": unique_seeds[:included_seed_count],
            "candidate_concepts": unique_seeds[:included_seed_count],
            "candidate_prerequisite_concepts": prereq_material_concepts,
            "concept_selection_policy": "llm_diagnosis_over_all_candidates",
            "primary_concept_selected": False,
            "retrieved_seed_count": len(unique_seeds),
            "retrieved_expanded_count": len(concept_lines),
            "retrieved_snippet_count": len(snippet_items),
            "retrieved_prereq_material_count": len({
                str(record.get("concept") or "")
                for record in prereq_material_items
                if record.get("concept")
            }),
            "retrieved_prereq_chunk_count": retrieved_prereq_chunk_count,
            "retrieved_prereq_chain_count": len(prereq_chains),
            "retrieved_prereq_node_count": retrieved_prereq_node_count,
            "retrieved_descendant_chain_count": len(descendant_chains),
            "retrieved_descendant_node_count": retrieved_descendant_node_count,
            "retrieved_chunk_count": len(chunk_audit_records),
            "unique_retrieved_chunk_count": len({
                record["content_sha256"] for record in chunk_audit_records
            }),
            "selected_chunk_count": sum(
                1 for record in chunk_audit_records
                if record.get("selected_for_injection")
            ),
            "rejected_chunk_count": sum(
                1 for record in chunk_audit_records
                if not record.get("selected_for_injection")
            ),
            "duplicate_chunk_count": int(rerank_summary.get("duplicate_count", 0) or 0),
            "noise_filtered_chunk_count": int(
                rerank_summary.get("noise_filtered_count", 0) or 0
            ),
            "injected_chunk_count": sum(
                1 for record in chunk_audit_records if not record["omitted"]
            ),
            "complete_injected_chunk_count": sum(
                1 for record in chunk_audit_records if record["is_complete"]
            ),
            "shortened_chunk_count": sum(
                1
                for record in chunk_audit_records
                if not record["omitted"] and not record["is_complete"]
            ),
            "omitted_chunk_count": sum(
                1 for record in chunk_audit_records if record["omitted"]
            ),
            "budget_omitted_chunk_count": sum(
                1
                for record in chunk_audit_records
                if record.get("selected_for_injection")
                and record.get("omitted_reason") == "context_budget"
            ),
            "chunk_rerank": rerank_summary,
            "prereq_chunk_depth": GRAPHRAG_PREREQ_CHUNK_DEPTH,
            "prerequisite_name_depth": safe_prerequisite_name_depth,
            "descendant_context_unlocked": descendant_context_unlocked,
            "prereq_chunks_per_concept": GRAPHRAG_PREREQ_CHUNKS_PER_CONCEPT,
            "prereq_chunk_concepts": prereq_material_concepts,
            "context_char_budget": GRAPHRAG_CONTEXT_CHAR_BUDGET,
            "context_chars": context_chars,
            "context_utf8_bytes": len("".join(ctx_parts).encode("utf-8")),
            "context_estimated_tokens": max(0, round(context_chars / 4)),
            "prompt_before_chars": len(prompt),
            "prompt_after_chars": len(prompt) + context_chars,
            "context_truncated": context_truncated,
            "graph_context_mode": normalized_graph_mode,
            "selection_policy": (
                "seed_order_then_direct_prerequisite_distance_1"
                if normalized_graph_mode == "prerequisite_d1"
                else "initial_seed_order"
            ),
            "seed_chunk_metadata_total": seed_chunk_metadata_total,
            "seed_chunk_metadata_resolved": seed_chunk_metadata_resolved,
            "seed_chunk_metadata_unresolved": seed_chunk_metadata_unresolved,
            "retrieved_prerequisite_concepts": list(dict.fromkeys(
                retrieved_prerequisite_concepts
            )),
            "injected_prerequisite_concepts": prereq_material_concepts,
            "prerequisite_decisions": prerequisite_decisions,
        }
        _set_last_graphrag_usage(usage)

        enhanced_prompt = prompt + "".join(ctx_parts)
        prompt_search_cursor = 0
        for record in chunk_audit_records:
            injected_text = str(record.get("injected_content") or "")
            has_source = bool(record.get("source") or record.get("source_ids"))
            has_locator = bool(
                record.get("chunk_seq_id") is not None
                or record.get("page_start") is not None
                or record.get("char_start") is not None
            )
            record["source_traceable"] = bool(
                has_source and record.get("chunk_id") and record.get("content_sha256")
            )
            record["precise_source_locator"] = bool(
                record.get("source") and has_locator
            )
            if not injected_text:
                record["prompt_span"] = None
                continue
            start = enhanced_prompt.find(injected_text, prompt_search_cursor)
            if start < 0:
                start = enhanced_prompt.find(injected_text)
            if start < 0:
                record["prompt_span"] = None
                continue
            end = start + len(injected_text)
            record["prompt_span"] = {"char_start": start, "char_end": end}
            prompt_search_cursor = end
        # ✨ 論文對比用：把 GraphRAG 塞進去的結構化 context 單獨拉出來
        injected_context_text = "".join(ctx_parts)
        try:
            from src.graphrag_trace import record_query
            record_query(
                question=question,
                top_k=selected_top_k,
                api_base=GRAPHRAG_API_BASE,
                result=result,
                usage=usage,
                latency_ms=latency_ms,
                prompt_before_chars=len(prompt),
                prompt_after_chars=len(enhanced_prompt),
                # ✨ 完整 prompt 三段（before / injected / after）
                # 用來跟 ChromaDB trace 對比資料完整度
                prompt_before_text=prompt,
                prompt_after_text=enhanced_prompt,
                injected_context_text=injected_context_text,
                chunk_records=chunk_audit_records,
            )
        except Exception:
            pass

        return enhanced_prompt

    except Exception as e:
        logger.error(f"❌ GraphRAG 增強失敗: {e}", exc_info=True)
        usage = _empty_graphrag_usage("error")
        _set_last_graphrag_usage(usage)
        try:
            from src.graphrag_trace import record_error
            record_error("graphrag_enhance_error", e)
        except Exception:
            pass
        return prompt

def search_knowledge(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """依 RAG_BACKEND 分派：GraphRAG（預設）或 ChromaDB。

    向後相容：兩個 backend 都回傳 [{content, metadata, distance}, ...] 格式。
    """
    # === 選 backend ===
    try:
        from src.rag_backend import get_active_backend
        backend = get_active_backend()
    except Exception:
        backend = "graphrag"

    # 🆕 llm_only baseline：不做任何 RAG 檢索
    if backend == "llm_only":
        return []

    if backend == "chromadb":
        try:
            from src.chromadb_rag import search_knowledge_chromadb
            return search_knowledge_chromadb(query, top_k=top_k)
        except Exception as e:
            logger.error(f"❌ ChromaDB backend 失敗: {e}")
            return []

    # === 以下為 GraphRAG 流程（預設）===
    try:
        from src.graphrag_client import query_natural_language

        result = query_natural_language(query, top_k=top_k)
        knowledge_items: List[Dict[str, Any]] = []

        # 1) seed concepts 的教材片段
        for item in (result.get("expanded") or [])[:top_k]:
            for snippet in (item.get("sample_chunks") or [])[:2]:
                if snippet:
                    knowledge_items.append({
                        "content": snippet,
                        "metadata": {
                            "concept": item.get("name"),
                            "category": "graphrag",
                            "prerequisites": item.get("prerequisites") or [],
                            "leads_to": item.get("leads_to") or [],
                            "book_ids": item.get("book_ids") or [],
                        },
                        "distance": 0.0,   # GraphRAG 沒有單一距離分數，給 0
                    })

        return knowledge_items[:top_k]

    except Exception as e:
        logger.error(f"❌ 知識檢索失敗: {e}")
        return []

# 註：translate_to_english 已於 GraphRAG 改造時移除
# 原因：GraphRAG 使用 Gemini Embedding，中文直接檢索，無需中翻英

# ==================== 輔助功能 ====================

def get_or_create_session(user_email: str, question: str) -> dict:
    """獲取或創建學習會話"""
    from src.remedial_learning_state import stable_session_key
    clean_question = question.strip().replace('\n', ' ').replace('\r', ' ')
    session_key = stable_session_key(user_email, question)

    # Migrate sessions created by the old process-randomized Python hash key.
    # The stored question/email are stable even when the old dictionary key is not.
    if session_key not in learning_sessions:
        for old_key, old_session in list(learning_sessions.items()):
            old_question = str(old_session.get('question') or '').strip().replace('\n', ' ').replace('\r', ' ')
            if old_session.get('user_email') == user_email and old_question == clean_question:
                learning_sessions[session_key] = old_session
                if old_key != session_key:
                    del learning_sessions[old_key]
                break

    # 顯示當前用戶的所有會話
    user_sessions = [key for key in learning_sessions.keys() if key.startswith(f"{user_email}_")]

    # 顯示會話統計信息
    if learning_sessions:
        # 統計不同用戶的會話數量
        user_counts = {}
        for key in learning_sessions.keys():
            if '_question_' in key:
                user_part = key.split('_question_')[0]
                user_counts[user_part] = user_counts.get(user_part, 0) + 1



    # 檢查是否已存在會話
    if session_key in learning_sessions:
        existing_session = learning_sessions[session_key]
        _ensure_learning_path_fields(existing_session)
        return existing_session

    # 如果沒有找到會話，創建新會話
    learning_sessions[session_key] = {
        'user_email': user_email,
        'question': question,
        'conversation_history': [],
        'understanding_level': 0,
        'learning_stage': 'core_concept_confirmation',
        'concept_progress': [],
        'current_target': None,
        'current_target_started_user_count': 0,
        'last_seed_concepts': [],
        'candidate_concepts': [],
        'candidate_prerequisite_concepts': [],
        'available_graphrag_concepts': [],
        'llm_selected_focus_concepts': [],
        'concept_selection_policy': 'llm_diagnosis_over_all_candidates',
        'mastered_concepts': [],
        'last_completed_concept': None,
        'recommended_next_concept': None,
        'next_concept_candidates': [],
        'next_concept_recommendation': None,
        'target_transition_pending': False,
        'created_at': datetime.now().isoformat()
    }
    _ensure_learning_path_fields(learning_sessions[session_key])

    # 立即保存到文件
    #save_sessions_to_file()


    return learning_sessions[session_key]

def build_initial_prompt(question: str, user_answer: str, correct_answer: str, grading_feedback: dict = None) -> str:
    """構建初始化提示詞"""

    # 如果有AI批改的評分反饋，加入提示詞中
    feedback_section = ""
    if grading_feedback:
        feedback_section = f"""

**AI批改評分反饋（請參考使用）：**
- 優點：{grading_feedback.get('strengths', '無')}
- 需要改進：{grading_feedback.get('weaknesses', '無')}
- 學習建議：{grading_feedback.get('suggestions', '無')}
- 評分說明：{grading_feedback.get('explanation', '無')}
"""

    return f"""{TEACHER_STYLE}

**題目：** {question}
**學生答案：** {user_answer}
**正確答案：** {correct_answer}{feedback_section}

請分析學生的答案，找出需要改進的地方，並提出一個具體的引導問題來開始教學。

**重要：** 初始化階段不給分數，只提出引導問題。

**回應要求：**
- 語氣親切自然，如同真正的老師
- 系統附加的 GraphRAG Seed 與先備概念全部都是候選證據，不預設唯一核心知識點
- 綜合學生答案、批改回饋與所有候選 Chunk，自行判斷一個或多個最需要補救的概念
- 回應第一段必須使用已注入的正式概念名稱，輸出「本輪判斷的知識缺口：概念A、概念B」；若無法判斷則輸出「本輪判斷的知識缺口：待確認」
- 不得僅因候選概念排名第一，就把它視為唯一知識缺口
- 分析學生答案的優缺點（可參考AI批改反饋）
- 提出具體的引導問題
- 不要給出評分（初始化階段）
- 絕對不要包含「評分：」字樣

請現在生成開場白："""

def build_followup_prompt(question: str, user_answer: str, correct_answer: str, user_input: str, conversation_history: list, grading_feedback: dict = None, session: Optional[dict] = None) -> str:
    """Build a later tutoring prompt without changing GraphRAG evidence.

    Understanding scores are retained as progress records and may alter the
    *style* of the next Socratic question.  They must not select a concept,
    trigger a new graph search, unlock a prerequisite queue, or replace the
    candidate evidence injected on the first turn.
    """
    # 獲取當前學習階段指導：優先使用 session 的真實狀態，舊資料才退回對話長度推斷
    current_stage = (session or {}).get('learning_stage') or 'core_concept_confirmation'

    if not session and conversation_history:
        # 根據對話長度判斷階段
        if len(conversation_history) >= 6:  # 3輪對話
            current_stage = 'related_concept_guidance'
        elif len(conversation_history) >= 4:  # 2輪對話
            current_stage = 'core_concept_confirmation'
        else:
            current_stage = 'core_concept_confirmation'

    stage_guidance = get_stage_guidance(current_stage)
    # GraphRAG candidate evidence is appended later by
    # ``handle_tutoring_conversation``.  It is deliberately absent here: the
    # same cached set is injected on every turn, and no target, queue, or
    # score-triggered graph state is presented to the LLM.
    candidate_evidence_policy = """

**GraphRAG 候選證據使用規則：**
- 本次對話會附上首次檢索後快取的完整候選教材證據；後續回合沿用相同內容。
- 候選 Seed 與距離 1 先備概念地位相同，系統未指定「目前目標」或固定教學順序。
- 請根據學生本輪回答自行引用一個或多個能解釋其困難的概念與教材；不需要逐一講解全部候選。
- 不得聲稱系統已切換到某概念、已解鎖先備佇列，或要求學生先完成特定概念才能繼續。
"""

    # 如果有AI批改的評分反饋，加入提示詞中
    feedback_section = ""
    if grading_feedback:
        feedback_section = f"""

**AI批改評分反饋（請參考使用）：**
- 優點：{grading_feedback.get('strengths', '無')}
- 需要改進：{grading_feedback.get('weaknesses', '無')}
- 學習建議：{grading_feedback.get('suggestions', '無')}
- 評分說明：{grading_feedback.get('explanation', '無')}
"""

    return f"""{TEACHER_STYLE}

**題目：** {question}
**正確答案：** {correct_answer}
**學生最新回答：** {user_input}{feedback_section}

**對話歷史：**
{format_conversation_history(conversation_history)}

**目前理解程度的對話指引：**
{stage_guidance}{candidate_evidence_policy}

請基於學生的回答進行教學指導，並按照以下步驟進行：

**教學步驟：**
1. **評估學生回答**：分析學生回答的質量
2. **給出正確答案**：如果學生回答錯誤，直接給出正確答案
3. **提出下一個問題**：基於當前進度，提出相關的延伸問題
4. **給出評分**：根據學生回答質量給予適當分數

**重要要求：**
- GraphRAG 提供的是候選概念集合，不存在由系統預先指定的唯一核心知識點
- 綜合學生本輪回答與既有對話，自行判斷目前最需要補救的一個或多個概念
- 回應第一段必須輸出「本輪判斷的知識缺口：概念A、概念B」；概念名稱應來自最初注入的 GraphRAG 候選集合，無法判斷時寫「待確認」
- 不要重複問學生「你知道嗎？」或「你覺得呢？」
- 如果學生回答錯誤，直接給出正確答案
- 避免陷入循環提問
- 每次都要給出評分

**評分邏輯：**
1. 第一個問題：根據學生回答質量，給予0-95分的基礎評分
2. 後續問題：基於當前分數，給予適當加分（1-10分）
3. 達到90分以上時：表示學生已熟悉目前概念，可以進階學習新概念
4. 不需要強求99或100分；90分以上即可作為概念熟悉門檻

**⚠️ 強制評分要求（必須遵守）：**
- **必須**在回應的最後一行給出評分
- **必須**使用格式：「評分：[分數]分」（例如：評分：85分）
- 評分範圍：0-100分
- 根據學生回答的質量給予適當分數
- **如果沒有評分，系統將無法正常工作！**
- **即使學生回答正確或表現優秀，也必須給出評分！**

**評分邏輯指南：**
- 如果學生回答正確或理解正確：給予高分（70-95分）；若已能清楚說明目前概念，可給90分以上
- 如果學生回答部分正確：給予中等分數（40-69分）
- 如果學生回答錯誤但顯示思考：給予基礎分數（20-39分）
- 如果學生完全理解錯誤：給予低分（0-19分）

**評分格式示例（必須照此格式）：**
同學，你的分析非常詳細！你正確指出了這個操作在特定情況下會為0。

評分：90分

**最後再次強調：**
- 回應的最後一行**必須**是「評分：[數字]分」
- 不要使用其他格式，如「得分：XX」或「分數：XX」
- 必須使用中文冒號「：」和「分」字
- 這是系統運作的必要條件，**絕對不能省略！**

請現在分析學生的回答並提供教學指導："""

def format_conversation_history(conversation_history: list) -> str:
    """格式化對話歷史"""
    if not conversation_history:
        return "無"

    formatted = ""
    for i, msg in enumerate(conversation_history[-4:], 1):  # 只顯示最近4條
        role = "學生" if msg['role'] == 'user' else "AI導師"
        formatted += f"{i}. {role}: {msg['content'][:100]}...\n"

    return formatted

def determine_learning_stage(understanding_level: int) -> str:
    """根據理解程度確定學習階段 - 優化版本"""
    if understanding_level >= LEARNING_COMPLETION_MARKER_SCORE:
        return 'completed'                       # 完成標記（預設99分，通常由90分門檻轉換而來）
    elif understanding_level >= LEARNING_CONCEPT_READY_THRESHOLD:
        return 'understanding_verification'      # 概念熟悉門檻（預設90分）
    elif understanding_level >= 70:
        return 'application_understanding'       # 應用理解（70-89分）
    elif understanding_level >= 40:
        return 'related_concept_guidance'        # 相關概念引導（40-69分）
    else:
        return 'core_concept_confirmation'       # 知識缺口診斷（0-39分，保留舊 key 相容）

def get_stage_guidance(stage: str) -> str:
    """根據學習階段提供指導"""
    stage_guidance = {
        'core_concept_confirmation': f"""
您目前處於知識缺口診斷階段。請：
- 綜合全部 GraphRAG 候選概念、先備資訊、學生回答與批改回饋
- 自行判斷目前最需要補救的一個或多個概念，不得預設第一名為唯一核心
- 只處理與學生錯誤有直接證據關聯的內容
- 若證據不足，先提出能區分不同知識缺口的具體問題
""",
        'related_concept_guidance': f"""
您目前處於相關概念引導階段。請：
- 圍繞本輪由證據判斷出的知識缺口，說明它與原題及其他候選概念的關係
- 確保每個問題都能幫助學生修正原題中的錯誤
- 可以使用具體例子幫助學生理解抽象概念
- 觀察學生的回答與反饋，適時調整問題難度
""",
        'application_understanding': f"""
您目前處於應用理解階段。請：
- 讓學生將理解應用到題目情境中
- 提供與題目相關的練習問題或案例
- 觀察學生是否能正確應用概念到題目
- 如果學生應用正確，可以進入理解驗證階段
""",
        'understanding_verification': f"""
您目前處於理解驗證階段。請：
- 要求學生用自己的話說明目前概念，以及它和原題的關係
- 評估學生是否真正理解目前概念
- 如果學生能清楚說明並達到90分以上，表示目前概念已熟悉，可以進階學習新概念
- 如果學生解釋不清楚，直接補上正確說明並針對盲點再問一題
""",
        'completed': f"""
學生已能整合原題所需概念，請完成總結並回到原題驗證答案。
"""
    }

    return stage_guidance.get(stage, stage_guidance['core_concept_confirmation'])

def get_stage_display_name(stage: str) -> str:
    """獲取學習階段的中文顯示名稱"""
    stage_names = {
        'core_concept_confirmation': '知識缺口診斷',
        'related_concept_guidance': '相關概念引導',
        'application_understanding': '應用理解',
        'understanding_verification': '理解驗證',
        'completed': '學習完成',
        'unknown': '未知階段'
    }
    return stage_names.get(stage, stage)

def record_progress(session: dict, score: int, smart_score: int, stage: str):
    """記錄學習進度"""
    if 'concept_progress' not in session:
        session['concept_progress'] = []

    session['concept_progress'].append({
        'stage': stage,
        'understanding_level': smart_score,
        'score': score,
        'timestamp': datetime.now().isoformat()
    })

def extract_score_from_response(ai_response: str) -> int:
    """從AI回應中提取評分"""
    try:
        # 尋找評分格式：評分：[分數]分（支援多種格式）
        score_patterns = [
            r'評分[：:]\s*(\d+)\s*分',  # 評分：85分 或 評分: 85分
            r'評分[：:]\s*(\d+)',        # 評分：85
            r'評分[為是]\s*(\d+)\s*分',  # 評分為85分 或 評分是85分
            r'得分[：:]\s*(\d+)\s*分',  # 得分：85分
            r'分數[：:]\s*(\d+)\s*分',  # 分數：85分
            r'(\d+)\s*分\s*$',           # 最後一行的「85分」
            r'分數[：:]\s*(\d+)分',
            r'分數[：:]\s*(\d+)',
            r'(\d+)分',
            r'評分[：:]\s*(\d+)',
            r'理解程度[：:]\s*(\d+)',
            r'評分[：:]\s*(\d+)\s*分',
            r'評分[：:]\s*(\d+)\s*',
            r'(\d+)\s*分',
            r'評分[：:]\s*(\d+)',
            r'分數[：:]\s*(\d+)'
        ]

        # 優先檢查回應的最後幾行（評分通常在最後）
        lines = ai_response.strip().split('\n')
        last_lines = '\n'.join(lines[-5:]) if len(lines) > 5 else ai_response  # 檢查最後5行

        # 如果找到評分，返回分數
        for pattern in score_patterns:
            # 先檢查最後幾行（更準確）
            match = re.search(pattern, last_lines, re.IGNORECASE | re.MULTILINE)
            if not match:
                # 如果最後幾行沒找到，檢查全文
                match = re.search(pattern, ai_response, re.IGNORECASE | re.MULTILINE)

            if match:
                score = int(match.group(1))
                # 確保分數在合理範圍內
                if 0 <= score <= 100:
                    logger.info(f"✅ 成功提取評分：{score}分（模式匹配）")
                    return score
                else:
                    logger.warning(f"⚠️ 提取到異常評分：{score}，超出0-100範圍")
                    continue  # 繼續嘗試其他模式

        # 如果所有模式都沒匹配到，記錄詳細警告
        logger.warning(f"⚠️ 未能從AI回應中提取評分")
        logger.warning(f"   回應長度：{len(ai_response)}字符")
        logger.warning(f"   最後200字符：{ai_response[-200:]}")

        # 作為備用方案，嘗試從最後幾行提取數字
        for line in reversed(lines[-3:] if len(lines) >= 3 else lines):
            if '評分' in line or '分數' in line or '得分' in line:
                # 嘗試提取數字
                numbers_in_line = re.findall(r'\d+', line)
                if numbers_in_line:
                    score = int(numbers_in_line[0])
                    if 0 <= score <= 100:
                        logger.info(f"✅ 備用方案提取評分：{score}分（從行：{line[:50]}）")
                        return score

        logger.error(f"❌ 完全無法提取評分，回應內容：{ai_response[-300:]}")
        return None

    except Exception as e:
        logger.error(f"❌ 評分提取失敗: {e}")
        return None

def clean_ai_response(ai_response: str) -> str:
    """清理AI回應，移除評分等內部信息"""
    try:
        # 移除評分格式
        cleaned = re.sub(r'評分[：:]\s*\d+分', '', ai_response)
        # 清理多餘空行
        cleaned = re.sub(r'\n\s*\n', '\n\n', cleaned)
        cleaned = cleaned.strip()

        if not cleaned:
            return "同學，我已經分析了您的回答。讓我們繼續學習吧！"

        return cleaned

    except Exception as e:
        logger.error(f"❌ 回應清理失敗: {e}")
        return ai_response

# ==================== 初始化函數 ====================

# 註：init_vector_database (ChromaDB) 已於 GraphRAG 改造時移除
# 原因：教材檢索改走 graphrag_client，向量直接存在 Neo4j 內


def call_gemini_api(
    prompt: str,
    ai_type: str = 'ollama',
    generation_config_override: Optional[Dict[str, Any]] = None,
) -> str:
    """
    調用 AI API（已改走統一入口 init_ai，受 AI_PROVIDER 環境變數控制）

    Args:
        prompt: 提示詞
        ai_type: 'ollama' (預設) 或 'gemini'
            - 注意：實際 ai_type 會被 AI_PROVIDER 環境變數覆寫，
              並在 Ollama 連不上時自動 fallback 到 Gemini。
        generation_config_override: 僅覆寫 Gemini 生成參數。論文盲測可傳入
            ``{"temperature": 0}``，避免回答隨機性干擾三方比較。
    """
    try:
        # ★ 改走 init_ai 統一入口，讓 AI_PROVIDER 主開關 + Ollama 連線預檢查生效
        model = init_ai(ai_type=ai_type)
        if not model:
            return "抱歉，AI 服務暫時不可用，請稍後再試。"

        # 偵測實際拿到的是哪個 wrapper（決定怎麼呼叫 / 怎麼解析回應）
        # GeminiWrapper 有 sdk_version 屬性；OllamaWrapper 沒有
        is_gemini = hasattr(model, "sdk_version") or "Gemini" in type(model).__name__

        if is_gemini:
            generation_config = {
                'max_output_tokens': 8192,
                'temperature': 0.7,
                'top_p': 0.8,
                'top_k': 40
            }
            if generation_config_override:
                generation_config.update(generation_config_override)
            response = model.generate_content(prompt, generation_config=generation_config)
            # 強制把 ai_type 設成 gemini，讓後面的回應解析走 Gemini 分支
            ai_type = 'gemini'
        else:
            response = model.invoke(prompt)
            ai_type = 'ollama'

        logger.info(f"📥 AI API回應接收（實際後端: {ai_type}），類型: {type(response).__name__}")

        # 檢查回應是否有效
        if not response:
            logger.error(f"❌ {ai_type.upper()} API返回空回應")
            return "抱歉，AI回應格式不正確，請稍後再試。"

        # 處理 Ollama 回應（LangChain AIMessage）
        # 🔧 修 bug：以前這個抽 text 的區塊被 `if ai_type == 'ollama':` 包住，
        # Gemini 分支直接跳過 → 掉到最後 str(response) fallback → 前端看到整個
        # GenerateContentResponse 物件的 dump（sdk_http_response, avg_logprobs...）
        # 現在拿掉 ai_type 判斷，讓 Ollama / Gemini 都走同一組解析邏輯。
        try:
            # 1) Gemini（google-genai / vertex）先嘗試 response.text（大部分情況都會命中）
            if hasattr(response, 'text'):
                text_val = response.text
                if callable(text_val):
                    text_val = text_val()
                if text_val and str(text_val).strip():
                    logger.info(f"✅ 從response.text獲取回應，長度: {len(str(text_val))} 字符")
                    return str(text_val).strip()
            # 2) Gemini candidates[0].content.parts fallback（safety filter 導致 .text 出錯時用）
            if hasattr(response, 'candidates') and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, 'content'):
                    content = candidate.content
                    if hasattr(content, 'parts'):
                        text_parts = []
                        for part in content.parts:
                            if hasattr(part, 'text') and part.text:
                                text_parts.append(part.text)
                        if text_parts:
                            full_text = ''.join(text_parts).strip()
                            if full_text:
                                logger.info(f"✅ 從candidates.content.parts獲取回應，長度: {len(full_text)} 字符")
                                return full_text
            if hasattr(response, 'content'):
                text = response.content
                if isinstance(text, str) and text.strip():
                    logger.info(f"✅ 從response.content獲取回應，長度: {len(text)} 字符")
                    return text.strip()
        except Exception as e:
            logger.debug(f"抽 response 文字時失敗: {e}")

        logger.error(f"❌ 無法從回應中提取文字，response 類型 = {type(response).__name__}")
        return "抱歉，無法存取AI回應，請稍後再試。"

    except Exception as e:
        logger.error(f"❌ {ai_type.upper()} API調用失敗: {e}", exc_info=True)
        return "抱歉，AI回應生成失敗，請稍後再試。"
