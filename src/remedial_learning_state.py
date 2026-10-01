"""Persistent, stateful GraphRAG remedial-learning planner.

The initial question is searched globally once.  Later turns reuse that frozen
top-five concept snapshot and only fetch exact material for the active core or
prerequisite concept.  A low-scoring answer expands direct prerequisites for
the active core, ranks them against the student's explicit knowledge gap, and
stores the resulting teaching queue and full textbook chunks in the session.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.graphrag_chunk_reranker import (
    rerank_chunk_candidates,
    score_texts_against_query,
)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


INITIAL_SEED_LIMIT = _env_int("REMEDIAL_INITIAL_SEED_LIMIT", 5, 1, 10)
# This value uses the normalised ``smart_score`` rather than the raw model
# score. At 0--39 the learner is still confirming the core concept, so only
# then may the planner switch to a directly connected prerequisite.
EXPAND_SCORE_THRESHOLD = _env_int("REMEDIAL_EXPAND_SCORE_THRESHOLD", 40, 0, 100)
# A prerequisite is not injected after one weak answer.  The learner must stay
# in the core-confirmation band for this many consecutive scored tutoring turns.
# This guards against a temporary misunderstanding moving the lesson away from
# the original question too early.
PREREQUISITE_TRIGGER_CONSECUTIVE_LOW_SCORES = _env_int(
    "REMEDIAL_PREREQUISITE_TRIGGER_CONSECUTIVE_LOW_SCORES", 5, 1, 20
)
MASTERY_SCORE_THRESHOLD = _env_int("REMEDIAL_MASTERY_SCORE_THRESHOLD", 90, 1, 100)
MAX_PREREQUISITE_CANDIDATES = _env_int(
    "REMEDIAL_MAX_PREREQUISITE_CANDIDATES", 8, 1, 30
)
MAX_ACTIVE_PREREQUISITES = _env_int(
    "REMEDIAL_MAX_ACTIVE_PREREQUISITES", 1, 1, 12
)
CONTEXT_CHUNKS_PER_CONCEPT = _env_int(
    "REMEDIAL_CONTEXT_CHUNKS_PER_CONCEPT", 2, 1, 5
)
# Anti-drift guardrails: prerequisite teaching may only use a direct parent
# whose meaning matches the observed knowledge gap and whose exact textbook
# chunks provide enough evidence.  A failed guard means "stay on the core";
# it is never overridden merely to fill the prerequisite queue.
MAX_GRAPH_DEPTH = _env_int("REMEDIAL_MAX_GRAPH_DEPTH", 1, 1, 1)
MIN_GAP_SEMANTIC = _env_float("REMEDIAL_MIN_GAP_SEMANTIC", 0.25, 0.0, 1.0)
MIN_CHUNK_EVIDENCE = _env_float(
    "REMEDIAL_MIN_CHUNK_EVIDENCE", 0.25, 0.0, 1.0
)


def _now() -> str:
    return datetime.now().isoformat()


def _begin_target_attempt(session: Dict[str, Any]) -> None:
    """Mark the next learner reply as the first attempt for the active target."""
    session["current_target_started_user_count"] = sum(
        1
        for message in (session.get("conversation_history") or [])
        if isinstance(message, dict) and message.get("role") == "user"
    )


def concept_key(name: Any) -> str:
    return "".join(ch for ch in str(name or "").casefold() if ch.isalnum())


def stable_session_key(user_email: str, question: str) -> str:
    clean_question = str(question or "").strip().replace("\n", " ").replace("\r", " ")
    digest = hashlib.sha256(clean_question.encode("utf-8")).hexdigest()[:20]
    return f"{user_email}_question_{digest}"


def ensure_state(session: Dict[str, Any]) -> Dict[str, Any]:
    session.setdefault("initial_retrieval", {})
    session.setdefault("concept_states", {})
    session.setdefault("active_core_concept", None)
    session.setdefault("active_teaching_concept", None)
    session.setdefault("teaching_mode", "initial_retrieval")
    session.setdefault("prerequisite_queue", [])
    session.setdefault("prerequisite_candidates", [])
    session.setdefault("prerequisite_rejections", [])
    session.setdefault("prerequisite_support", {})
    session.setdefault("concept_context_cache", {})
    session.setdefault("last_knowledge_gap", None)
    session.setdefault("core_low_score_streak", 0)
    session.setdefault("remedial_events", [])
    return session


def _seed_objects(seed_names: Iterable[str]) -> List[Dict[str, Any]]:
    result = []
    seen = set()
    for name in seed_names:
        normalized = str(name or "").strip()
        key = concept_key(normalized)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append({
            "name": normalized,
            "seed_rank": len(result) + 1,
            "mastery": "undetermined",
            "last_score": None,
            "evidence": None,
        })
        if len(result) >= INITIAL_SEED_LIMIT:
            break
    return result


def freeze_initial_retrieval(
    session: Dict[str, Any],
    question: str,
    usage: Optional[Dict[str, Any]],
) -> bool:
    """Persist the first global GraphRAG top-five snapshot exactly once."""
    ensure_state(session)
    existing = session.get("initial_retrieval") or {}
    if existing.get("seed_concepts"):
        return False
    seeds = _seed_objects((usage or {}).get("seed_concepts") or [])
    if not seeds:
        return False
    snapshot = {
        "question": str(question or ""),
        "created_at": _now(),
        "retrieval_mode": "initial_global_query",
        "seed_concepts": seeds,
        "usage": {
            "backend": (usage or {}).get("backend"),
            "top_k": (usage or {}).get("top_k"),
            "retrieved_chunk_count": (usage or {}).get("retrieved_chunk_count", 0),
            "selected_chunk_count": (usage or {}).get("selected_chunk_count", 0),
        },
    }
    session["initial_retrieval"] = snapshot
    first = seeds[0]["name"]
    session["active_core_concept"] = first
    session["active_teaching_concept"] = first
    session["current_target"] = first
    session["teaching_mode"] = "diagnose_core"
    session["core_low_score_streak"] = 0
    for seed in seeds:
        session["concept_states"].setdefault(seed["name"], {
            "concept": seed["name"],
            "role": "core",
            "seed_rank": seed["seed_rank"],
            "mastery": "undetermined",
            "last_score": None,
            "evidence": None,
            "updated_at": _now(),
        })
    session["remedial_events"].append({
        "event": "initial_retrieval_frozen",
        "seed_concepts": [seed["name"] for seed in seeds],
        "timestamp": _now(),
    })
    return True


def _seed_rank(session: Dict[str, Any], concept_name: str) -> int:
    for item in (session.get("initial_retrieval") or {}).get("seed_concepts") or []:
        if concept_key(item.get("name")) == concept_key(concept_name):
            return int(item.get("seed_rank") or INITIAL_SEED_LIMIT + 1)
    return INITIAL_SEED_LIMIT + 1


def _make_candidate_record(
    concept_name: str,
    definition: str,
    value: Any,
    ordinal: int,
    *,
    section: str,
) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        data = value
        text = str(data.get("text") or "")
    else:
        data = {}
        text = str(value or "")
    if not text.strip():
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    book_id = data.get("book_id") or data.get("source_ids")
    source_ids = list(book_id) if isinstance(book_id, (list, tuple, set)) else ([str(book_id)] if book_id else [])
    return {
        "section": section,
        "concept": concept_name,
        "concepts": [concept_name],
        "concept_definition": definition,
        "ordinal": ordinal,
        "chunk_id": str(data.get("chunk_id") or digest),
        "content_sha256": digest,
        "source": str(data.get("source") or data.get("book_id") or ""),
        "source_ids": source_ids,
        "chunk_seq_id": data.get("chunk_seq_id"),
        "chapter_id": str(data.get("chapter_id") or ""),
        "page_start": data.get("page_start"),
        "page_end": data.get("page_end"),
        "char_start": data.get("char_start"),
        "char_end": data.get("char_end"),
        "upstream_text_hash": str(data.get("text_hash") or ""),
        "raw_content": text,
        "raw_chars": len(text),
        "injected_content": "",
        "injected_chars": 0,
        "is_complete": False,
        "omitted": True,
        "omitted_reason": "not_selected",
    }


def get_or_fetch_concept_context(
    session: Dict[str, Any],
    concept_name: str,
    *,
    role: str,
    parent_of: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Return a cached exact-concept profile and complete selected chunks."""
    ensure_state(session)
    key = "::".join((
        concept_key(concept_name),
        role,
        concept_key(parent_of),
    ))
    cache = session["concept_context_cache"]
    if key in cache:
        cache[key]["last_accessed_at"] = _now()
        return cache[key], True

    from src.graphrag_client import fetch_concept_profile

    profile = fetch_concept_profile(
        concept_name,
        max_snippets=max(CONTEXT_CHUNKS_PER_CONCEPT * 3, 3),
        include_leads_to=False,
        include_context_relations=True,
    )
    resolved_name = str(profile.get("name") or concept_name)
    definition = str(profile.get("definition") or "").strip()
    raw_values = profile.get("sample_chunk_records") or [
        {"text": text} for text in (profile.get("sample_chunks") or [])
    ]
    section = "prerequisite" if role == "prerequisite" else "core"
    records = []
    for ordinal, value in enumerate(raw_values, start=1):
        record = _make_candidate_record(
            resolved_name,
            definition,
            value,
            ordinal,
            section=section,
        )
        if record:
            if role == "prerequisite":
                record["prerequisite_targets"] = [parent_of] if parent_of else []
                record["prerequisite_target_ranks"] = (
                    {parent_of: _seed_rank(session, parent_of)} if parent_of else {}
                )
                record["source_seed_rank"] = _seed_rank(session, parent_of or "")
                record["prerequisite_distance"] = 1
            records.append(record)

    query = ". ".join(value for value in (resolved_name, definition) if value)
    reranked = rerank_chunk_candidates(
        query or resolved_name,
        records,
        max_chunks=CONTEXT_CHUNKS_PER_CONCEPT,
        max_prereq_chunks=CONTEXT_CHUNKS_PER_CONCEPT,
    ) if records else {"records": [], "selected": [], "summary": {"selected_count": 0}}

    entry = {
        "concept": resolved_name,
        "role": role,
        "parent_of": parent_of,
        "definition": definition,
        "prerequisites": list(profile.get("prerequisites") or []),
        "parents": list(profile.get("parents") or []),
        "selected_chunks": reranked.get("selected") or [],
        "chunk_records": reranked.get("records") or [],
        "rerank_summary": reranked.get("summary") or {},
        "fetched_at": _now(),
        "last_accessed_at": _now(),
    }
    cache[key] = entry
    return entry, False


