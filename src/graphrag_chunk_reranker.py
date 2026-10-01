"""Question-aware filtering and reranking for GraphRAG textbook chunks.

GraphRAG's upstream vector search ranks concepts.  A concept may then be linked
to several textbook chunks whose order is merely their order in the book.  This
module adds the missing second-stage ranking: candidate chunks are preserved for
audit, exact duplicates and obvious back-matter noise are rejected, and the
remaining *complete* chunks are ranked against the original question.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    value = str(os.getenv(name, "1" if default else "0")).strip().casefold()
    return value not in {"0", "false", "no", "off", "disabled"}


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


RERANK_ENABLED = _env_bool("GRAPHRAG_CHUNK_RERANK_ENABLED", True)
FILTER_NOISE_SECTIONS = _env_bool("GRAPHRAG_FILTER_NOISE_SECTIONS", True)
MIN_RELEVANCE_SCORE = _env_float(
    "GRAPHRAG_CHUNK_RERANK_MIN_SCORE", 0.25, 0.0, 1.0
)
SCORE_WINDOW = _env_float(
    "GRAPHRAG_CHUNK_RERANK_SCORE_WINDOW", 0.05, 0.0, 1.0
)
MAX_PREREQ_CHUNKS = _env_int(
    "GRAPHRAG_MAX_INJECTED_PREREQ_CHUNKS", 5, 0, 20
)
PREREQUISITE_FIRST = _env_bool("GRAPHRAG_PREREQUISITE_FIRST", True)
MIN_CORE_CHUNKS = _env_int(
    "GRAPHRAG_MIN_CORE_CHUNKS", 1, 0, 20
)
PREREQUISITE_SCORE_BONUS = _env_float(
    "GRAPHRAG_PREREQUISITE_SCORE_BONUS", 0.0, 0.0, 0.25
)
SCORING_WINDOW_CHARS = _env_int(
    "GRAPHRAG_RERANK_WINDOW_CHARS", 1200, 300, 4000
)
SCORING_WINDOW_OVERLAP = _env_int(
    "GRAPHRAG_RERANK_WINDOW_OVERLAP", 200, 0, 1000
)
MAX_SCORING_WINDOWS = _env_int(
    "GRAPHRAG_RERANK_MAX_WINDOWS", 8, 1, 30
)


_EMBEDDING_NAME = "chromadb-default/all-MiniLM-L6-v2"
_embedding_function: Any = None
_embedding_lock = threading.RLock()


_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "because", "by", "can",
    "describe", "device", "do", "does", "explain", "for", "following",
    "from", "given", "how", "in", "includes", "is", "it", "of", "on",
    "please", "show", "suppose", "that", "the", "this", "to", "use",
    "using", "want", "we", "which", "why", "with", "your",
    "一個", "以及", "使用", "如何", "為何", "什麼", "以下", "請", "說明",
}

# These words are too broad to prove that a long chunk is actually about the
# concept selected upstream.  More distinctive overlap such as "Huffman" or
# "WiFi" can be used as a topical anchor.
_GENERIC_CONCEPT_ANCHORS = {
    "algorithm", "character", "cod", "code", "coding", "computer", "condition",
    "cycle", "data", "encoding", "frequency", "instruction", "memory",
    "network", "problem", "process", "resource", "system", "technology",
    "tree",
}


def _split_camel_case(value: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value or "")


def _english_token_variants(token: str) -> set[str]:
    """Return conservative inflection variants for retrieval term matching."""
    variants = {token}
    if token.endswith("ing") and len(token) > 5:
        stem = token[:-3]
        variants.update({stem, stem + "e"})
    if token.endswith("ed") and len(token) > 4:
        stem = token[:-2]
        variants.update({stem, stem + "e"})
    if token.endswith("es") and len(token) > 4:
        stem = token[:-2]
        variants.update({stem, stem + "e"})
    elif token.endswith("s") and len(token) > 3:
        variants.add(token[:-1])
    return {value for value in variants if len(value) > 1}


def _tokenize(value: str) -> set[str]:
    text = _split_camel_case(str(value or "")).casefold()
    tokens: set[str] = set()
    for token in re.findall(r"[a-z][a-z0-9_-]{1,}|\d+", text):
        if token not in _STOPWORDS and len(token) > 1:
            tokens.update(_english_token_variants(token))
    # Whitespace tokenization is weak for Chinese.  Character bigrams provide a
    # deterministic lexical fallback without changing the injected chunk text.
    for run in re.findall(r"[\u3400-\u9fff]{2,}", text):
        tokens.update(run[index:index + 2] for index in range(len(run) - 1))
    return tokens


def _coverage_score(query_tokens: set[str], value: str) -> float:
    if not query_tokens:
        return 0.0
    value_tokens = _tokenize(value)
    if not value_tokens:
        return 0.0
    return len(query_tokens & value_tokens) / len(query_tokens)


def _weak_concept_evidence_penalty(
    query_tokens: set[str],
    concept_text: str,
    raw_content: str,
) -> Tuple[float, List[str], int]:
    """Penalize long chunks that only mention the matched concept in passing.

    Graph traversal can reach a chunk because its metadata says "Huffman" even
    when the body is predominantly about another topic.  This check uses only
    distinctive concept tokens that are also present in the original question;
    it never edits or shortens the raw chunk.
    """
    text = str(raw_content or "")
    if len(text) < 1800:
        return 0.0, [], 0
    anchors = sorted(
        (query_tokens & _tokenize(concept_text)) - _GENERIC_CONCEPT_ANCHORS
    )
    if not anchors:
        return 0.0, [], 0

    folded = text.casefold()
    distinct_question_evidence = []
    for token in sorted(query_tokens - _GENERIC_CONCEPT_ANCHORS):
        if re.fullmatch(r"[a-z0-9_-]+", token):
            pattern = rf"(?<![a-z0-9_-]){re.escape(token)}(?:s|es|ed|d|ing)?(?![a-z0-9_-])"
            if re.search(pattern, folded):
                distinct_question_evidence.append(token)
        elif token in folded:
            distinct_question_evidence.append(token)

    evidence_count = 0
    for anchor in anchors:
        if re.fullmatch(r"[a-z0-9_-]+", anchor):
            evidence_count += len(
                re.findall(rf"(?<![a-z0-9_-]){re.escape(anchor)}(?![a-z0-9_-])", folded)
            )
        else:
            evidence_count += folded.count(anchor)
    # A chunk may mention the concept label only once yet explain several
    # question-specific parts (for example FETCH, DECODE and EXECUTE).  That is
    # substantive evidence and must not receive the incidental-mention penalty.
    if evidence_count <= 1 and len(distinct_question_evidence) < 3:
        return 0.14, anchors, evidence_count
    return 0.0, anchors, evidence_count


def _content_hash(record: Dict[str, Any]) -> str:
    digest = str(record.get("content_sha256") or "").strip()
    if digest:
        return digest
    raw = str(record.get("raw_content") or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _noise_reason(raw_content: str) -> Optional[str]:
    if not FILTER_NOISE_SECTIONS:
        return None
    text = str(raw_content or "")
    beginning = re.sub(r"\s+", " ", text[:500]).strip().casefold()
    if re.match(r"^(?:\d+\s+)?references\b", beginning):
        return "reference_section"
    if re.match(r"^(?:\d+\s+)?(?:index|subject index)\b", beginning):
        return "index_section"
    if re.match(r"^(?:[a-z]-?\d+\s+)?glossary\b", beginning):
        return "glossary_section"
    if "practice exercises" in beginning[:220]:
        return "practice_exercises_section"
    exercise_heading = re.search(
        r"(?:^|\n)\s*(?:practice\s+)?exercises\s*(?:\n|$)",
        text,
        flags=re.IGNORECASE,
    )
    if exercise_heading and exercise_heading.start() <= int(len(text) * 0.55):
        exercise_tail = text[exercise_heading.end():]
        numbered_exercises = len(
            re.findall(r"(?:^|\n)\s*\d{1,2}\.\d{1,3}\b", exercise_tail)
        )
        if numbered_exercises >= 2:
            return "exercises_section"

    numbered_citations = len(
        re.findall(r"(?:^|\n)\s*\d{1,3}\.\s+[A-Z]", text, flags=re.MULTILINE)
    )
    publication_markers = len(
        re.findall(
            r"\b(?:journal|proceedings|addison-wesley|springer|prentice(?:-| )hall|"
            r"university press|communications of the acm|vol\.|edition)\b",
            text,
            flags=re.IGNORECASE,
        )
    )
    if numbered_citations >= 3 and publication_markers >= 2:
        return "reference_list"
    return None


def _scoring_windows(raw_content: str) -> List[str]:
    text = str(raw_content or "")
    if len(text) <= SCORING_WINDOW_CHARS:
        return [text]
    step = max(1, SCORING_WINDOW_CHARS - SCORING_WINDOW_OVERLAP)
    windows = []
    for start in range(0, len(text), step):
        windows.append(text[start:start + SCORING_WINDOW_CHARS])
        if start + SCORING_WINDOW_CHARS >= len(text):
            break
        if len(windows) >= MAX_SCORING_WINDOWS:
            break
    return windows or [text]


def _get_embedding_function() -> Any:
    global _embedding_function
    if _embedding_function is not None:
        return _embedding_function
    with _embedding_lock:
        if _embedding_function is None:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

            _embedding_function = DefaultEmbeddingFunction()
    return _embedding_function


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return max(-1.0, min(1.0, numerator / (left_norm * right_norm)))


def _semantic_scores(
    question: str,
    records: Sequence[Dict[str, Any]],
) -> Tuple[Optional[List[float]], str, Optional[str]]:
    if not records:
        return [], _EMBEDDING_NAME, None
    flattened = [str(question or "")]
    owners: List[int] = []
    for index, record in enumerate(records):
        for window in _scoring_windows(str(record.get("raw_content") or "")):
            owners.append(index)
            flattened.append(window)
    try:
        with _embedding_lock:
            vectors = _get_embedding_function()(flattened)
        question_vector = vectors[0]
        window_scores: List[List[float]] = [[] for _ in records]
        for owner, vector in zip(owners, vectors[1:]):
            window_scores[owner].append(_cosine(question_vector, vector))

        scores: List[float] = []
        for values in window_scores:
            if not values:
                scores.append(0.0)
                continue
            top = sorted(values, reverse=True)[:2]
            # A single matching sentence in a long unrelated chunk should not
            # dominate completely, so combine the best window with top-window mean.
            scores.append(0.70 * top[0] + 0.30 * (sum(top) / len(top)))
        return scores, _EMBEDDING_NAME, None
    except Exception as exc:
        logger.warning("GraphRAG chunk semantic reranking fell back to lexical: %s", exc)
        return None, "lexical_fallback", str(exc)


def score_texts_against_query(
    query: str,
    texts: Sequence[str],
) -> Tuple[List[float], str, Optional[str]]:
    """Score short concept descriptions against a knowledge-gap query.

    This public wrapper is used by the stateful remedial-learning planner.  It
    shares the same local embedding model as chunk reranking and falls back to
    deterministic lexical coverage when the model is unavailable.
    """
    records = [{"raw_content": str(value or "")} for value in texts]
    scores, backend, error = _semantic_scores(query, records)
    if scores is not None:
        return [round(max(0.0, min(1.0, float(score))), 6) for score in scores], backend, error
    query_tokens = _tokenize(query)
    lexical = [
        round(_coverage_score(query_tokens, str(value or "")), 6)
        for value in texts
    ]
    return lexical, backend, error


def _prepare_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prepared = []
    for candidate_index, original in enumerate(records, start=1):
        record = original
        record["candidate_index"] = candidate_index
        record["content_sha256"] = _content_hash(record)
        record["selected_for_injection"] = False
        record["selection_status"] = "candidate"
        record["relevance_rank"] = None
        record["relevance_score"] = None
        record["semantic_score"] = None
        record["lexical_score"] = None
        record["concept_match_score"] = None
        record["weak_concept_evidence_penalty"] = 0.0
        record["concept_anchor_tokens"] = []
        record["concept_anchor_evidence_count"] = 0
        record["section_priority_bonus"] = 0.0
        record["selection_priority"] = None
        record["scoring_basis"] = None
        record["scoring_query"] = None
        record["score_scope_rank"] = None
        record["effective_threshold"] = None
        record["duplicate_of_sha256"] = None
        record.setdefault("injected_content", "")
        record.setdefault("injected_chars", 0)
        record.setdefault("is_complete", False)
        record["omitted"] = True
        record["omitted_reason"] = "not_selected"
        prepared.append(record)
    return prepared


def select_initial_seed_chunks(
    records: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """LEGACY seed-only selector; not used by strict_research_policy_v1.

    Select every unique chunk attached directly to the initial seed concepts.

    Initial discovery is an experimental observation point: it must expose what
    the upstream top-five concepts actually provide, without semantic reranking,
    score thresholds, or section-based noise filtering.  Exact duplicate text is
    injected once while every duplicate retrieval remains in the audit records.
    Prerequisite chunks are intentionally outside this function; they belong to
    the stateful follow-up learning flow.
    """
    all_records = _prepare_records(records)
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for record in all_records:
        groups.setdefault(record["content_sha256"], []).append(record)

    selected: List[Dict[str, Any]] = []
    duplicate_count = 0
    for group in groups.values():
        canonical = min(
            group,
            key=lambda item: int(item.get("candidate_index") or 0),
        )
        concepts: List[str] = []
        for item in group:
            for concept in item.get("concepts") or [item.get("concept")]:
                concept = str(concept or "").strip()
                if concept and concept not in concepts:
                    concepts.append(concept)
        canonical["concepts"] = concepts
        canonical["candidate_sections"] = list(dict.fromkeys(
            str(item.get("section") or "") for item in group
            if item.get("section")
        ))
        canonical["duplicate_candidate_count"] = max(0, len(group) - 1)
        canonical["scoring_basis"] = "initial_seed_order"
        canonical["scoring_query"] = None
        selected.append(canonical)

        for duplicate in group:
            if duplicate is canonical:
                continue
            duplicate_count += 1
            duplicate["selection_status"] = "rejected"
            duplicate["omitted_reason"] = "duplicate_content"
            duplicate["duplicate_of_sha256"] = canonical["content_sha256"]

    selected.sort(key=lambda item: int(item.get("candidate_index") or 0))
    for priority, record in enumerate(selected, start=1):
        record["selected_for_injection"] = True
        record["selection_status"] = "selected_unfiltered"
        record["selection_priority"] = priority
        record["relevance_rank"] = priority
        record["omitted_reason"] = "awaiting_prompt_budget"

    return {
        "records": all_records,
        "selected": selected,
        "summary": {
            "enabled": False,
            "backend": "not_run",
            "error": None,
            "candidate_count": len(all_records),
            "unique_candidate_count": len(groups),
            "eligible_unique_count": len(groups),
            "selected_count": len(selected),
            "rejected_count": duplicate_count,
            "duplicate_count": duplicate_count,
            "noise_filtered_count": 0,
            "max_chunks": None,
            "max_prereq_chunks": 0,
            "prerequisite_first": False,
            "min_core_chunks": len(selected),
            "prerequisite_score_bonus": 0.0,
            "selected_prereq_count": 0,
            "min_score": None,
            "score_window": None,
            "scoring_mode": "initial_seed_chunks_unfiltered",
            "best_score": None,
            "core_best_score": None,
            "prerequisite_best_score": None,
            "effective_threshold": None,
            "prerequisite_thresholds": [],
            "best_available_fallback": False,
            "question": None,
        },
    }


def rerank_chunk_candidates(
    question: str,
    records: Iterable[Dict[str, Any]],
    *,
    max_chunks: int,
    max_prereq_chunks: int = MAX_PREREQ_CHUNKS,
) -> Dict[str, Any]:
    """Rank full GraphRAG chunk candidates and return an auditable selection.

    The input dictionaries are annotated in place so the trace can retain every
    candidate, including duplicates and rejected noise.  Selected records still
    contain the original unmodified ``raw_content`` for verbatim prompt injection.
    """
    all_records = _prepare_records(records)
    max_chunks = max(1, int(max_chunks or 1))
    max_prereq_chunks = max(0, int(max_prereq_chunks or 0))

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for record in all_records:
        groups.setdefault(record["content_sha256"], []).append(record)

    canonical_records: List[Dict[str, Any]] = []
    duplicate_count = 0
    for digest, group in groups.items():
        canonical = sorted(
            group,
            key=lambda item: (
                0 if item.get("section") == "core" else 1,
                int(item.get("candidate_index") or 0),
            ),
        )[0]
        concepts = []
        sections = []
        prerequisite_targets = []
        prerequisite_target_ranks: Dict[str, int] = {}
        prerequisite_distances = []
        source_seed_ranks = []
        for item in group:
            for concept in item.get("concepts") or [item.get("concept")]:
                concept = str(concept or "").strip()
                if concept and concept not in concepts:
                    concepts.append(concept)
            section = str(item.get("section") or "").strip()
            if section and section not in sections:
                sections.append(section)
            for target in item.get("prerequisite_targets") or []:
                target = str(target or "").strip()
                if target and target not in prerequisite_targets:
                    prerequisite_targets.append(target)
            for target, rank in (item.get("prerequisite_target_ranks") or {}).items():
                try:
                    numeric_rank = int(rank)
                except (TypeError, ValueError):
                    continue
                prerequisite_target_ranks[str(target)] = min(
                    prerequisite_target_ranks.get(str(target), numeric_rank),
                    numeric_rank,
                )
            try:
                prerequisite_distances.append(int(item.get("prerequisite_distance")))
            except (TypeError, ValueError):
                pass
            try:
                source_seed_ranks.append(int(item.get("source_seed_rank")))
            except (TypeError, ValueError):
                pass
        canonical["concepts"] = concepts
        canonical["candidate_sections"] = sections
        canonical["duplicate_candidate_count"] = max(0, len(group) - 1)
        if canonical.get("section") == "prerequisite":
            canonical["prerequisite_targets"] = prerequisite_targets
            canonical["prerequisite_target_ranks"] = prerequisite_target_ranks
            if prerequisite_distances:
                canonical["prerequisite_distance"] = min(prerequisite_distances)
            if source_seed_ranks:
                canonical["source_seed_rank"] = min(source_seed_ranks)
        canonical_records.append(canonical)
        for duplicate in group:
            if duplicate is canonical:
                continue
            duplicate_count += 1
            duplicate["selection_status"] = "rejected"
            duplicate["omitted_reason"] = "duplicate_content"
            duplicate["duplicate_of_sha256"] = digest

    eligible: List[Dict[str, Any]] = []
    noise_filtered_count = 0
    for record in canonical_records:
        reason = _noise_reason(str(record.get("raw_content") or ""))
        if reason:
            noise_filtered_count += 1
            record["selection_status"] = "rejected"
            record["omitted_reason"] = reason
        else:
            eligible.append(record)

    core_eligible = [
        record for record in eligible if record.get("section") != "prerequisite"
    ]
    prereq_groups: Dict[str, List[Dict[str, Any]]] = {}
    for record in eligible:
        if record.get("section") != "prerequisite":
            record["scoring_basis"] = "original_question"
            record["scoring_query"] = str(question or "")
            continue
        concept_name = str(record.get("concept") or "").strip()
        definition = str(record.get("concept_definition") or "").strip()
        scoring_query = ". ".join(
            value for value in (concept_name, definition) if value
        ) or concept_name
        record["scoring_basis"] = "prerequisite_concept"
        record["scoring_query"] = scoring_query
        prereq_groups.setdefault(scoring_query, []).append(record)

    semantic_by_record: Dict[int, Optional[float]] = {}
    rerank_backends: List[str] = []
    rerank_errors: List[str] = []

    def score_semantic_group(scoring_query: str, group: List[Dict[str, Any]]) -> None:
        scores, group_backend, group_error = _semantic_scores(scoring_query, group)
        if group_backend and group_backend not in rerank_backends:
            rerank_backends.append(group_backend)
        if group_error:
            rerank_errors.append(group_error)
        for index, grouped_record in enumerate(group):
            semantic_by_record[id(grouped_record)] = (
                scores[index] if scores is not None and index < len(scores) else None
            )

    if core_eligible:
        score_semantic_group(str(question or ""), core_eligible)
    for scoring_query, group in prereq_groups.items():
        score_semantic_group(scoring_query, group)

    for record in eligible:
        scoring_query = str(record.get("scoring_query") or question or "")
        query_tokens = _tokenize(scoring_query)
        lexical_score = _coverage_score(query_tokens, str(record.get("raw_content") or ""))
        concept_text = " ".join(record.get("concepts") or [record.get("concept") or ""])
        concept_score = _coverage_score(query_tokens, concept_text)
        weak_penalty, anchor_tokens, anchor_count = _weak_concept_evidence_penalty(
            query_tokens,
            concept_text,
            str(record.get("raw_content") or ""),
        )
        semantic_score = semantic_by_record.get(id(record))
        if semantic_score is None:
            relevance_score = 0.65 * lexical_score + 0.35 * concept_score
        else:
            relevance_score = (
                0.82 * semantic_score
                + 0.12 * lexical_score
                + 0.06 * concept_score
            )
        section_bonus = (
            PREREQUISITE_SCORE_BONUS
            if PREREQUISITE_FIRST and record.get("section") == "prerequisite"
            else 0.0
        )
        relevance_score += section_bonus
        relevance_score -= weak_penalty
        record["semantic_score"] = (
            round(float(semantic_score), 6) if semantic_score is not None else None
        )
        record["lexical_score"] = round(float(lexical_score), 6)
        record["concept_match_score"] = round(float(concept_score), 6)
        record["weak_concept_evidence_penalty"] = round(float(weak_penalty), 6)
        record["concept_anchor_tokens"] = anchor_tokens
        record["concept_anchor_evidence_count"] = anchor_count
        record["section_priority_bonus"] = round(float(section_bonus), 6)
        record["relevance_score"] = round(max(0.0, min(1.0, relevance_score)), 6)

    core_ranked = sorted(
        core_eligible,
        key=lambda item: (
            -float(item.get("relevance_score") or 0.0),
            int(item.get("candidate_index") or 0),
        ),
    )
    prereq_ranked = sorted(
        [record for record in eligible if record.get("section") == "prerequisite"],
        key=lambda item: (
            int(item.get("source_seed_rank") or 999999),
            int(item.get("prerequisite_distance") or 999999),
            -float(item.get("relevance_score") or 0.0),
            int(item.get("candidate_index") or 0),
        ),
    )
    ranked_by_score = sorted(
        eligible,
        key=lambda item: (
            -float(item.get("relevance_score") or 0.0),
            int(item.get("candidate_index") or 0),
        ),
    )
    for rank, record in enumerate(ranked_by_score, start=1):
        record["relevance_rank"] = rank

    score_scope_groups: Dict[str, List[Dict[str, Any]]] = {}
    for record in eligible:
        scope_key = f"{record.get('scoring_basis')}::{record.get('scoring_query')}"
        score_scope_groups.setdefault(scope_key, []).append(record)

    qualified: List[Dict[str, Any]] = []
    scope_thresholds: Dict[str, float] = {}
    for scope_key, group in score_scope_groups.items():
        scoped_ranked = sorted(
            group,
            key=lambda item: -float(item.get("relevance_score") or 0.0),
        )
        scoped_best = float(scoped_ranked[0].get("relevance_score") or 0.0)
        threshold = max(MIN_RELEVANCE_SCORE, scoped_best - SCORE_WINDOW)
        scope_thresholds[scope_key] = round(threshold, 6)
        for scope_rank, record in enumerate(scoped_ranked, start=1):
            record["score_scope_rank"] = scope_rank
            record["effective_threshold"] = round(threshold, 6)
            score = float(record.get("relevance_score") or 0.0)
            qualifies = not RERANK_ENABLED or score >= threshold
            if not qualifies:
                record["selection_status"] = "rejected"
                record["omitted_reason"] = (
                    "low_concept_relevance"
                    if record.get("section") == "prerequisite"
                    else "low_question_relevance"
                )
                continue
            qualified.append(record)

    qualified_ids = {id(record) for record in qualified}

    if PREREQUISITE_FIRST:
        prereq_candidates = [
            record for record in prereq_ranked if id(record) in qualified_ids
        ]
        core_candidates = [
            record for record in core_ranked if id(record) in qualified_ids
        ]
        # With a one-chunk prompt, prerequisite-first means teaching the
        # foundation.  With more room, retain at least one core chunk so the
        # model can explicitly bridge the prerequisite back to the target.
        core_reserve = min(
            MIN_CORE_CHUNKS,
            len(core_candidates),
            max(0, max_chunks - 1) if prereq_candidates else max_chunks,
        )
        prereq_slots = min(
            max_prereq_chunks,
            max(0, max_chunks - core_reserve),
        )
        chosen_prereqs = prereq_candidates[:prereq_slots]
        remaining_slots = max(0, max_chunks - len(chosen_prereqs))
        chosen_cores = core_candidates[:remaining_slots]
        selected = chosen_prereqs + chosen_cores
    else:
        selected = []
        prereq_selected = 0
        for record in ranked_by_score:
            if id(record) not in qualified_ids:
                continue
            if len(selected) >= max_chunks:
                break
            if record.get("section") == "prerequisite":
                if prereq_selected >= max_prereq_chunks:
                    continue
                prereq_selected += 1
            selected.append(record)

    selected_ids = {id(record) for record in selected}
    for priority, record in enumerate(selected, start=1):
        record["selected_for_injection"] = True
        record["selection_status"] = "selected"
        record["selection_priority"] = priority
        record["omitted_reason"] = "awaiting_prompt_budget"
    for record in qualified:
        if id(record) in selected_ids:
            continue
        record["selection_status"] = "rejected"
        if record.get("section") == "prerequisite":
            record["omitted_reason"] = "prerequisite_limit"
        else:
            record["omitted_reason"] = "rerank_limit"

    # Never turn a non-noise retrieval into an empty prompt merely because a
    # model's absolute score calibration shifted.  Keep the single best chunk
    # and mark coverage as low so the trace remains honest.
    best_available_fallback = False
    fallback_candidates = core_ranked or prereq_ranked
    if not selected and fallback_candidates:
        fallback = fallback_candidates[0]
        fallback["selected_for_injection"] = True
        fallback["selection_status"] = "selected_low_confidence"
        fallback["selection_priority"] = 1
        fallback["omitted_reason"] = "awaiting_prompt_budget"
        selected = [fallback]
        best_available_fallback = True

    rejected_count = sum(
        1 for record in all_records if not record.get("selected_for_injection")
    )
    backend = "+".join(rerank_backends) if rerank_backends else "not_run"
    rerank_error = "; ".join(dict.fromkeys(rerank_errors)) or None
    all_scores = [
        float(record.get("relevance_score") or 0.0) for record in eligible
    ]
    core_scores = [
        float(record.get("relevance_score") or 0.0) for record in core_eligible
    ]
    prereq_scores = [
        float(record.get("relevance_score") or 0.0)
        for record in eligible
        if record.get("section") == "prerequisite"
    ]
    core_scope_key = f"original_question::{str(question or '')}"
    effective_threshold = scope_thresholds.get(core_scope_key)
    prerequisite_thresholds = []
    seen_prereq_queries = set()
    for record in prereq_ranked:
        scoring_query = str(record.get("scoring_query") or "")
        if scoring_query in seen_prereq_queries:
            continue
        seen_prereq_queries.add(scoring_query)
        prerequisite_thresholds.append({
            "concept": record.get("concept"),
            "source_seed_rank": record.get("source_seed_rank"),
            "distance": record.get("prerequisite_distance"),
            "threshold": record.get("effective_threshold"),
        })
    return {
        "records": all_records,
        "selected": selected,
        "summary": {
            "enabled": RERANK_ENABLED,
            "backend": backend,
            "error": rerank_error,
            "candidate_count": len(all_records),
            "unique_candidate_count": len(groups),
            "eligible_unique_count": len(eligible),
            "selected_count": len(selected),
            "rejected_count": rejected_count,
            "duplicate_count": duplicate_count,
            "noise_filtered_count": noise_filtered_count,
            "max_chunks": max_chunks,
            "max_prereq_chunks": max_prereq_chunks,
            "prerequisite_first": PREREQUISITE_FIRST,
            "min_core_chunks": MIN_CORE_CHUNKS,
            "prerequisite_score_bonus": PREREQUISITE_SCORE_BONUS,
            "selected_prereq_count": sum(
                1 for record in selected if record.get("section") == "prerequisite"
            ),
            "min_score": MIN_RELEVANCE_SCORE,
            "score_window": SCORE_WINDOW,
            "scoring_mode": "dual_query",
            "best_score": round(max(all_scores), 6) if all_scores else None,
            "core_best_score": round(max(core_scores), 6) if core_scores else None,
            "prerequisite_best_score": (
                round(max(prereq_scores), 6) if prereq_scores else None
            ),
            "effective_threshold": effective_threshold,
            "prerequisite_thresholds": prerequisite_thresholds,
            "best_available_fallback": best_available_fallback,
            "question": str(question or ""),
        },
    }
