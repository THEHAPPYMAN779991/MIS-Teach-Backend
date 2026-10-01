"""Map quiz questions to canonical concepts in the new GraphRAG graph.

The mapping is intentionally separate from grading. A grade answers whether the
student response is correct; this module answers which graph concepts were
assessed. The resulting snapshot can be stored with an attempt so later graph
updates do not silently rewrite learning history.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests

logger = logging.getLogger(__name__)

MAPPING_SCHEMA_VERSION = "graphrag-question-concept/v1"
DEFAULT_TOP_K = max(1, min(int(os.getenv("QUESTION_CONCEPT_TOP_K", "8")), 20))
DEFAULT_MAX_CONCEPTS = max(
    1, min(int(os.getenv("QUESTION_CONCEPT_MAX_CONCEPTS", "5")), 10)
)
MIN_VECTOR_SCORE = float(os.getenv("QUESTION_CONCEPT_MIN_SCORE", "0.70"))
SCORE_WINDOW = float(os.getenv("QUESTION_CONCEPT_SCORE_WINDOW", "0.04"))

_PLACEHOLDERS = {
    "", "unknown", "none", "null", "未知", "未知概念", "未知領域",
    "計算機概論", "ai生成", "基於內容生成",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unique(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        result.append(text)
    return result


def normalize_concept_values(value: Any) -> List[str]:
    """Normalize legacy key-points/micro_concepts values into clean hints."""
    if value is None:
        return []
    if isinstance(value, dict):
        return normalize_concept_values(
            value.get("name") or value.get("concept") or value.get("id")
        )
    if isinstance(value, (list, tuple, set)):
        flattened: List[str] = []
        for item in value:
            flattened.extend(normalize_concept_values(item))
        return _unique(flattened)

    text = str(value).strip()
    if not text:
        return []
    parts = re.split(r"[,，;；\n|]+", text)
    cleaned = []
    for part in parts:
        hint = part.strip().strip("[](){}\"'")
        if hint.casefold() in _PLACEHOLDERS:
            continue
        if 1 <= len(hint) <= 120:
            cleaned.append(hint)
    return _unique(cleaned)


def collect_question_hints(question: Dict[str, Any]) -> List[str]:
    hints: List[str] = []
    for key in (
        "primary_concept",
        "concept_names",
        "micro_concepts",
        "key_points",
        "key-points",
        "concept",
        "concept_name",
        "topic",
    ):
        hints.extend(normalize_concept_values(question.get(key)))
    return _unique(hints)


def build_mapping_query(question: Dict[str, Any]) -> str:
    question_text = str(
        question.get("question_text")
        or question.get("group_question_text")
        or ""
    ).strip()
    hints = collect_question_hints(question)
    options = normalize_concept_values(question.get("options"))

    sections = [question_text]
    if hints:
        sections.append("題目既有標記：" + "、".join(hints[:12]))
    if options and len(question_text) < 1200:
        sections.append("選項：" + "；".join(options[:8]))
    return "\n".join(section for section in sections if section)[:4000]


def _default_resolver(name: str) -> Optional[str]:
    from src.graphrag_client import resolve_concept_name

    return resolve_concept_name(name)


def _default_api_base() -> str:
    from src.graphrag_client import GRAPHRAG_API_BASE

    return GRAPHRAG_API_BASE.rstrip("/")


def _score(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def map_question_to_concepts(
    question: Dict[str, Any],
    *,
    top_k: int = DEFAULT_TOP_K,
    max_concepts: int = DEFAULT_MAX_CONCEPTS,
    resolver: Optional[Callable[[str], Optional[str]]] = None,
    post: Optional[Callable[..., Any]] = None,
    api_base: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a versioned, auditable question-to-concept mapping."""
    resolver = resolver or _default_resolver
    post = post or requests.post
    query = build_mapping_query(question)
    hints = collect_question_hints(question)
    api_base = (api_base or _default_api_base()).rstrip("/")
    top_k = max(1, min(int(top_k), 20))
    max_concepts = max(1, min(int(max_concepts), 10))

    canonical_hints: List[str] = []
    for hint in hints[:12]:
        try:
            resolved = resolver(hint)
        except Exception as exc:
            logger.debug("GraphRAG hint resolution failed for %s: %s", hint, exc)
            resolved = None
        if resolved:
            canonical_hints.append(resolved)
    canonical_hints = _unique(canonical_hints)

    result: Dict[str, Any] = {}
    retrieval_error = None
    if query:
        try:
            response = post(
                f"{api_base}/retrieve",
                json={
                    "question": query,
                    "top_k": top_k,
                    "prereq_depth": 1,
                    "descendant_depth": 1,
                },
                timeout=float(os.getenv("QUESTION_CONCEPT_TIMEOUT", "30")),
            )
            response.raise_for_status()
            result = response.json() or {}
        except Exception as exc:
            retrieval_error = str(exc)
            logger.warning("Question concept retrieval failed: %s", exc)

    seeds = []
    for rank, seed in enumerate(result.get("seed_concepts") or [], 1):
        if isinstance(seed, str):
            seed = {"name": seed}
        name = str(seed.get("name") or "").strip()
        if not name:
            continue
        seeds.append({
            "name": name,
            "score": _score(seed.get("score")),
            "category": seed.get("category") or "",
            "definition": str(seed.get("definition") or "")[:300],
            "rank": rank,
            "source": "vector",
        })

    scored = [seed for seed in seeds if seed["score"] is not None]
    if scored:
        best_score = max(seed["score"] for seed in scored)
        threshold = max(MIN_VECTOR_SCORE, best_score - SCORE_WINDOW)
        selected = [seed for seed in seeds if seed["score"] is not None and seed["score"] >= threshold]
    else:
        best_score = None
        threshold = None
        selected = seeds[:1]

    concepts: List[Dict[str, Any]] = []
    seen = set()
    for seed in selected:
        key = seed["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        concepts.append(seed)
        if len(concepts) >= max_concepts:
            break

    # Exact/alias metadata mappings remain useful when the vector service is down.
    for name in canonical_hints:
        if len(concepts) >= max_concepts:
            break
        if name.casefold() in seen:
            continue
        seen.add(name.casefold())
        concepts.append({
            "name": name,
            "score": None,
            "category": "",
            "definition": "",
            "rank": len(concepts) + 1,
            "source": "metadata_hint",
        })

    for index, concept in enumerate(concepts):
        concept["role"] = "primary" if index == 0 else "secondary"

    if seeds:
        status = "mapped"
        source = "graphrag_vector"
    elif concepts:
        status = "mapped_from_metadata"
        source = "canonical_metadata_fallback"
    elif retrieval_error:
        status = "error"
        source = "unavailable"
    else:
        status = "no_hits"
        source = "graphrag_vector"

    retrieval_trace = result.get("retrieval_trace") or {}
    return {
        "schema_version": MAPPING_SCHEMA_VERSION,
        "status": status,
        "source": source,
        "primary_concept": concepts[0]["name"] if concepts else None,
        "concept_names": [concept["name"] for concept in concepts],
        "concepts": concepts,
        "explicit_hints": hints,
        "canonical_hints": canonical_hints,
        "query": query,
        "mapped_at": _utc_now(),
        "confidence": best_score,
        "selection": {
            "top_k": top_k,
            "max_concepts": max_concepts,
            "min_score": MIN_VECTOR_SCORE,
            "score_window": SCORE_WINDOW,
            "effective_threshold": threshold,
            "retrieved_seed_count": len(seeds),
            "selected_count": len(concepts),
        },
        "retrieval": {
            "endpoint": f"{api_base}/retrieve",
            "embedding": retrieval_trace.get("embedding") or {},
            "vector_search": retrieval_trace.get("vector_search") or {},
            "graph_expansion": retrieval_trace.get("graph_expansion") or {},
            "error": retrieval_error,
        },
    }


def load_persisted_mapping(question_id: Any) -> Optional[Dict[str, Any]]:
    if not question_id:
        return None
    try:
        from bson import ObjectId
        from accessories import mongo

        candidates = [question_id]
        text_id = str(question_id)
        if len(text_id) == 24:
            try:
                candidates.insert(0, ObjectId(text_id))
            except Exception:
                pass
        for collection_name in ("exam", "test5"):
            collection = getattr(mongo.db, collection_name)
            for candidate in candidates:
                doc = collection.find_one(
                    {"_id": candidate}, {"concept_mapping": 1}
                )
                mapping = (doc or {}).get("concept_mapping")
                if mapping and mapping.get("schema_version") == MAPPING_SCHEMA_VERSION:
                    return mapping
    except Exception as exc:
        logger.debug("Unable to load persisted question mapping: %s", exc)
    return None


def persist_question_mapping(question_id: Any, mapping: Dict[str, Any]) -> bool:
    if not question_id or not mapping or not mapping.get("primary_concept"):
        return False
    try:
        from bson import ObjectId
        from accessories import mongo

        candidates = [question_id]
        text_id = str(question_id)
        if len(text_id) == 24:
            try:
                candidates.insert(0, ObjectId(text_id))
            except Exception:
                pass
        update = {
            "$set": {
                "concept_mapping": mapping,
                "primary_concept": mapping.get("primary_concept"),
                "concept_names": mapping.get("concept_names") or [],
            }
        }
        for collection_name in ("exam", "test5"):
            collection = getattr(mongo.db, collection_name)
            for candidate in candidates:
                result = collection.update_one({"_id": candidate}, update)
                if result.matched_count:
                    return True
    except Exception as exc:
        logger.warning("Unable to persist question concept mapping: %s", exc)
    return False


def ensure_question_concept_mapping(
    question: Dict[str, Any],
    question_id: Any = None,
    *,
    persist: bool = True,
) -> Dict[str, Any]:
    existing = question.get("concept_mapping") or load_persisted_mapping(question_id)
    if existing and existing.get("schema_version") == MAPPING_SCHEMA_VERSION:
        return existing

    mapping = map_question_to_concepts(question)
    if persist and mapping.get("primary_concept"):
        persist_question_mapping(question_id, mapping)
    return mapping
