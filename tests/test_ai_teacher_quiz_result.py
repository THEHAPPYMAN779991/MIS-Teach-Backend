import unittest
from unittest.mock import patch

from bson import ObjectId

from src import ai_teacher


class FakeCollection:
    def __init__(self, documents=None):
        self.documents = documents or {}

    def find_one(self, query):
        return self.documents.get(str(query.get('_id')))


class FakeDatabase:
    def __init__(self, collections):
        self.collections = collections

    def __getitem__(self, name):
        return self.collections.get(name, FakeCollection())


class FakeMongo:
    def __init__(self, collections):
        self.db = FakeDatabase(collections)


class QuizResultQuestionLookupTests(unittest.TestCase):
    def test_prefers_current_test5_question_bank(self):
        question_id = ObjectId()
        current = {'_id': question_id, 'question_text': 'current'}
        legacy = {'_id': question_id, 'question_text': 'legacy'}
        fake_mongo = FakeMongo({
            'test5': FakeCollection({str(question_id): current}),
            'exam': FakeCollection({str(question_id): legacy}),
        })

        with patch.object(ai_teacher, 'mongo', fake_mongo):
            result = ai_teacher._find_question_document(str(question_id))

        self.assertEqual(result['question_text'], 'current')

    def test_falls_back_to_legacy_exam_question_bank(self):
        question_id = ObjectId()
        legacy = {'_id': question_id, 'question_text': 'legacy'}
        fake_mongo = FakeMongo({
            'test5': FakeCollection(),
            'exam': FakeCollection({str(question_id): legacy}),
        })

        with patch.object(ai_teacher, 'mongo', fake_mongo):
            result = ai_teacher._find_question_document(str(question_id))

        self.assertEqual(result['question_text'], 'legacy')

    def test_normalizes_text_difficulty(self):
        self.assertEqual(ai_teacher._normalize_difficulty('easy'), 1)
        self.assertEqual(ai_teacher._normalize_difficulty('medium'), 2)
        self.assertEqual(ai_teacher._normalize_difficulty('hard'), 3)
        self.assertEqual(ai_teacher._normalize_difficulty('unknown'), 2)


if __name__ == '__main__':
    unittest.main()
