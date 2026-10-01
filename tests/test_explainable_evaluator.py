import unittest
from unittest.mock import patch

from tool import grade_triple_compare as grader


class ExplainableEvaluatorTests(unittest.TestCase):
    def test_json_evaluator_uses_structured_output_and_disables_thinking(self):
        payload = '{"contexts":{"X":{},"Y":{}}}'
        with patch(
            "src.rag_sys.rag_ai_role.call_gemini_api",
            return_value=payload,
        ) as call_api:
            result = grader._call_json_evaluator("PROMPT", "contexts")

        self.assertTrue(result["ok"])
        config = call_api.call_args.kwargs["generation_config_override"]
        self.assertEqual(config["max_output_tokens"], 8192)
        self.assertEqual(config["temperature"], 0.0)
        self.assertEqual(config["response_mime_type"], "application/json")
        self.assertEqual(config["thinking_config"], {"thinking_budget": 0})

    def test_retrieval_evaluator_requests_compact_reason_codes(self):
        captured = {}
        label_to_backend = grader._retrieval_backend_labels("q1")

        def fake_evaluator(prompt, required_root):
            captured["prompt"] = prompt
            captured["required_root"] = required_root
            return {
                "ok": True,
                "error": None,
                "parsed_response": {
                    "contexts": {
                        label: {
                            "chunks": [],
                            "prerequisite_usefulness": (
                                40 if backend == "graphrag" else None
                            ),
                        }
                        for label, backend in label_to_backend.items()
                    }
                },
            }

        sides = {
            "graphrag": {
                "chunks": [{
                    "chunk_id": "graph-1",
                    "raw_content": "Graph evidence",
                    "selected_for_injection": True,
                    "retrieval_stage": "seed",
                    "concept": "ConceptA",
                    "source": "book-a",
                }, {
                    "chunk_id": "graph-rejected",
                    "raw_content": "REJECTED DUPLICATE MUST NOT BE EVALUATED",
                    "selected_for_injection": False,
                    "retrieval_stage": "prerequisite",
                    "concept": "DuplicateConcept",
                    "source": "book-a",
                }, {
                    "chunk_id": "graph-prerequisite",
                    "raw_content": "Injected prerequisite evidence",
                    "selected_for_injection": True,
                    "retrieval_stage": "prerequisite",
                    "concept": "FoundationA",
                    "source": "book-a",
                }]
            },
            "chromadb": {
                "chunks": [{
                    "chunk_id": "vector-1",
                    "raw_content": "Vector evidence",
                    "selected_for_injection": True,
                    "retrieval_stage": "vector",
                    "source": "book-b.md",
                }]
            },
        }

        with patch.object(grader, "_call_json_evaluator", side_effect=fake_evaluator):
            result = grader._evaluate_retrieval_quality(
                question_id="q1",
                question="Explain the concept",
                sides=sides,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["required_root"], "contexts")
        self.assertIn("reason_code", captured["prompt"])
        self.assertIn("不要輸出逐 Chunk 的自然語言理由", captured["prompt"])
        self.assertNotIn('"reason":""', captured["prompt"])
        self.assertNotIn("REJECTED DUPLICATE MUST NOT BE EVALUATED", captured["prompt"])
        self.assertIn("Injected prerequisite evidence", captured["prompt"])
        self.assertEqual(result["evaluation_scope"], "actual_injected_chunks_only")
        self.assertEqual(len(result["chunk_label_maps"]["graphrag"]), 2)
        self.assertEqual(len(result["chunk_label_maps"]["chromadb"]), 1)
        self.assertTrue(result["prerequisite_context_present"]["graphrag"])
        self.assertFalse(result["prerequisite_context_present"]["chromadb"])

    def test_retrieval_evaluator_rejects_null_when_prerequisite_was_injected(self):
        with patch.object(
            grader,
            "_call_json_evaluator",
            return_value={
                "ok": True,
                "error": None,
                "parsed_response": {
                    "contexts": {
                        "X": {"chunks": [], "prerequisite_usefulness": None},
                        "Y": {"chunks": [], "prerequisite_usefulness": None},
                    }
                },
            },
        ):
            result = grader._evaluate_retrieval_quality(
                question_id="q2",
                question="Explain the concept",
                sides={
                    "graphrag": {"chunks": [{
                        "chunk_id": "p1",
                        "raw_content": "Prerequisite evidence",
                        "selected_for_injection": True,
                        "retrieval_stage": "prerequisite",
                    }]},
                    "chromadb": {"chunks": [{
                        "chunk_id": "v1",
                        "raw_content": "Vector evidence",
                        "selected_for_injection": True,
                        "retrieval_stage": "vector",
                    }]},
                },
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"],
            "invalid_prerequisite_usefulness_for_graphrag",
        )

    def test_formal_validity_requires_numeric_prerequisite_usefulness(self):
        common = {
            "ok": True,
            "generation_ok": True,
            "retrieval_ok": True,
            "is_fallback_response": False,
            "prompt": {"final_text": "prompt"},
            "answer_audit": {"answer_model_saw_gold": False},
        }
        sides = {
            "graphrag": {
                **common,
                "chunk_summary": {
                    "injected": 1,
                    "source_traceability_rate": 1.0,
                    "precise_source_locator_rate": 1.0,
                },
                "usage": {"prereq_chunk_count": 1},
            },
            "chromadb": {
                **common,
                "chunk_summary": {
                    "injected": 1,
                    "source_traceability_rate": 1.0,
                    "precise_source_locator_rate": 1.0,
                },
            },
            "llm_only": dict(common),
        }
        validity = grader._build_question_validity(
            doc={"_gold_audit": {"status": "approved"}},
            sides=sides,
            comparison={"chunk_comparison": {"valid": True}},
            evaluations={
                "ok": True,
                "profile": "explainable",
                "retrieval_quality": {
                    "ok": True,
                    "backend_results": {
                        "graphrag": {"prerequisite_usefulness": None},
                    },
                },
            },
            evaluation_requested=True,
            graph_context_mode="prerequisite_d1",
        )

        self.assertFalse(validity["valid_for_prerequisite_analysis"])
        self.assertFalse(validity["valid_for_formal_analysis"])
        self.assertIn(
            "prerequisite_usefulness_missing_or_invalid",
            validity["formal_excluded_reasons"],
        )


if __name__ == "__main__":
    unittest.main()
