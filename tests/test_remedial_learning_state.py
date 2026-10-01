import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import graphrag_client, graphrag_trace, rag_backend
from src import remedial_learning_state as remedial
from src.rag_sys import rag_ai_role


def _context(name, score=0.8):
    raw = f"{name} complete textbook evidence."
    return {
        "concept": name,
        "definition": f"{name} definition",
        "selected_chunks": [
            {
                "concept": name,
                "section": "prerequisite",
                "raw_content": raw,
                "raw_chars": len(raw),
                "relevance_score": score,
                "content_sha256": f"hash-{name}",
            }
        ],
        "chunk_records": [],
    }


class RemedialLearningStateTests(unittest.TestCase):
    def setUp(self):
        self._sessions = dict(rag_ai_role.learning_sessions)
        self._tutoring_mode = rag_ai_role.GRAPHRAG_TUTORING_MODE
        # This suite primarily verifies the retained legacy state machine.
        # Production defaults to one_shot; individual tests opt into it below.
        rag_ai_role.GRAPHRAG_TUTORING_MODE = "stateful"
        rag_ai_role.learning_sessions.clear()

    def tearDown(self):
        rag_ai_role.GRAPHRAG_TUTORING_MODE = self._tutoring_mode
        rag_ai_role.learning_sessions.clear()
        rag_ai_role.learning_sessions.update(self._sessions)
        graphrag_trace.clear_current_trace()
        rag_ai_role.reset_graphrag_usage()

    def test_session_key_is_stable_and_legacy_session_is_migrated(self):
        first_key = remedial.stable_session_key("student@example.com", "Question\ntext")
        second_key = remedial.stable_session_key("student@example.com", "Question\ntext")
        self.assertEqual(first_key, second_key)
        legacy = {
            "user_email": "student@example.com",
            "question": "Question\ntext",
            "conversation_history": [{"role": "assistant", "content": "saved"}],
        }
        rag_ai_role.learning_sessions["student@example.com_question_legacyhash"] = legacy
        loaded = rag_ai_role.get_or_create_session("student@example.com", "Question\ntext")
        self.assertIs(loaded, legacy)
        self.assertIn(first_key, rag_ai_role.learning_sessions)
        self.assertNotIn("student@example.com_question_legacyhash", rag_ai_role.learning_sessions)

    def test_initial_top_five_is_frozen_and_not_replaced(self):
        session = {}
        first_usage = {
            "backend": "graphrag",
            "top_k": 5,
            "seed_concepts": ["A", "B", "C", "D", "E", "F"],
        }
        self.assertTrue(remedial.freeze_initial_retrieval(session, "question", first_usage))
        self.assertFalse(
            remedial.freeze_initial_retrieval(
                session,
                "question",
                {"seed_concepts": ["X", "Y"]},
            )
        )
        frozen = session["initial_retrieval"]["seed_concepts"]
        self.assertEqual([item["name"] for item in frozen], ["A", "B", "C", "D", "E"])
        self.assertEqual([item["seed_rank"] for item in frozen], [1, 2, 3, 4, 5])
        self.assertEqual(session["active_core_concept"], "A")
        self.assertEqual(session["teaching_mode"], "diagnose_core")

    def test_prerequisite_priority_uses_student_gap_and_excludes_mastered(self):
        session = {}
        remedial.freeze_initial_retrieval(
            session,
            "Build a Huffman tree",
            {"seed_concepts": ["HuffmanCoding", "CharacterFrequency"]},
        )
        session["concept_states"]["HuffmanCoding"].update({
            "mastery": "unknown",
            "last_score": 20,
        })
        session["mastered_concepts"] = ["BinaryTree"]
        relations = {
            "prerequisites": [
                {"name": "PriorityQueue", "depth": 1, "strength": 0.9},
                {"name": "GreedyChoice", "depth": 1, "strength": 0.9},
                {"name": "BinaryTree", "depth": 1, "strength": 0.9},
                {"name": "FarAncestor", "depth": 2, "strength": 0.5},
            ]
        }

        def fake_context(_session, name, **_kwargs):
            return _context(name), False

        with (
            patch("src.graphrag_client.get_concept_relations", return_value=relations),
            patch.object(remedial, "get_or_fetch_concept_context", side_effect=fake_context),
            patch.object(
                remedial,
                "score_texts_against_query",
                return_value=([0.40, 0.90], "test-embedding", None),
            ),
        ):
            candidates = remedial.rank_and_queue_prerequisites(
                session,
                "HuffmanCoding",
                "Student does not understand why the two minimum frequencies are selected.",
            )

        self.assertEqual([item["name"] for item in candidates], ["GreedyChoice", "PriorityQueue"])
        self.assertNotIn("BinaryTree", [item["name"] for item in candidates])
        self.assertNotIn("FarAncestor", [item["name"] for item in candidates])
        self.assertEqual(session["active_teaching_concept"], "GreedyChoice")
        self.assertEqual(session["teaching_mode"], "teach_prerequisite")
        self.assertEqual(session["prerequisite_queue"][0]["status"], "active")
        self.assertGreater(
            candidates[0]["score_components"]["knowledge_gap_semantic"],
            candidates[1]["score_components"]["knowledge_gap_semantic"],
        )

    def test_low_gap_semantic_is_rejected_instead_of_forced_into_prompt(self):
        session = {}
        remedial.freeze_initial_retrieval(
            session, "question", {"seed_concepts": ["CoreConcept"]}
        )
        relations = {
            "prerequisites": [
                {"name": "UnrelatedParent", "depth": 1, "strength": 1.0}
            ]
        }
        with (
            patch("src.graphrag_client.get_concept_relations", return_value=relations),
            patch.object(
                remedial,
                "get_or_fetch_concept_context",
                return_value=(_context("UnrelatedParent", score=0.95), False),
            ),
            patch.object(
                remedial,
                "score_texts_against_query",
                return_value=([0.05], "test-embedding", None),
            ),
        ):
            candidates = remedial.rank_and_queue_prerequisites(
                session, "CoreConcept", "student cannot apply the core rule"
            )

        self.assertEqual(candidates, [])
        self.assertEqual(session["prerequisite_queue"], [])
        self.assertEqual(session["active_teaching_concept"], "CoreConcept")
        self.assertEqual(session["teaching_mode"], "remediate_core_no_prerequisite")
        self.assertEqual(
            session["prerequisite_rejections"][-1]["reason"],
            "low_knowledge_gap_semantic",
        )

    def test_low_chunk_evidence_is_rejected_even_with_strong_graph_match(self):
        session = {}
        remedial.freeze_initial_retrieval(
            session, "question", {"seed_concepts": ["CoreConcept"]}
        )
        relations = {
            "prerequisites": [
                {"name": "WeakEvidenceParent", "depth": 1, "strength": 1.0}
            ]
        }
        with (
            patch("src.graphrag_client.get_concept_relations", return_value=relations),
            patch.object(
                remedial,
                "get_or_fetch_concept_context",
                return_value=(_context("WeakEvidenceParent", score=0.10), False),
            ),
            patch.object(
                remedial,
                "score_texts_against_query",
                return_value=([0.95], "test-embedding", None),
            ),
        ):
            candidates = remedial.rank_and_queue_prerequisites(
                session, "CoreConcept", "student gap"
            )

        self.assertEqual(candidates, [])
        self.assertEqual(
            session["prerequisite_rejections"][-1]["reason"],
            "low_chunk_evidence_quality",
        )

    def test_second_level_ancestor_is_rejected_before_fetching_chunks(self):
        session = {}
        remedial.freeze_initial_retrieval(
            session, "question", {"seed_concepts": ["CoreConcept"]}
        )
        relations = {
            "prerequisites": [
                {"name": "FarAncestor", "depth": 2, "strength": 1.0}
            ]
        }
        with (
            patch("src.graphrag_client.get_concept_relations", return_value=relations),
            patch.object(remedial, "get_or_fetch_concept_context") as fetch,
        ):
            candidates = remedial.rank_and_queue_prerequisites(
                session, "CoreConcept", "student gap"
            )

        self.assertEqual(candidates, [])
        self.assertEqual(fetch.call_count, 0)
        self.assertTrue(
            all(
                item["reason"] == "graph_depth_exceeded"
                for item in session["prerequisite_rejections"]
            )
        )

    def test_prerequisite_prompt_forbids_second_level_expansion(self):
        session = {}
        remedial.freeze_initial_retrieval(
            session, "question", {"seed_concepts": ["CoreConcept"]}
        )
        session["active_teaching_concept"] = "DirectParent"
        session["teaching_mode"] = "teach_prerequisite"
        session["prerequisite_queue"] = [
            {
                "name": "DirectParent",
                "parent_of": "CoreConcept",
                "status": "active",
            }
        ]
        with patch.object(
            remedial,
            "get_or_fetch_concept_context",
            return_value=(_context("DirectParent"), True),
        ):
            block = remedial.build_active_context(session)

        self.assertIn("只允許教授上述直接先輩", block["text"])
        self.assertIn("不得自行延伸到它的先輩", block["text"])
        self.assertIn("不得自行補造未注入的圖譜內容", block["text"])

    def test_exact_concept_context_is_cached_with_full_chunk(self):
        session = {}
        remedial.freeze_initial_retrieval(
            session,
            "question",
            {"seed_concepts": ["CoreConcept"]},
        )
        profile = {
            "name": "CoreConcept",
            "definition": "Core definition",
            "prerequisites": [],
            "parents": [],
            "sample_chunk_records": [
                {"text": "COMPLETE CORE CHUNK", "chunk_id": "core-1"}
            ],
        }
        selected = {
            "section": "core",
            "concept": "CoreConcept",
            "raw_content": "COMPLETE CORE CHUNK",
            "raw_chars": 19,
            "content_sha256": "hash-core",
            "relevance_score": 0.9,
        }
        reranked = {
            "records": [selected],
            "selected": [selected],
            "summary": {"selected_count": 1},
        }
        with (
            patch("src.graphrag_client.fetch_concept_profile", return_value=profile) as fetch,
            patch.object(remedial, "rerank_chunk_candidates", return_value=reranked),
        ):
            first, first_hit = remedial.get_or_fetch_concept_context(
                session, "CoreConcept", role="core"
            )
            second, second_hit = remedial.get_or_fetch_concept_context(
                session, "CoreConcept", role="core"
            )

        self.assertFalse(first_hit)
        self.assertTrue(second_hit)
        self.assertIs(first, second)
        self.assertEqual(fetch.call_count, 1)
        block = remedial.build_active_context(session)
        self.assertTrue(block["cache_hit"])
        self.assertIn("COMPLETE CORE CHUNK", block["text"])
        self.assertEqual(
            block["chunk_records"][0]["injected_content"],
            "COMPLETE CORE CHUNK",
        )
        self.assertTrue(block["chunk_records"][0]["is_complete"])

    def test_duplicate_prerequisite_evidence_receives_redundancy_penalty(self):
        session = {}
        remedial.freeze_initial_retrieval(
            session,
            "question",
            {"seed_concepts": ["CoreConcept"]},
        )
        session["concept_states"]["CoreConcept"].update({
            "mastery": "unknown",
            "last_score": 10,
        })
        relations = {
            "prerequisites": [
                {"name": "PrereqA", "depth": 1, "strength": 0.9},
                {"name": "PrereqB", "depth": 1, "strength": 0.9},
            ]
        }

        def duplicated_context(_session, name, **_kwargs):
            context = _context(name)
            context["selected_chunks"][0]["content_sha256"] = "same-evidence"
            return context, False

        with (
            patch("src.graphrag_client.get_concept_relations", return_value=relations),
            patch.object(remedial, "get_or_fetch_concept_context", side_effect=duplicated_context),
            patch.object(
                remedial,
                "score_texts_against_query",
                return_value=([0.9, 0.8], "test-embedding", None),
            ),
        ):
            candidates = remedial.rank_and_queue_prerequisites(
                session,
                "CoreConcept",
                "student knowledge gap",
            )

        self.assertEqual(candidates[0]["name"], "PrereqA")
        self.assertEqual(
            candidates[0]["score_components"]["redundancy_penalty"], 0.0
        )
        self.assertEqual(
            candidates[1]["score_components"]["redundancy_penalty"], 0.08
        )

    def test_no_parent_on_first_seed_falls_back_in_frozen_seed_order(self):
        session = {}
        remedial.freeze_initial_retrieval(
            session,
            "question",
            {"seed_concepts": ["CoreFirst", "CoreSecond", "CoreThird"]},
        )
        session["concept_states"]["CoreFirst"].update({
            "mastery": "unknown",
            "last_score": 20,
        })

        def relations(name):
            if name == "CoreFirst":
                return {"prerequisites": []}
            if name == "CoreSecond":
                return {
                    "prerequisites": [
                        {"name": "SecondParent", "depth": 1, "strength": 0.9}
                    ]
                }
            return {
                "prerequisites": [
                    {"name": "ThirdParent", "depth": 1, "strength": 0.9}
                ]
            }

        with (
            patch("src.graphrag_client.get_concept_relations", side_effect=relations),
            patch.object(
                remedial,
                "get_or_fetch_concept_context",
                side_effect=lambda _session, name, **_kwargs: (_context(name), False),
            ),
            patch.object(
                remedial,
                "score_texts_against_query",
                return_value=([0.8], "test-embedding", None),
            ),
        ):
            candidates = remedial.rank_and_queue_prerequisites(
                session,
                "CoreFirst",
                "student gap",
            )

        self.assertEqual([item["name"] for item in candidates], ["SecondParent"])
        self.assertEqual(candidates[0]["parent_of"], "CoreSecond")
        self.assertEqual(candidates[0]["seed_rank"], 2)
        self.assertEqual(
            session["last_knowledge_gap"]["remediation_source_concept"],
            "CoreSecond",
        )
        self.assertEqual(session["active_core_concept"], "CoreFirst")

    def test_five_consecutive_low_core_scores_gate_prerequisite_teaching(self):
        session = {}
        remedial.freeze_initial_retrieval(
            session,
            "question",
            {"seed_concepts": ["CoreConcept"]},
        )

        def fake_rank(target_session, core, gap):
            target_session["prerequisite_queue"] = [
                {"name": "PrereqConcept", "status": "active", "parent_of": core}
            ]
            target_session["active_teaching_concept"] = "PrereqConcept"
            target_session["current_target"] = "PrereqConcept"
            target_session["teaching_mode"] = "teach_prerequisite"
            return target_session["prerequisite_queue"]

        with patch.object(
            remedial,
            "rank_and_queue_prerequisites",
            side_effect=fake_rank,
        ) as rank:
            first = remedial.update_after_scored_answer(
                session, question="question", user_input="weak", score=39
            )
            self.assertEqual(first["action"], "continue_core_before_prerequisite_gate")
            self.assertEqual(session["core_low_score_streak"], 1)

            # A score outside 0--39 breaks the consecutive-low sequence.
            remedial.update_after_scored_answer(
                session, question="question", user_input="partial", score=40
            )
            self.assertEqual(session["core_low_score_streak"], 0)

            for attempt in range(1, 5):
                event = remedial.update_after_scored_answer(
                    session,
                    question="question",
                    user_input=f"weak answer {attempt}",
                    score=39,
                )
                self.assertEqual(event["action"], "continue_core_before_prerequisite_gate")
                self.assertEqual(session["core_low_score_streak"], attempt)

            fifth = remedial.update_after_scored_answer(
                session, question="question", user_input="still weak", score=39
            )

        self.assertEqual(rank.call_count, 1)
        self.assertEqual(fifth["action"], "prerequisites_queued")
        self.assertEqual(fifth["consecutive_low_core_scores"], 5)
        self.assertEqual(fifth["prerequisite_trigger_after"], 5)
        self.assertEqual(session["active_teaching_concept"], "PrereqConcept")
        self.assertEqual(session["core_low_score_streak"], 0)

    def test_scored_prerequisite_moves_queue_then_returns_to_core(self):
        session = {}
        remedial.freeze_initial_retrieval(
            session,
            "question",
            {"seed_concepts": ["CoreConcept"]},
        )
        session["teaching_mode"] = "teach_prerequisite"
        session["active_teaching_concept"] = "PrereqOne"
        session["current_target"] = "PrereqOne"
        session["prerequisite_queue"] = [
            {"name": "PrereqOne", "status": "active"},
            {"name": "PrereqTwo", "status": "pending"},
        ]

        below_gate = remedial.update_after_scored_answer(
            session,
            question="question",
            user_input="correct explanation",
            score=89,
        )
        self.assertEqual(below_gate["action"], "continue_current_concept")
        self.assertEqual(session["active_teaching_concept"], "PrereqOne")

        first = remedial.update_after_scored_answer(
            session,
            question="question",
            user_input="complete explanation",
            score=90,
        )
        self.assertEqual(first["action"], "next_prerequisite")
        self.assertEqual(session["active_teaching_concept"], "PrereqTwo")

        second = remedial.update_after_scored_answer(
            session,
            question="question",
            user_input="another correct explanation",
            score=90,
        )
        self.assertEqual(second["action"], "return_to_core")
        self.assertEqual(session["active_teaching_concept"], "CoreConcept")
        self.assertEqual(session["teaching_mode"], "return_to_core")

    def test_initial_wrong_answer_can_prime_next_turn_with_grading_evidence(self):
        session = {}
        remedial.freeze_initial_retrieval(
            session,
            "question",
            {"seed_concepts": ["CoreConcept"]},
        )

        def fake_rank(target_session, core, gap):
            target_session["prerequisite_queue"] = [
                {"name": "PrereqConcept", "status": "active", "priority_score": 0.8}
            ]
            target_session["active_teaching_concept"] = "PrereqConcept"
            target_session["current_target"] = "PrereqConcept"
            target_session["teaching_mode"] = "teach_prerequisite"
            target_session["last_knowledge_gap"] = {"core_concept": core, "text": gap}
            return target_session["prerequisite_queue"]

        with patch.object(remedial, "rank_and_queue_prerequisites", side_effect=fake_rank) as rank:
            event = remedial.prime_from_initial_wrong_answer(
                session,
                question="question",
                user_answer="wrong answer",
                correct_answer="correct answer",
                grading_feedback={"weaknesses": "missing the core rule"},
            )

        self.assertIsNotNone(event)
        self.assertEqual(rank.call_count, 1)
        self.assertEqual(session["concept_states"]["CoreConcept"]["mastery"], "unknown")
        self.assertEqual(session["active_teaching_concept"], "PrereqConcept")

        no_evidence_session = {}
        remedial.freeze_initial_retrieval(
            no_evidence_session,
            "question",
            {"seed_concepts": ["CoreConcept"]},
        )
        self.assertIsNone(
            remedial.prime_from_initial_wrong_answer(
                no_evidence_session,
                question="question",
                user_answer="wrong answer",
                correct_answer="correct answer",
                grading_feedback=None,
            )
        )

    def test_followup_reuses_frozen_seeds_without_global_requery(self):
        initial_result = {
            "seed_concepts": ["CoreConcept", "SecondConcept"],
            "expanded": [
                {
                    "name": "CoreConcept",
                    "definition": "Core definition",
                    "sample_chunks": ["Core complete initial textbook chunk."],
                    "prerequisites": [],
                    "leads_to": [],
                    "parents": [],
                    "subtypes": [],
                },
                {
                    "name": "SecondConcept",
                    "definition": "Second definition",
                    "sample_chunks": ["Second complete initial textbook chunk."],
                    "prerequisites": [],
                    "leads_to": [],
                    "parents": [],
                    "subtypes": [],
                },
            ],
            "prereq_chains": [],
            "descendant_chains": [],
        }

        def profile(name, **_kwargs):
            return {
                "name": name,
                "definition": f"{name} definition",
                "prerequisites": ["PrereqConcept"] if name == "CoreConcept" else [],
                "parents": [],
                "sample_chunk_records": [
                    {"text": f"{name} complete exact-concept chunk.", "chunk_id": name}
                ],
            }

        relations = {
            "prerequisites": [
                {"name": "PrereqConcept", "depth": 1, "strength": 0.9}
            ]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(graphrag_trace, "TRACE_DIR", Path(tmpdir)),
                patch.object(rag_backend, "get_active_backend", return_value="graphrag"),
                patch.object(rag_ai_role, "should_search_database", return_value=True),
                patch.object(rag_ai_role, "save_sessions_to_file"),
                patch.object(
                    rag_ai_role,
                    "call_gemini_api",
                    side_effect=[
                        "Initial diagnostic question.",
                        "Student needs remediation.\n評分：20分",
                        "Prerequisite is now understood.\n評分：95分",
                    ],
                ),
                patch.object(
                    graphrag_client,
                    "query_natural_language",
                    return_value=initial_result,
                ) as global_query,
                patch.object(graphrag_client, "fetch_concept_profile", side_effect=profile),
                patch.object(graphrag_client, "get_concept_relations", return_value=relations),
                patch(
                    "src.graphrag_chunk_reranker._semantic_scores",
                    side_effect=lambda _query, records: (
                        [0.8] * len(records),
                        "test-embedding",
                        None,
                    ),
                ),
                patch("src.tutoring_progress.upsert_tutoring_progress"),
            ):
                first = rag_ai_role.handle_tutoring_conversation(
                    "student@example.com",
                    "Question text",
                    "wrong",
                    "correct",
                )
                second = rag_ai_role.handle_tutoring_conversation(
                    "student@example.com",
                    "Question text",
                    "wrong",
                    "correct",
                    user_input="I do not know.",
                )
                third = rag_ai_role.handle_tutoring_conversation(
                    "student@example.com",
                    "Question text",
                    "wrong",
                    "correct",
                    user_input="I can now explain the prerequisite.",
                )

        self.assertTrue(first["is_initial"])
        self.assertFalse(second["is_initial"])
        self.assertEqual(global_query.call_count, 1)
        self.assertEqual(global_query.call_args.args[0], "Question text")
        self.assertEqual(
            [
                item["name"]
                for item in second["remedial_learning_state"]["initial_retrieval"]["seed_concepts"]
            ],
            ["CoreConcept", "SecondConcept"],
        )
        self.assertEqual(
            second["remedial_learning_state"]["active_teaching_concept"],
            "CoreConcept",
        )
        self.assertEqual(
            second["remedial_learning_state"]["teaching_mode"],
            "diagnose_core",
        )
        self.assertEqual(second["graphrag_usage"]["retrieval_mode"], "stateful_exact_concept")
        self.assertEqual(
            third["graphrag_usage"]["active_teaching_concept"],
            "CoreConcept",
        )
        self.assertEqual(
            third["remedial_learning_state"]["active_teaching_concept"],
            "CoreConcept",
        )
        self.assertEqual(
            third["remedial_learning_state"]["teaching_mode"],
            "diagnose_core",
        )

    def test_initial_wrong_quiz_keeps_seed_chunks_and_starts_with_core(self):
        initial_result = {
            "seed_concepts": ["CoreConcept"],
            "expanded": [
                {
                    "name": "CoreConcept",
                    "definition": "Core definition",
                    "sample_chunks": ["Core initial chunk."],
                    "prerequisites": [],
                    "leads_to": [],
                    "parents": [],
                    "subtypes": [],
                }
            ],
            "prereq_chains": [],
            "descendant_chains": [],
        }

        def profile(name, **_kwargs):
            return {
                "name": name,
                "definition": f"{name} definition",
                "prerequisites": [],
                "parents": [],
                "sample_chunk_records": [
                    {"text": f"{name} COMPLETE EXACT CHUNK", "chunk_id": name}
                ],
            }

        captured_prompts = []

        def generate(prompt):
            captured_prompts.append(prompt)
            return "Prerequisite teaching question."

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(graphrag_trace, "TRACE_DIR", Path(tmpdir)),
                patch.object(rag_backend, "get_active_backend", return_value="graphrag"),
                patch.object(rag_ai_role, "should_search_database", return_value=True),
                patch.object(rag_ai_role, "save_sessions_to_file"),
                patch.object(rag_ai_role, "call_gemini_api", side_effect=generate),
                patch.object(graphrag_client, "query_natural_language", return_value=initial_result),
                patch.object(
                    graphrag_client,
                    "get_concept_relations",
                    return_value={
                        "prerequisites": [
                            {"name": "PrereqConcept", "depth": 1, "strength": 0.9}
                        ]
                    },
                ),
                patch.object(graphrag_client, "fetch_concept_profile", side_effect=profile),
                patch(
                    "src.graphrag_chunk_reranker._semantic_scores",
                    side_effect=lambda _query, records: (
                        [0.8] * len(records),
                        "test-embedding",
                        None,
                    ),
                ),
                patch("src.tutoring_progress.upsert_tutoring_progress"),
            ):
                result = rag_ai_role.handle_tutoring_conversation(
                    "student@example.com",
                    "Question text",
                    "wrong answer",
                    "correct answer",
                    grading_feedback={"weaknesses": "missing prerequisite reasoning"},
                )

        self.assertEqual(len(captured_prompts), 1)
        self.assertIn("Core initial chunk.", captured_prompts[0])
        self.assertNotIn("PrereqConcept COMPLETE EXACT CHUNK", captured_prompts[0])
        self.assertEqual(
            result["graphrag_usage"]["chunk_rerank"]["scoring_mode"],
            "initial_seed_chunks_unfiltered",
        )
        self.assertEqual(
            result["remedial_learning_state"]["active_teaching_concept"],
            "CoreConcept",
        )
        self.assertEqual(
            result["remedial_learning_state"]["teaching_mode"],
            "diagnose_core",
        )

    def test_one_shot_mode_injects_seed_and_d1_prerequisite_then_disables_planner(self):
        initial_result = {
            "seed_concepts": ["CoreConcept"],
            "expanded": [
                {
                    "name": "CoreConcept",
                    "definition": "Core definition",
                    "sample_chunks": ["Core complete chunk."],
                    "prerequisites": ["PrereqConcept"],
                    "leads_to": [],
                    "parents": [],
                    "subtypes": [],
                }
            ],
            "prereq_chains": [
                {
                    "concept": "CoreConcept",
                    "ancestors": [{"name": "PrereqConcept", "distance": 1}],
                }
            ],
            "descendant_chains": [],
        }

        def profile(name, **_kwargs):
            text = (
                "Core complete chunk."
                if name == "CoreConcept"
                else "Prerequisite complete chunk."
            )
            return {
                "name": name,
                "definition": f"{name} definition",
                "prerequisites": [],
                "parents": [],
                "sample_chunk_records": [
                    {
                        "text": text,
                        "chunk_id": f"chunk-{name}",
                        "source": "book",
                        "page_start": 1,
                    }
                ],
            }

        captured_prompts = []

        def generate(prompt):
            captured_prompts.append(prompt)
            if len(captured_prompts) == 1:
                return "Initial answer."
            return "Follow-up answer.\n評分：20分"

        rag_ai_role.GRAPHRAG_TUTORING_MODE = "one_shot"
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(graphrag_trace, "TRACE_DIR", Path(tmpdir)),
                patch.object(rag_backend, "get_active_backend", return_value="graphrag"),
                patch.object(rag_ai_role, "should_search_database", return_value=True),
                patch.object(rag_ai_role, "save_sessions_to_file"),
                patch.object(rag_ai_role, "call_gemini_api", side_effect=generate),
                patch.object(
                    graphrag_client,
                    "query_natural_language",
                    return_value=initial_result,
                ) as global_query,
                patch.object(graphrag_client, "fetch_concept_profile", side_effect=profile),
                patch.object(remedial, "prime_from_initial_wrong_answer") as prime,
                patch.object(remedial, "update_after_scored_answer") as update_state,
                patch.object(rag_ai_role, "update_learning_progress"),
                patch("src.tutoring_progress.upsert_tutoring_progress"),
            ):
                first = rag_ai_role.handle_tutoring_conversation(
                    "student@example.com",
                    "Question text",
                    "wrong",
                    "correct",
                )
                second = rag_ai_role.handle_tutoring_conversation(
                    "student@example.com",
                    "Question text",
                    "wrong",
                    "correct",
                    user_input="I still do not understand.",
                )

        self.assertEqual(global_query.call_count, 1)
        self.assertIn("Core complete chunk.", captured_prompts[0])
        self.assertIn("Prerequisite complete chunk.", captured_prompts[0])
        self.assertIn("候選證據集合", captured_prompts[0])
        self.assertNotIn("Prerequisite complete chunk.", captured_prompts[1])
        self.assertFalse(prime.called)
        self.assertFalse(update_state.called)
        self.assertEqual(first["graphrag_tutoring_mode"], "one_shot")
        self.assertEqual(first["graphrag_usage"]["graph_context_mode"], "prerequisite_d1")
        self.assertEqual(
            first["graphrag_usage"]["injected_prerequisite_concepts"],
            ["PrereqConcept"],
        )
        self.assertEqual(first["remedial_learning_state"], {})
        self.assertEqual(
            second["graphrag_usage"]["retrieval_mode"],
            "one_shot_followup_disabled",
        )
        self.assertEqual(second["remedial_learning_state"], {})

    def test_initial_without_valid_prerequisite_keeps_unfiltered_seed_chunk(self):
        initial_result = {
            "seed_concepts": ["CoreConcept"],
            "expanded": [
                {
                    "name": "CoreConcept",
                    "definition": "Core definition",
                    "sample_chunks": ["Broad discovery chunk."],
                    "prerequisites": [],
                    "leads_to": ["LockedNextConcept"],
                    "parents": [],
                    "subtypes": [],
                }
            ],
            "prereq_chains": [
                {
                    "concept": "CoreConcept",
                    "ancestors": [
                        {"name": "FarAncestor", "distance": 2}
                    ],
                }
            ],
            "descendant_chains": [
                {
                    "concept": "CoreConcept",
                    "descendants": [
                        {"name": "LockedNextConcept", "distance": 1}
                    ],
                }
            ],
        }

        def profile(name, **_kwargs):
            return {
                "name": name,
                "definition": f"{name} definition",
                "prerequisites": [],
                "parents": [],
                "sample_chunk_records": [
                    {"text": f"{name} COMPLETE EXACT CORE CHUNK", "chunk_id": name}
                ],
            }

        captured_prompts = []

        def generate(prompt):
            captured_prompts.append(prompt)
            return "Core diagnostic question."

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(graphrag_trace, "TRACE_DIR", Path(tmpdir)),
                patch.object(rag_backend, "get_active_backend", return_value="graphrag"),
                patch.object(rag_ai_role, "should_search_database", return_value=True),
                patch.object(rag_ai_role, "save_sessions_to_file"),
                patch.object(rag_ai_role, "call_gemini_api", side_effect=generate),
                patch.object(
                    graphrag_client,
                    "query_natural_language",
                    return_value=initial_result,
                ) as global_query,
                patch.object(
                    graphrag_client,
                    "get_concept_relations",
                    return_value={"prerequisites": []},
                ),
                patch.object(graphrag_client, "fetch_concept_profile", side_effect=profile),
                patch(
                    "src.graphrag_chunk_reranker._semantic_scores",
                    side_effect=lambda _query, records: (
                        [0.8] * len(records),
                        "test-embedding",
                        None,
                    ),
                ),
                patch("src.tutoring_progress.upsert_tutoring_progress"),
            ):
                result = rag_ai_role.handle_tutoring_conversation(
                    "student@example.com",
                    "Original question only",
                    "wrong answer",
                    "correct answer",
                    grading_feedback={"weaknesses": "missing core reasoning"},
                )

        self.assertEqual(global_query.call_args.args[0], "Original question only")
        self.assertEqual(len(captured_prompts), 1)
        self.assertIn("Broad discovery chunk.", captured_prompts[0])
        self.assertNotIn("CoreConcept COMPLETE EXACT CORE CHUNK", captured_prompts[0])
        self.assertNotIn("FarAncestor", captured_prompts[0])
        self.assertNotIn("LockedNextConcept", captured_prompts[0])
        self.assertEqual(
            result["graphrag_usage"]["chunk_rerank"]["scoring_mode"],
            "initial_seed_chunks_unfiltered",
        )
        self.assertEqual(
            result["remedial_learning_state"]["teaching_mode"],
            "diagnose_core",
        )

    def test_downstream_recommendation_is_locked_until_score_90(self):
        key = remedial.stable_session_key("student@example.com", "Question text")
        session = {
            "user_email": "student@example.com",
            "question": "Question text",
            "conversation_history": [
                {"role": "assistant", "content": "Initial diagnostic"}
            ],
            "initial_retrieval": {
                "seed_concepts": [{"name": "CoreConcept", "seed_rank": 1}]
            },
            "active_core_concept": "CoreConcept",
            "active_teaching_concept": "CoreConcept",
            "current_target": "CoreConcept",
            "teaching_mode": "diagnose_core",
        }
        remedial.ensure_state(session)
        rag_ai_role.learning_sessions[key] = session

        with (
            patch.object(rag_ai_role, "save_sessions_to_file"),
            patch.object(
                rag_ai_role,
                "_enhance_prompt_with_learning_state",
                side_effect=lambda prompt, *_args: prompt,
            ),
            patch.object(
                rag_ai_role,
                "call_gemini_api",
                side_effect=["Almost mastered.\n評分：89分", "Mastered.\n評分：90分"],
            ),
            patch.object(rag_ai_role, "update_learning_progress"),
            patch.object(
                remedial,
                "update_after_scored_answer",
                return_value={"action": "core_mastered"},
            ),
            patch.object(rag_ai_role, "maybe_recommend_next_concept") as recommend,
            patch("src.tutoring_progress.upsert_tutoring_progress"),
        ):
            rag_ai_role.handle_tutoring_conversation(
                "student@example.com",
                "Question text",
                "wrong",
                "correct",
                user_input="almost",
            )
            self.assertEqual(recommend.call_count, 0)
            rag_ai_role.handle_tutoring_conversation(
                "student@example.com",
                "Question text",
                "wrong",
                "correct",
                user_input="complete",
            )

        self.assertEqual(recommend.call_count, 1)

    def test_activating_unlocked_downstream_synchronizes_stateful_target(self):
        session = {
            "target_transition_pending": True,
            "recommended_next_concept": {"name": "UnlockedNext"},
            "active_core_concept": "OldCore",
            "active_teaching_concept": "OldCore",
            "current_target": "OldCore",
            "teaching_mode": "core_mastered",
            "prerequisite_queue": [{"name": "OldParent", "status": "active"}],
            "prerequisite_candidates": [{"name": "OldParent"}],
            "last_knowledge_gap": {"text": "old gap"},
            "conversation_history": [],
        }

        rag_ai_role._activate_pending_next_concept(session)

        self.assertEqual(session["current_target"], "UnlockedNext")
        self.assertEqual(session["active_core_concept"], "UnlockedNext")
        self.assertEqual(session["active_teaching_concept"], "UnlockedNext")
        self.assertEqual(session["teaching_mode"], "diagnose_core")
        self.assertEqual(session["prerequisite_queue"], [])
        self.assertIsNone(session["last_knowledge_gap"])
        self.assertFalse(session["target_transition_pending"])


if __name__ == "__main__":
    unittest.main()
