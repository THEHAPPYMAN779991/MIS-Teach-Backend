import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "tool" / "rescore_rag_evidence_v2.py"
SPEC = importlib.util.spec_from_file_location("rescore_rag_evidence_v2", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _chunk(chunk_id, answer_ids=None, prereq_ids=None, chunk_type="direct_answer"):
    return {
        "review_chunk_id": chunk_id,
        "chunk_type": chunk_type,
        "direct_answer_relevance": 3 if answer_ids else 0,
        "prerequisite_learning_value": 3 if prereq_ids else 0,
        "extended_context_value": 0,
        "bridge_clear": 1 if prereq_ids else 0,
        "relation_direction_valid": None,
        "explains_assigned_concept": 1 if prereq_ids else 0,
        "contains_usable_evidence": 1 if (answer_ids or prereq_ids) else 0,
        "noise": 0,
        "duplicate": 0,
        "used_by_answer": 0,
        "supported_answer_ids": answer_ids or [],
        "supported_prerequisite_ids": prereq_ids or [],
        "reason": "test",
    }


def _review(graph_chunks, chroma_chunks, prereqs=True):
    return {
        "schema_version": "ai_chunk_review_v2",
        "review_limitations": [],
        "required_answer_key_points": [
            {"id": "A1", "description": "first"},
            {"id": "A2", "description": "second"},
        ],
        "required_prerequisite_concepts": (
            [{"id": "P1", "description": "base"}] if prereqs else []
        ),
        "backend_reviews": {
            "graphrag": {"chunks": graph_chunks},
            "chromadb": {"chunks": chroma_chunks},
        },
    }


def _sources():
    return {
        "graphrag": [
            {"review_chunk_id": "graphrag_C001"},
            {"review_chunk_id": "graphrag_C002"},
        ],
        "chromadb": [{"review_chunk_id": "chromadb_C001"}],
    }


def test_unique_required_ids_keep_coverage_bounded():
    review = _review(
        [
            _chunk("graphrag_C001", ["A1", "A2"], ["P1"]),
            _chunk("graphrag_C002", ["A1", "A2"], ["P1"]),
        ],
        [_chunk("chromadb_C001", ["A1"], [])],
    )
    validation = MODULE.validate_review(review, _sources())
    assert validation["ok"], validation
    graph = MODULE.metric_for_backend("graphrag", review, {})
    assert graph["answer_key_point_coverage"] == 1.0
    assert graph["prerequisite_coverage"] == 1.0
    assert 0 <= graph["aes_answer_evidence_score"] <= 1
    assert 0 <= graph["pls_prerequisite_learning_score"] <= 1


def test_required_prereq_with_no_candidate_scores_zero_not_none():
    review = _review(
        [_chunk("graphrag_C001", ["A1"]), _chunk("graphrag_C002")],
        [_chunk("chromadb_C001", ["A1"])],
    )
    graph = MODULE.metric_for_backend("graphrag", review, {})
    chroma = MODULE.metric_for_backend("chromadb", review, {})
    assert graph["prerequisite_precision"] == 0.0
    assert graph["prerequisite_coverage"] == 0.0
    assert graph["pls_prerequisite_learning_score"] == 0.0
    assert chroma["pls_prerequisite_learning_score"] == 0.0


def test_no_required_prerequisite_is_not_applicable_for_both():
    review = _review(
        [_chunk("graphrag_C001", ["A1"]), _chunk("graphrag_C002")],
        [_chunk("chromadb_C001", ["A1"])],
        prereqs=False,
    )
    for backend in ("graphrag", "chromadb"):
        metric = MODULE.metric_for_backend(backend, review, {})
        assert metric["prerequisite_coverage"] is None
        assert metric["prerequisite_precision"] is None
        assert metric["pls_prerequisite_learning_score"] is None


def test_validation_rejects_unknown_supported_id_and_missing_chunk():
    review = _review(
        [_chunk("graphrag_C001", ["A9"])],
        [_chunk("chromadb_C001", ["A1"])],
    )
    validation = MODULE.validate_review(review, _sources())
    assert not validation["ok"]
    assert any("unknown_id:A9" in item for item in validation["violations"])
    assert any("missing_chunk_id:graphrag_C002" in item for item in validation["violations"])


def test_duplicate_chunk_type_is_valid_only_as_non_evidence():
    duplicate = _chunk("chromadb_C001", chunk_type="duplicate")
    duplicate.update({
        "direct_answer_relevance": 0,
        "prerequisite_learning_value": 0,
        "extended_context_value": 0,
        "duplicate": 1,
        "supported_answer_ids": [],
        "supported_prerequisite_ids": [],
    })
    review = _review([], [duplicate])
    validation = MODULE.validate_review(
        review,
        {"graphrag": [], "chromadb": [{"review_chunk_id": "chromadb_C001"}]},
    )
    assert validation["ok"], validation["violations"]

    duplicate["supported_answer_ids"] = ["A1"]
    validation = MODULE.validate_review(
        review,
        {"graphrag": [], "chromadb": [{"review_chunk_id": "chromadb_C001"}]},
    )
    assert not validation["ok"]
    assert any("duplicate_type_cannot_support_ids" in item for item in validation["violations"])
