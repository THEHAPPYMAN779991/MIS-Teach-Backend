import hashlib
import unittest
from unittest.mock import patch

from src import graphrag_chunk_reranker as reranker
from src.rag_backend_api import _comparison_prompt


def _record(section, concept, text, ordinal):
    return {
        "section": section,
        "concept": concept,
        "concepts": [concept],
        "ordinal": ordinal,
        "chunk_id": f"chunk-{ordinal}",
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "raw_content": text,
        "raw_chars": len(text),
        "injected_content": "",
        "injected_chars": 0,
        "is_complete": False,
        "omitted": True,
        "omitted_reason": "not_processed",
    }


class GraphRagChunkRerankerTests(unittest.TestCase):
    def test_initial_seed_selection_keeps_every_unique_chunk_without_scoring(self):
        reference = _record(
            "core",
            "HuffmanCode",
            "References\n1. Huffman paper.\n2. Coding paper.",
            1,
        )
        exercise = _record(
            "core",
            "HuffmanCodes",
            "Exercises\n10.3 Construct the Huffman code from frequencies.",
            2,
        )
        duplicate = _record(
            "core",
            "HuffmanCoding",
            exercise["raw_content"],
            3,
        )

        with patch.object(reranker, "_semantic_scores") as semantic_scores:
            result = reranker.select_initial_seed_chunks(
                [reference, exercise, duplicate]
            )

        self.assertEqual(result["selected"], [reference, exercise])
        self.assertTrue(reference["selected_for_injection"])
        self.assertTrue(exercise["selected_for_injection"])
        self.assertEqual(duplicate["omitted_reason"], "duplicate_content")
        self.assertEqual(set(exercise["concepts"]), {"HuffmanCodes", "HuffmanCoding"})
        self.assertEqual(result["summary"]["scoring_mode"], "initial_seed_chunks_unfiltered")
        self.assertFalse(result["summary"]["enabled"])
        self.assertEqual(result["summary"]["noise_filtered_count"], 0)
        semantic_scores.assert_not_called()

    def test_global_dedup_noise_filter_and_question_ranking(self):
        relevant = (
            "Huffman coding begins by counting character frequency. "
            "Combine the two least frequent nodes to build the Huffman tree."
        )
        records = [
            _record("prerequisite", "OptimalPrefixCode", relevant, 1),
            _record("core", "HuffmanCodingTree", relevant, 2),
            _record(
                "core",
                "HuffmanCode",
                "References\n1. A. Author, Journal of Algorithms.\n"
                "2. B. Author, Addison-Wesley.\n3. C. Author, University Press.",
                3,
            ),
            _record(
                "core",
                "BinaryTrie",
                "Suffix arrays store suffixes for efficient pattern matching.",
                4,
            ),
            _record(
                "core",
                "HuffmanCode",
                ("Unrelated chapter summary. " * 2)
                + "\nExercises\n10.1 First problem\n10.2 Construct a Huffman code\n",
                5,
            ),
        ]

        # After exact deduplication and reference filtering the eligible order is
        # the canonical relevant core chunk followed by the suffix-array chunk.
        with patch.object(
            reranker,
            "_semantic_scores",
            return_value=([0.90, 0.20], "test-embedding", None),
        ):
            result = reranker.rerank_chunk_candidates(
                "Please build a Huffman tree from character frequencies.",
                records,
                max_chunks=5,
                max_prereq_chunks=2,
            )

        selected = result["selected"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["section"], "core")
        self.assertEqual(selected[0]["raw_content"], relevant)
        self.assertEqual(
            set(selected[0]["concepts"]),
            {"OptimalPrefixCode", "HuffmanCodingTree"},
        )

        by_index = {record["candidate_index"]: record for record in result["records"]}
        self.assertEqual(by_index[1]["omitted_reason"], "duplicate_content")
        self.assertEqual(by_index[3]["omitted_reason"], "reference_section")
        self.assertEqual(by_index[4]["omitted_reason"], "low_question_relevance")
        self.assertEqual(by_index[5]["omitted_reason"], "exercises_section")
        self.assertEqual(result["summary"]["duplicate_count"], 1)
        self.assertEqual(result["summary"]["noise_filtered_count"], 2)

    def test_best_non_noise_candidate_is_kept_when_absolute_scores_are_low(self):
        record = _record(
            "core",
            "WirelessTechnology",
            "Mobile devices can use wireless networks and positioning services.",
            1,
        )
        with patch.object(
            reranker,
            "_semantic_scores",
            return_value=([0.01], "test-embedding", None),
        ):
            result = reranker.rerank_chunk_candidates(
                "How does WiFi positioning work?",
                [record],
                max_chunks=5,
            )

        self.assertEqual(result["selected"], [record])
        self.assertEqual(record["selection_status"], "selected_low_confidence")
        self.assertTrue(result["summary"]["best_available_fallback"])

    def test_long_chunk_with_only_incidental_concept_mention_is_penalized(self):
        direct = _record(
            "core",
            "HuffmanCodingTree",
            "Huffman coding uses character frequency. " * 80,
            1,
        )
        incidental = _record(
            "core",
            "HuffmanCodingTree",
            ("Optimal binary search tree dynamic programming recurrence. " * 55)
            + "Huffman is mentioned only for comparison.",
            2,
        )
        with patch.object(
            reranker,
            "_semantic_scores",
            return_value=([0.70, 0.68], "test-embedding", None),
        ):
            result = reranker.rerank_chunk_candidates(
                "Build a Huffman coding tree.",
                [direct, incidental],
                max_chunks=5,
            )

        self.assertEqual(result["selected"], [direct])
        self.assertEqual(incidental["weak_concept_evidence_penalty"], 0.14)
        self.assertEqual(incidental["concept_anchor_tokens"], ["huffman"])
        self.assertEqual(incidental["concept_anchor_evidence_count"], 1)
        self.assertEqual(incidental["omitted_reason"], "low_question_relevance")

    def test_relevant_prerequisite_is_injected_before_core_with_core_bridge(self):
        prerequisite = _record(
            "prerequisite",
            "ConcurrencyControl",
            "Concurrency control coordinates transactions before isolation is analyzed.",
            1,
        )
        prerequisite["concept_definition"] = (
            "Concurrency control is a prerequisite for transaction isolation."
        )
        prerequisite["prerequisite_targets"] = ["TransactionIsolation"]
        prerequisite["prerequisite_target_ranks"] = {"TransactionIsolation": 1}
        prerequisite["source_seed_rank"] = 1
        prerequisite["prerequisite_distance"] = 1
        core = _record(
            "core",
            "TransactionIsolation",
            "Transaction isolation defines which concurrent effects are visible.",
            2,
        )
        with patch.object(
            reranker,
            "_semantic_scores",
            side_effect=[
                ([0.66], "test-embedding", None),
                ([0.61], "test-embedding", None),
            ],
        ):
            result = reranker.rerank_chunk_candidates(
                "Explain transaction isolation under concurrency.",
                [prerequisite, core],
                max_chunks=2,
                max_prereq_chunks=4,
            )

        self.assertEqual(result["selected"], [prerequisite, core])
        self.assertEqual(prerequisite["selection_priority"], 1)
        self.assertEqual(core["selection_priority"], 2)
        self.assertEqual(prerequisite["section_priority_bonus"], 0.0)
        self.assertEqual(prerequisite["scoring_basis"], "prerequisite_concept")
        self.assertIn("Concurrency control is a prerequisite", prerequisite["scoring_query"])
        self.assertNotEqual(prerequisite["scoring_query"], "Explain transaction isolation under concurrency.")
        self.assertEqual(core["scoring_basis"], "original_question")
        self.assertTrue(result["summary"]["prerequisite_first"])

    def test_comparison_retrieval_query_does_not_leak_answer_or_image_base64(self):
        _, search_query, mode = _comparison_prompt({
            "question": "Please build a Huffman tree.",
            "student_answer": "data:image/png;base64," + ("A" * 5000),
            "correct_answer": "SECRET EXPECTED ANSWER",
        })

        self.assertEqual(mode, "wrong_answer_feedback")
        self.assertEqual(search_query, "Please build a Huffman tree.")
        self.assertNotIn("SECRET", search_query)
        self.assertNotIn("base64", search_query)


if __name__ == "__main__":
    unittest.main()
