import unittest
from unittest.mock import patch

from flask import Flask

from src.question_concept_mapper import (
    MAPPING_SCHEMA_VERSION,
    build_mapping_query,
    map_question_to_concepts,
    normalize_concept_values,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class QuestionConceptMapperTests(unittest.TestCase):
    def test_normalizes_legacy_fields_without_placeholders(self):
        self.assertEqual(
            normalize_concept_values(["二元樹, 樹", "unknown", {"name": "節點"}]),
            ["二元樹", "樹", "節點"],
        )

    def test_vector_results_are_filtered_and_ranked(self):
        question = {
            "question_text": "學習二元樹之前需要哪些知識？",
            "key_points": "資料結構, 二元樹",
        }

        def fake_post(url, json, timeout):
            self.assertTrue(url.endswith("/retrieve"))
            self.assertEqual(json["top_k"], 8)
            return FakeResponse({
                "seed_concepts": [
                    {"name": "二元樹", "score": 0.91, "category": "資料結構"},
                    {"name": "樹", "score": 0.89, "category": "資料結構"},
                    {"name": "陣列", "score": 0.72, "category": "資料結構"},
                ],
                "retrieval_trace": {
                    "embedding": {"vector_length": 768},
                    "vector_search": {"index_name": "concept_embeddings"},
                },
            })

        mapping = map_question_to_concepts(
            question,
            resolver=lambda name: name if name in {"二元樹", "資料結構"} else None,
            post=fake_post,
            api_base="http://graph.test",
        )

        self.assertEqual(mapping["schema_version"], MAPPING_SCHEMA_VERSION)
        self.assertEqual(mapping["status"], "mapped")
        self.assertEqual(mapping["primary_concept"], "二元樹")
        self.assertEqual(mapping["concept_names"], ["二元樹", "樹", "資料結構"])
        self.assertNotIn("陣列", mapping["concept_names"])
        self.assertEqual(mapping["retrieval"]["embedding"]["vector_length"], 768)

    def test_graph_outage_uses_only_canonical_metadata(self):
        def unavailable(*args, **kwargs):
            raise ConnectionError("GraphRAG offline")

        mapping = map_question_to_concepts(
            {"question_text": "二元樹的高度為何？", "micro_concepts": ["binary tree"]},
            resolver=lambda name: "二元樹" if name == "binary tree" else None,
            post=unavailable,
            api_base="http://graph.test",
        )

        self.assertEqual(mapping["status"], "mapped_from_metadata")
        self.assertEqual(mapping["primary_concept"], "二元樹")
        self.assertEqual(mapping["source"], "canonical_metadata_fallback")
        self.assertIn("GraphRAG offline", mapping["retrieval"]["error"])

    def test_unresolved_question_is_explicit_error(self):
        mapping = map_question_to_concepts(
            {"question_text": "完全未知的題目"},
            resolver=lambda name: None,
            post=lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("offline")),
            api_base="http://graph.test",
        )
        self.assertEqual(mapping["status"], "error")
        self.assertIsNone(mapping["primary_concept"])
        self.assertEqual(mapping["concept_names"], [])

    def test_mapping_query_includes_question_and_hints(self):
        query = build_mapping_query({
            "question_text": "BST 的左子樹有何限制？",
            "key-points": ["二元搜尋樹", "左子樹"],
        })
        self.assertIn("BST 的左子樹", query)
        self.assertIn("二元搜尋樹", query)

    def test_debug_api_exposes_mapping_contract(self):
        from src.graphrag_proxy import graphrag_bp

        app = Flask(__name__)
        app.register_blueprint(graphrag_bp)
        expected = {
            "schema_version": MAPPING_SCHEMA_VERSION,
            "status": "mapped",
            "primary_concept": "二元樹",
            "concept_names": ["二元樹"],
            "concepts": [{"name": "二元樹", "role": "primary"}],
        }
        with patch(
            "src.question_concept_mapper.map_question_to_concepts",
            return_value=expected,
        ):
            response = app.test_client().post(
                "/api/graphrag/question-concepts/map",
                json={"question_text": "何謂二元樹？"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["primary_concept"], "二元樹")


if __name__ == "__main__":
    unittest.main()
