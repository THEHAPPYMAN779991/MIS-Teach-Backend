import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import chromadb_rag, chromadb_trace, graphrag_client, graphrag_trace, rag_backend
from src.rag_backend_api import _run_comparison_side
from src.rag_sys import rag_ai_role


class GraphRagChunkAuditTests(unittest.TestCase):
    def tearDown(self):
        graphrag_trace.clear_current_trace()
        rag_ai_role.reset_graphrag_usage()

    def test_long_textbook_chunk_is_injected_verbatim_and_audited(self):
        raw_chunk = "BEGIN\n" + ("full textbook evidence " * 180) + "\nEND"
        result = {
            "seed_concepts": ["TargetConcept"],
            "expanded": [
                {
                    "name": "TargetConcept",
                    "definition": "Target definition",
                    "sample_chunks": [raw_chunk],
                    "prerequisites": [],
                    "leads_to": [],
                    "parents": [],
                    "subtypes": [],
                    "book_ids": ["book-test"],
                }
            ],
            "prereq_chains": [],
            "descendant_chains": [],
        }

        graphrag_trace.start_trace("focused question", route="test")
        with (
            patch.object(rag_backend, "get_active_backend", return_value="graphrag"),
            patch.object(rag_ai_role, "should_search_database", return_value=True),
            patch.object(graphrag_client, "query_natural_language", return_value=result),
        ):
            enhanced = rag_ai_role.enhance_prompt_with_knowledge(
                "BASE PROMPT",
                "focused question",
                top_k=1,
            )

        usage = rag_ai_role.get_last_graphrag_usage()
        trace = graphrag_trace.get_current_trace()
        record = trace["chunks"]["records"][0]

        self.assertIn(raw_chunk, enhanced)
        self.assertEqual(record["raw_content"], raw_chunk)
        self.assertEqual(record["injected_content"], raw_chunk)
        self.assertTrue(record["is_complete"])
        self.assertFalse(record["omitted"])
        self.assertEqual(usage["retrieved_chunk_count"], 1)
        self.assertEqual(usage["injected_chunk_count"], 1)
        self.assertEqual(usage["complete_injected_chunk_count"], 1)
        self.assertEqual(usage["shortened_chunk_count"], 0)
        self.assertEqual(usage["omitted_chunk_count"], 0)
        self.assertFalse(usage["context_truncated"])

    def test_trace_json_preserves_raw_chunk_beyond_preview_limit(self):
        raw_chunk = "R" * (graphrag_trace.MAX_TEXT_LENGTH + 500)
        record = {
            "section": "core",
            "concept": "TargetConcept",
            "concepts": ["TargetConcept"],
            "ordinal": 1,
            "chunk_id": "chunk-test",
            "content_sha256": "hash-test",
            "source": "book.md",
            "source_ids": ["book-test"],
            "chunk_seq_id": 1,
            "raw_content": raw_chunk,
            "raw_chars": len(raw_chunk),
            "injected_content": raw_chunk,
            "injected_chars": len(raw_chunk),
            "is_complete": True,
            "omitted": False,
            "omitted_reason": None,
        }
        usage = graphrag_trace._base_usage()
        usage.update({
            "used": True,
            "reason": "used",
            "retrieved_chunk_count": 1,
            "unique_retrieved_chunk_count": 1,
            "injected_chunk_count": 1,
            "complete_injected_chunk_count": 1,
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(graphrag_trace, "TRACE_DIR", Path(tmpdir)):
                graphrag_trace.start_trace("focused question", route="test")
                graphrag_trace.record_query(
                    question="focused question",
                    top_k=1,
                    api_base="http://example.test",
                    result={"seed_concepts": ["TargetConcept"]},
                    usage=usage,
                    latency_ms=1.0,
                    prompt_before_chars=4,
                    prompt_after_chars=4 + len(raw_chunk),
                    prompt_before_text="BASE",
                    prompt_after_text="BASE" + raw_chunk,
                    injected_context_text=raw_chunk,
                    chunk_records=[record],
                )
                finalized = graphrag_trace.finalize_trace("answer", usage)

                with open(finalized["trace_json_path"], encoding="utf-8") as handle:
                    saved = json.load(handle)
                markdown = Path(finalized["trace_markdown_path"]).read_text(encoding="utf-8")

        self.assertEqual(saved["chunks"]["records"][0]["raw_content"], raw_chunk)
        self.assertEqual(saved["chunks"]["records"][0]["injected_content"], raw_chunk)
        self.assertIn(raw_chunk, markdown)

    def test_initial_prompt_uses_only_seed_chunks_in_upstream_order(self):
        result = {
            "seed_concepts": ["CoreFirst", "CoreSecond"],
            "expanded": [
                {
                    "name": "CoreFirst",
                    "definition": "First target",
                    "sample_chunks": ["CoreFirst target explanation."],
                },
                {
                    "name": "CoreSecond",
                    "definition": "Second target",
                    "sample_chunks": ["CoreSecond target explanation."],
                },
            ],
            # Deliberately return the second seed's prerequisite first.  The
            # application must restore original seed priority.
            "prereq_chains": [
                {
                    "concept": "CoreSecond",
                    "ancestors": [
                        {"name": "PrereqSecond", "distance": 1, "definition": "Second foundation"}
                    ],
                },
                {
                    "concept": "CoreFirst",
                    "ancestors": [
                        {"name": "PrereqFirst", "distance": 1, "definition": "First foundation"}
                    ],
                },
            ],
            "descendant_chains": [],
        }

        graphrag_trace.start_trace("target question", route="test")
        with (
            patch.object(rag_backend, "get_active_backend", return_value="graphrag"),
            patch.object(rag_ai_role, "should_search_database", return_value=True),
            patch.object(graphrag_client, "query_natural_language", return_value=result),
            patch.object(graphrag_client, "fetch_concept_profile") as fetch_profile,
            patch("src.graphrag_chunk_reranker._semantic_scores") as semantic_scores,
        ):
            enhanced = rag_ai_role.enhance_prompt_with_knowledge(
                "BASE PROMPT",
                "target question",
                top_k=5,
            )

        records = graphrag_trace.get_current_trace()["chunks"]["records"]
        selected = sorted(
            (record for record in records if record.get("selected_for_injection")),
            key=lambda record: record["selection_priority"],
        )
        self.assertEqual(
            [record["concept"] for record in selected],
            ["CoreFirst", "CoreSecond"],
        )
        self.assertNotIn("PrereqFirst", enhanced)
        self.assertNotIn("PrereqSecond", enhanced)
        self.assertTrue(all(record["section"] == "core" for record in records))
        self.assertTrue(all(
            record["scoring_basis"] == "initial_seed_order" for record in selected
        ))
        fetch_profile.assert_not_called()
        semantic_scores.assert_not_called()

    def test_initial_prompt_injects_no_prerequisites_or_descendants(self):
        result = {
            "seed_concepts": ["CoreConcept"],
            "expanded": [
                {
                    "name": "CoreConcept",
                    "definition": "Core definition",
                    "sample_chunks": ["Core complete textbook evidence."],
                    "prerequisites": ["DirectPrerequisite"],
                    "leads_to": ["LockedNextConcept"],
                    "parents": ["TaxonomyParent"],
                    "subtypes": ["LockedSubtype"],
                }
            ],
            "prereq_chains": [
                {
                    "concept": "CoreConcept",
                    "ancestors": [
                        {
                            "name": "DirectPrerequisite",
                            "distance": 1,
                            "definition": "Direct foundation",
                        },
                        {
                            "name": "FarAncestor",
                            "distance": 2,
                            "definition": "Must stay audit-only",
                        },
                    ],
                }
            ],
            "descendant_chains": [
                {
                    "concept": "CoreConcept",
                    "descendants": [
                        {"name": "LockedNextConcept", "distance": 1},
                        {"name": "FarDescendant", "distance": 2},
                    ],
                }
            ],
        }

        graphrag_trace.start_trace("target question", route="test")
        with (
            patch.object(rag_backend, "get_active_backend", return_value="graphrag"),
            patch.object(rag_ai_role, "should_search_database", return_value=True),
            patch.object(graphrag_client, "query_natural_language", return_value=result),
            patch.object(graphrag_client, "fetch_concept_profile") as fetch_profile,
            patch("src.graphrag_chunk_reranker._semantic_scores") as semantic_scores,
        ):
            enhanced = rag_ai_role.enhance_prompt_with_knowledge(
                "BASE PROMPT",
                "target question",
                top_k=5,
            )

        usage = rag_ai_role.get_last_graphrag_usage()
        self.assertIn("Core complete textbook evidence.", enhanced)
        self.assertNotIn("DirectPrerequisite", enhanced)
        self.assertNotIn("FarAncestor", enhanced)
        self.assertNotIn("LockedNextConcept", enhanced)
        self.assertNotIn("FarDescendant", enhanced)
        self.assertNotIn("後續延伸知識節點", enhanced)
        self.assertNotIn("TaxonomyParent", enhanced)
        self.assertNotIn("LockedSubtype", enhanced)
        self.assertEqual(usage["prereq_node_count"], 0)
        self.assertEqual(usage["retrieved_prereq_node_count"], 2)
        self.assertEqual(usage["descendant_node_count"], 0)
        self.assertEqual(usage["retrieved_descendant_node_count"], 2)
        self.assertFalse(usage["descendant_context_unlocked"])
        self.assertEqual(usage["prerequisite_name_depth"], 0)
        fetch_profile.assert_not_called()
        semantic_scores.assert_not_called()

    def test_explainable_prerequisite_mode_injects_only_distance_one_with_path(self):
        result = {
            "seed_concepts": ["CoreConcept"],
            "expanded": [
                {
                    "name": "CoreConcept",
                    "definition": "Core definition",
                    "sample_chunks": ["Core complete textbook evidence."],
                }
            ],
            "prereq_chains": [
                {
                    "concept": "CoreConcept",
                    "ancestors": [
                        {"name": "DirectFoundation", "distance": 1},
                        {"name": "FarFoundation", "distance": 2},
                    ],
                }
            ],
            "descendant_chains": [
                {"concept": "CoreConcept", "descendants": [{"name": "LockedNext", "distance": 1}]}
            ],
        }

        def profile(name, **_kwargs):
            if name == "CoreConcept":
                return {
                    "name": name,
                    "definition": f"{name} definition",
                    "sample_chunk_records": [
                        {
                            "text": "Core complete textbook evidence.",
                            "source": "book.md",
                            "chunk_id": "chunk-core",
                            "chunk_seq_id": 4,
                        }
                    ],
                }
            return {
                "name": name,
                "definition": f"{name} definition",
                "sample_chunk_records": [
                    {
                        "text": f"{name} complete prerequisite evidence.",
                        "source": "book.md",
                        "chunk_id": f"chunk-{name}",
                        "page_start": 10,
                        "page_end": 10,
                    }
                ],
            }

        graphrag_trace.start_trace("target question", route="test")
        with (
            patch.object(rag_backend, "get_active_backend", return_value="graphrag"),
            patch.object(rag_ai_role, "should_search_database", return_value=True),
            patch.object(graphrag_client, "query_natural_language", return_value=result),
            patch.object(graphrag_client, "fetch_concept_profile", side_effect=profile) as fetch_profile,
        ):
            enhanced = rag_ai_role.enhance_prompt_with_knowledge(
                "BASE PROMPT",
                "target question",
                top_k=5,
                graph_context_mode="prerequisite_d1",
                max_prereq_concepts=1,
            )

        usage = rag_ai_role.get_last_graphrag_usage()
        records = graphrag_trace.get_current_trace()["chunks"]["records"]
        core_records = [record for record in records if record.get("section") == "core"]
        prereq_records = [record for record in records if record.get("section") == "prerequisite"]

        self.assertIn("DirectFoundation complete prerequisite evidence.", enhanced)
        self.assertIn("DirectFoundation --PREREQUISITE_OF--> CoreConcept", enhanced)
        self.assertNotIn("FarFoundation complete prerequisite evidence.", enhanced)
        self.assertNotIn("LockedNext", enhanced)
        self.assertEqual(usage["graph_context_mode"], "prerequisite_d1")
        self.assertEqual(usage["seed_chunk_metadata_total"], 1)
        self.assertEqual(usage["seed_chunk_metadata_resolved"], 1)
        self.assertEqual(usage["seed_chunk_metadata_unresolved"], 0)
        self.assertEqual(usage["prereq_chunk_count"], 1)
        self.assertEqual(usage["injected_prerequisite_concepts"], ["DirectFoundation"])
        self.assertEqual(len(core_records), 1)
        self.assertEqual(core_records[0]["source"], "book.md")
        self.assertEqual(core_records[0]["chunk_seq_id"], 4)
        self.assertTrue(core_records[0]["precise_source_locator"])
        self.assertEqual(core_records[0]["metadata_match_status"], "exact_neo4j_text_match")
        self.assertEqual(len(prereq_records), 1)
        self.assertEqual(
            prereq_records[0]["selection_reason"],
            "direct_prerequisite_distance_1_seed_order",
        )
        self.assertEqual(
            prereq_records[0]["graph_path"],
            [{"from": "DirectFoundation", "relation": "PREREQUISITE_OF", "to": "CoreConcept"}],
        )
        self.assertIsNotNone(prereq_records[0]["prompt_span"])
        self.assertEqual(fetch_profile.call_count, 2)

    def test_chromadb_comparison_reports_only_verbatim_injected_chunks_as_complete(self):
        fake_hits = [
            {
                "content": "full chunk one",
                "metadata": {"source": "book.md", "chunk": 1},
                "distance": 0.1,
            },
            {
                "content": "full chunk two",
                "metadata": {"source": "book.md", "chunk": 2},
                "distance": 0.2,
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(chromadb_trace, "TRACE_DIR", Path(tmpdir)),
                patch.object(rag_ai_role, "call_gemini_api", return_value="answer"),
                patch.object(
                    chromadb_rag,
                    "search_knowledge_chromadb",
                    return_value=fake_hits,
                ),
            ):
                side = _run_comparison_side(
                    "chromadb",
                    "BASE",
                    "focused question",
                    2,
                )

        self.assertTrue(side["ok"])
        self.assertEqual(side["chunk_summary"]["retrieved"], 2)
        self.assertEqual(side["chunk_summary"]["injected"], 2)
        self.assertEqual(side["chunk_summary"]["complete_injected"], 2)
        self.assertEqual(side["chunk_summary"]["shortened"], 0)
        self.assertEqual(side["chunk_summary"]["omitted"], 0)
        self.assertTrue(
            side["chunk_summary"]["valid_for_complete_context_comparison"]
        )
        self.assertTrue(
            all(
                record["raw_content"] == record["injected_content"]
                and record["is_complete"]
                for record in side["chunks"]
            )
        )


if __name__ == "__main__":
    unittest.main()