def _mastery_deficit(session: Dict[str, Any], concept_name: str) -> float:
    state = (session.get("concept_states") or {}).get(concept_name) or {}
    score = state.get("last_score")
    if isinstance(score, (int, float)):
        return max(0.0, min(1.0, 1.0 - float(score) / 100.0))
    mastery = state.get("mastery")
    return {"unknown": 1.0, "uncertain": 0.6, "mastered": 0.0}.get(mastery, 0.6)


def rank_and_queue_prerequisites(
    session: Dict[str, Any],
    core_concept: str,
    knowledge_gap: str,
) -> List[Dict[str, Any]]:
    """Expand direct prerequisites and rank concepts for the next turn."""
    ensure_state(session)
    from src.graphrag_client import get_concept_relations

    mastered_keys = {concept_key(name) for name in session.get("mastered_concepts") or []}
    source_candidates = [core_concept]
    for seed in (session.get("initial_retrieval") or {}).get("seed_concepts") or []:
        seed_name = str(seed.get("name") or "").strip()
        if seed_name and concept_key(seed_name) not in {
            concept_key(name) for name in source_candidates
        }:
            source_candidates.append(seed_name)

    profiles: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
    concept_texts = []
    remediation_source = core_concept
    rejections: List[Dict[str, Any]] = []
    # Prefer the active unknown concept.  If it has no usable direct parent,
    # walk the already-frozen top-five order (never a new global search) until
    # the first concept with reliable prerequisite evidence is found.
    for source_concept in source_candidates:
        relations = get_concept_relations(source_concept)
        direct = []
        for relation in relations.get("prerequisites") or []:
            distance = max(1, int(relation.get("depth") or 1))
            if distance > MAX_GRAPH_DEPTH:
                rejections.append({
                    "name": str(relation.get("name") or relation.get("id") or ""),
                    "parent_of": source_concept,
                    "reason": "graph_depth_exceeded",
                    "graph_depth": distance,
                    "maximum_graph_depth": MAX_GRAPH_DEPTH,
                })
                continue
            direct.append(relation)
        local_profiles: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
        local_texts = []
        for relation in direct:
            name = str(relation.get("name") or relation.get("id") or "").strip()
            if not name:
                continue
            if concept_key(name) in mastered_keys:
                rejections.append({
                    "name": name,
                    "parent_of": source_concept,
                    "reason": "already_mastered",
                    "graph_depth": max(1, int(relation.get("depth") or 1)),
                })
                continue
            context, _ = get_or_fetch_concept_context(
                session,
                name,
                role="prerequisite",
                parent_of=source_concept,
            )
            if not context.get("selected_chunks"):
                rejections.append({
                    "name": name,
                    "parent_of": source_concept,
                    "reason": "no_valid_complete_chunk",
                    "graph_depth": max(1, int(relation.get("depth") or 1)),
                    "selected_chunk_count": 0,
                })
                continue
            local_profiles.append((relation, context, source_concept))
            local_texts.append(
                ". ".join(value for value in (name, context.get("definition")) if value)
            )
        if local_profiles:
            profiles = local_profiles
            concept_texts = local_texts
            remediation_source = source_concept
            break

    gap_scores, semantic_backend, semantic_error = score_texts_against_query(
        knowledge_gap,
        concept_texts,
    ) if concept_texts else ([], "not_run", None)
    deficit = _mastery_deficit(session, core_concept)
    support_map = session["prerequisite_support"]
    candidates = []
    for index, (relation, context, source_concept) in enumerate(profiles):
        name = str(relation.get("name") or relation.get("id") or "")
        key = concept_key(name)
        targets = support_map.setdefault(key, [])
        if source_concept not in targets:
            targets.append(source_concept)
        coverage = min(1.0, len(targets) / 3.0)
        seed_rank = _seed_rank(session, source_concept)
        seed_weight = max(
            0.0,
            min(1.0, (INITIAL_SEED_LIMIT + 1 - seed_rank) / INITIAL_SEED_LIMIT),
        )
        distance = max(1, int(relation.get("depth") or 1))
        strength = max(0.0, min(1.0, float(relation.get("strength") or 0.0)))
        graph_score = 0.5 * (1.0 / distance) + 0.5 * strength
        evidence_scores = [
            float(record.get("relevance_score") or 0.0)
            for record in context.get("selected_chunks") or []
        ]
        evidence_quality = max(evidence_scores, default=0.0)
        gap_match = float(gap_scores[index]) if index < len(gap_scores) else 0.0
        guard_values = {
            "knowledge_gap_semantic": round(gap_match, 6),
            "minimum_gap_semantic": MIN_GAP_SEMANTIC,
            "chunk_evidence_quality": round(evidence_quality, 6),
            "minimum_chunk_evidence": MIN_CHUNK_EVIDENCE,
            "graph_depth": distance,
            "maximum_graph_depth": MAX_GRAPH_DEPTH,
        }
        if gap_match < MIN_GAP_SEMANTIC:
            rejections.append({
                "name": name,
                "parent_of": source_concept,
                "reason": "low_knowledge_gap_semantic",
                **guard_values,
            })
            continue
        if evidence_quality < MIN_CHUNK_EVIDENCE:
            rejections.append({
                "name": name,
                "parent_of": source_concept,
                "reason": "low_chunk_evidence_quality",
                **guard_values,
            })
            continue
        priority = (
            0.35 * gap_match
            + 0.20 * deficit
            + 0.15 * seed_weight
            + 0.10 * graph_score
            + 0.10 * coverage
            + 0.10 * evidence_quality
        )
        chunk_hashes = [
            str(record.get("content_sha256") or "")
            for record in context.get("selected_chunks") or []
            if record.get("content_sha256")
        ]
        candidates.append({
            "name": name,
            "parent_of": source_concept,
            "original_active_core": core_concept,
            "seed_rank": seed_rank,
            "distance": distance,
            "graph_depth": distance,
            "relation_strength": strength,
            "priority_score": round(priority, 6),
            "score_components": {
                "knowledge_gap_semantic": round(gap_match, 6),
                "mastery_deficit": round(deficit, 6),
                "source_seed_weight": round(seed_weight, 6),
                "graph_score": round(graph_score, 6),
                "unknown_target_coverage": round(coverage, 6),
                "chunk_evidence_quality": round(evidence_quality, 6),
                "redundancy_penalty": 0.0,
            },
            "definition": context.get("definition") or "",
            "selected_chunk_count": len(context.get("selected_chunks") or []),
            "selected_chunk_hashes": chunk_hashes,
            "semantic_backend": semantic_backend,
            "semantic_error": semantic_error,
            "drift_guard_passed": True,
            "guard_thresholds": {
                "maximum_graph_depth": MAX_GRAPH_DEPTH,
                "minimum_gap_semantic": MIN_GAP_SEMANTIC,
                "minimum_chunk_evidence": MIN_CHUNK_EVIDENCE,
            },
            "status": "pending",
            "reason": "ranked_from_student_gap",
        })

    candidates.sort(
        key=lambda item: (
            -float(item.get("priority_score") or 0.0),
            int(item.get("seed_rank") or 999),
            int(item.get("distance") or 999),
            item.get("name") or "",
        )
    )
    # Keep the highest-priority concept when two graph nodes point to the same
    # textbook evidence, then lower later duplicates instead of injecting the
    # same material under multiple prerequisite names.
    seen_chunk_hashes = set()
    for item in candidates:
        hashes = set(item.get("selected_chunk_hashes") or [])
        overlap_ratio = (
            len(hashes & seen_chunk_hashes) / len(hashes) if hashes else 0.0
        )
        redundancy_penalty = 0.08 * overlap_ratio
        item["score_components"]["redundancy_penalty"] = round(
            redundancy_penalty, 6
        )
        item["priority_score"] = round(
            max(0.0, float(item["priority_score"]) - redundancy_penalty),
            6,
        )
        seen_chunk_hashes.update(hashes)
    candidates.sort(
        key=lambda item: (
            -float(item.get("priority_score") or 0.0),
            int(item.get("seed_rank") or 999),
            int(item.get("distance") or 999),
            item.get("name") or "",
        )
    )
    candidates = candidates[:MAX_PREREQUISITE_CANDIDATES]
    queue = [dict(item) for item in candidates[:MAX_ACTIVE_PREREQUISITES]]
    session["prerequisite_candidates"] = candidates
    session["prerequisite_rejections"] = rejections
    session["prerequisite_queue"] = queue
    session["last_knowledge_gap"] = {
        "core_concept": core_concept,
        "remediation_source_concept": remediation_source,
        "text": knowledge_gap,
        "guard_thresholds": {
            "maximum_graph_depth": MAX_GRAPH_DEPTH,
            "minimum_gap_semantic": MIN_GAP_SEMANTIC,
            "minimum_chunk_evidence": MIN_CHUNK_EVIDENCE,
        },
        "created_at": _now(),
    }
    if queue:
        queue[0]["status"] = "active"
        session["active_teaching_concept"] = queue[0]["name"]
        session["current_target"] = queue[0]["name"]
        session["teaching_mode"] = "teach_prerequisite"
        session["understanding_level"] = 0
        _begin_target_attempt(session)
    else:
        session["active_teaching_concept"] = core_concept
        session["current_target"] = core_concept
        session["teaching_mode"] = "remediate_core_no_prerequisite"
    session["remedial_events"].append({
        "event": "prerequisites_ranked",
        "core_concept": core_concept,
        "knowledge_gap": knowledge_gap,
        "candidates": [
            {"name": item["name"], "priority_score": item["priority_score"]}
            for item in candidates
        ],
        "rejections": [
            {"name": item.get("name"), "reason": item.get("reason")}
            for item in rejections
        ],
        "timestamp": _now(),
    })
    return candidates


