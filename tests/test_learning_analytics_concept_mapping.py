import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import src.learning_analytics as analytics


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class LearningAnalyticsConceptMappingTests(unittest.TestCase):
    def test_attempt_snapshot_is_used_without_mongo_question(self):
        mapping = {
            "schema_version": "graphrag-question-concept/v1",
            "status": "mapped",
            "primary_concept": "二元樹",
            "concept_names": ["二元樹", "樹"],
            "concepts": [
                {"name": "二元樹", "category": "資料結構", "role": "primary"}
            ],
        }
        row = SimpleNamespace(
            answer_id=1,
            question_id="missing-question",
            attempt_time=SimpleNamespace(isoformat=lambda: "2026-07-02T00:00:00"),
            time_spent=12,
            is_correct=True,
            feedback=json.dumps({"concept_mapping": mapping}, ensure_ascii=False),
        )

        with patch.object(
            analytics.sqldb.session, "execute", return_value=FakeResult([row])
        ), patch.object(analytics, "_find_question_document", return_value={}):
            records = analytics.get_student_quiz_records("student@example.com")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["micro_concept_id"], "二元樹")
        self.assertEqual(records[0]["concept_names"], ["二元樹", "樹"])
        self.assertEqual(records[0]["domain_name"], "資料結構")
        self.assertEqual(records[0]["concept_mapping_status"], "mapped")

    def test_legacy_field_is_canonicalized(self):
        with patch.object(
            analytics, "_canonicalize_legacy_concept", return_value="二元搜尋樹"
        ):
            snapshot = analytics._extract_concept_snapshot(
                {}, {"micro_concepts": ["BST基礎", "BST"]}
            )

        self.assertEqual(snapshot["status"], "legacy_canonicalized")
        self.assertEqual(snapshot["primary_concept"], "二元搜尋樹")


if __name__ == "__main__":
    unittest.main()