def prime_from_initial_wrong_answer(
    session: Dict[str, Any],
    *,
    question: str,
    user_answer: str,
    correct_answer: str,
    grading_feedback: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Prepare the next turn when the quiz already proves a knowledge gap.

    A mere initial page load is not enough evidence.  Priming occurs only when
    the submitted and reference answers differ and either grading evidence or
    an explicit ``idontknow`` marker is available.
    """
    ensure_state(session)
    if session.get("last_knowledge_gap"):
        return None
    submitted = str(user_answer or "").strip()
    expected = str(correct_answer or "").strip()
    if not submitted or not expected:
        return None
    normalized_submitted = " ".join(submitted.casefold().split())
    normalized_expected = " ".join(expected.casefold().split())
    if normalized_submitted == normalized_expected:
        return None
    feedback_parts = []
    if isinstance(grading_feedback, dict):
        for key in ("weaknesses", "explanation", "suggestions", "analysis"):
            value = grading_feedback.get(key)
            if value:
                feedback_parts.append(str(value))
    explicit_unknown = "idontknow" in normalized_submitted or "不知道" in submitted
    if not feedback_parts and not explicit_unknown:
        return None

    core = session.get("active_core_concept")
    if not core:
        return None
    state = session["concept_states"].setdefault(core, {
        "concept": core,
        "role": "core",
        "seed_rank": _seed_rank(session, core),
    })
    state.update({
        "mastery": "unknown",
        "last_score": 0,
        "evidence": submitted,
        "updated_at": _now(),
    })
    knowledge_gap = (
        f"原始題目：{question}\n"
        f"目前不會的核心知識點：{core}\n"
        f"學生原始錯誤答案：{submitted}\n"
        f"批改證據：{' '.join(feedback_parts) or '學生明確表示不知道'}"
    )
    candidates = rank_and_queue_prerequisites(session, core, knowledge_gap)
    event = {
        "event": "initial_wrong_answer_primed",
        "core_concept": core,
        "candidate_count": len(candidates),
        "active_teaching_concept": session.get("active_teaching_concept"),
        "timestamp": _now(),
    }
    session["remedial_events"].append(event)
    return event


def _activate_next_prerequisite(session: Dict[str, Any]) -> Optional[str]:
    for item in session.get("prerequisite_queue") or []:
        if item.get("status") == "pending":
            item["status"] = "active"
            session["active_teaching_concept"] = item["name"]
            session["current_target"] = item["name"]
            session["teaching_mode"] = "teach_prerequisite"
            session["understanding_level"] = 0
            _begin_target_attempt(session)
            return item["name"]
    core = session.get("active_core_concept")
    session["active_teaching_concept"] = core
    session["current_target"] = core
    session["teaching_mode"] = "return_to_core"
    session["understanding_level"] = 0
    _begin_target_attempt(session)
    return core


def update_after_scored_answer(
    session: Dict[str, Any],
    *,
    question: str,
    user_input: str,
    score: int,
    grading_feedback: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Update mastery from the current smart score and plan the next turn.

    Graph expansion is limited to direct prerequisites in the frozen initial
    top-five Seed result. Only five consecutive core scores in the 0--39 band
    select one eligible prerequisite; once it is mastered, the plan returns to
    the original core concept.
    """
    ensure_state(session)
    active = session.get("active_teaching_concept") or session.get("active_core_concept")
    core = session.get("active_core_concept") or active
    mode = session.get("teaching_mode") or "diagnose_core"
    if not active:
        return {"action": "no_active_concept"}
    evidence = str(user_input or "").strip()
    state = session["concept_states"].setdefault(active, {
        "concept": active,
        "role": "prerequisite" if active != core else "core",
        "seed_rank": _seed_rank(session, core),
    })
    state.update({
        "last_score": int(score),
        "evidence": evidence,
        "mastery": "mastered" if score >= MASTERY_SCORE_THRESHOLD else (
            "unknown" if score < EXPAND_SCORE_THRESHOLD else "uncertain"
        ),
        "updated_at": _now(),
    })

    is_core_attempt = concept_key(active) == concept_key(core) and mode != "teach_prerequisite"
    consecutive_low_scores = int(session.get("core_low_score_streak") or 0)
    if is_core_attempt:
        if score < EXPAND_SCORE_THRESHOLD:
            consecutive_low_scores += 1
        else:
            consecutive_low_scores = 0
        session["core_low_score_streak"] = consecutive_low_scores

    action = "continue_current_concept"
    if mode == "teach_prerequisite":
        if score >= MASTERY_SCORE_THRESHOLD:
            for item in session.get("prerequisite_queue") or []:
                if concept_key(item.get("name")) == concept_key(active):
                    item["status"] = "mastered"
            mastered = session.setdefault("mastered_concepts", [])
            if concept_key(active) not in {concept_key(name) for name in mastered}:
                mastered.append(active)
            next_name = _activate_next_prerequisite(session)
            action = "next_prerequisite" if session.get("teaching_mode") == "teach_prerequisite" else "return_to_core"
            action_target = next_name
        else:
            action_target = active
    elif is_core_attempt and score < EXPAND_SCORE_THRESHOLD:
        if consecutive_low_scores < PREREQUISITE_TRIGGER_CONSECUTIVE_LOW_SCORES:
            action = "continue_core_before_prerequisite_gate"
            action_target = active
        else:
            feedback_parts = []
            if isinstance(grading_feedback, dict):
                for key in ("weaknesses", "explanation", "suggestions"):
                    value = grading_feedback.get(key)
                    if value:
                        feedback_parts.append(str(value))
            knowledge_gap = (
                f"question={question}\n"
                f"core_concept={core}\n"
                f"student_response={evidence or 'no usable response'}\n"
                f"smart_score={score}\n"
                f"consecutive_low_core_scores={consecutive_low_scores}\n"
                f"grading_feedback={' '.join(feedback_parts) or evidence or 'weak understanding'}"
            )
            candidates = rank_and_queue_prerequisites(session, core, knowledge_gap)
            action = "prerequisites_queued" if candidates else "no_valid_prerequisite"
            action_target = session.get("active_teaching_concept")
            # A return to the core begins a new five-turn observation window.
            session["core_low_score_streak"] = 0
    elif score < EXPAND_SCORE_THRESHOLD:
        # Low scores while teaching a non-core target stay on that target. They
        # must not recursively expand a second prerequisite level.
        action_target = active
    elif score >= MASTERY_SCORE_THRESHOLD:
        mastered = session.setdefault("mastered_concepts", [])
        if concept_key(active) not in {concept_key(name) for name in mastered}:
            mastered.append(active)
        session["teaching_mode"] = "core_mastered"
        action = "core_mastered"
        action_target = active
    else:
        action_target = active

    event = {
        "event": "scored_answer_applied",
        "concept": active,
        "core_concept": core,
        "score": int(score),
        "mastery": state["mastery"],
        "action": action,
        "action_target": action_target,
        "consecutive_low_core_scores": consecutive_low_scores,
        "prerequisite_trigger_after": PREREQUISITE_TRIGGER_CONSECUTIVE_LOW_SCORES,
        "timestamp": _now(),
    }
    session["remedial_events"].append(event)
    return event

def build_active_context(session: Dict[str, Any]) -> Dict[str, Any]:
    """Build the next prompt block from the active concept and cached chunks."""
    ensure_state(session)
    active = session.get("active_teaching_concept") or session.get("active_core_concept")
    core = session.get("active_core_concept") or active
    if not active:
        return {"text": "", "chunk_records": [], "cache_hit": False}
    role = "prerequisite" if active != core else "core"
    active_queue_item = next(
        (
            item for item in (session.get("prerequisite_queue") or [])
            if concept_key(item.get("name")) == concept_key(active)
            and item.get("status") == "active"
        ),
        {},
    )
    prerequisite_target = active_queue_item.get("parent_of") or core
    context, cache_hit = get_or_fetch_concept_context(
        session,
        active,
        role=role,
        parent_of=prerequisite_target if role == "prerequisite" else None,
    )
    initial_seeds = (session.get("initial_retrieval") or {}).get("seed_concepts") or []
    seed_summary = ", ".join(
        f"{item.get('seed_rank')}. {item.get('name')}" for item in initial_seeds
    )
    lines = [
        "\n\n**【有狀態 GraphRAG 補救學習內容】**",
        f"- 第一次固定核心知識點：{seed_summary}",
        f"- 目前核心知識點：{core or '未知'}",
        f"- 本輪實際教學概念：{active}",
        f"- 教學模式：{session.get('teaching_mode')}",
    ]
    if role == "prerequisite":
        lines.append(f"- 圖譜關係：{active} PREREQUISITE_OF {prerequisite_target}")
        if prerequisite_target != core:
            lines.append(
                f"- 路徑說明：{prerequisite_target} 是固定 top-5 中第一個具有可靠先輩教材的核心點；"
                f"完成後仍回到主要核心 {core}。"
            )
        lines.append("- 教學要求：先確認並補齊此先備概念，再銜接回核心概念。")
        lines.append(
            "- 防失真限制：本輪只允許教授上述直接先輩（圖距離 1）；"
            "不得自行延伸到它的先輩、第二層路徑或其他旁支。"
        )
        lines.append(
            "- 若上述完整教材不足以回答，必須明確說明證據不足並回到核心概念；"
            "不得自行補造未注入的圖譜內容。"
        )
    definition = str(context.get("definition") or "").strip()
    if definition:
        lines.append(f"\n**概念定義：**\n{definition}")
    trace_records = []
    selected_chunks = context.get("selected_chunks") or []
    if selected_chunks:
        lines.append("\n**完整教材 Chunk：**")
    for index, cached_record in enumerate(selected_chunks, start=1):
        record = dict(cached_record)
        raw = str(record.get("raw_content") or "")
        record["selected_for_injection"] = True
        record["selection_status"] = "selected_cached" if cache_hit else "selected"
        record["selection_priority"] = index
        record["injected_content"] = raw
        record["injected_chars"] = len(raw)
        record["is_complete"] = True
        record["omitted"] = False
        record["omitted_reason"] = None
        record["stateful_cache_hit"] = cache_hit
        trace_records.append(record)
        lines.extend([f"\n[Chunk {index}｜完整原文]", raw])
    lines.append(
        "\n**只能將上述完整教材 Chunk 視為本輪教材證據；"
        "不得把先前未選取的候選內容當成本輪已提供資料。**"
    )
    return {
        "text": "\n".join(lines),
        "chunk_records": trace_records,
        "cache_hit": cache_hit,
        "concept": active,
        "core_concept": core,
        "role": role,
        "definition": definition,
        "teaching_mode": session.get("teaching_mode"),
    }


def public_state(session: Dict[str, Any]) -> Dict[str, Any]:
    ensure_state(session)
    return {
        "initial_retrieval": session.get("initial_retrieval") or {},
        "active_core_concept": session.get("active_core_concept"),
        "active_teaching_concept": session.get("active_teaching_concept"),
        "teaching_mode": session.get("teaching_mode"),
        "concept_states": session.get("concept_states") or {},
        "prerequisite_queue": session.get("prerequisite_queue") or [],
        "prerequisite_candidates": session.get("prerequisite_candidates") or [],
        "prerequisite_rejections": session.get("prerequisite_rejections") or [],
        "last_knowledge_gap": session.get("last_knowledge_gap"),
        "core_low_score_streak": int(session.get("core_low_score_streak") or 0),
        "prerequisite_trigger_after": PREREQUISITE_TRIGGER_CONSECUTIVE_LOW_SCORES,
        "remedial_events": (session.get("remedial_events") or [])[-20:],
        "cached_concepts": [
            entry.get("concept") for entry in (session.get("concept_context_cache") or {}).values()
        ],
    }
