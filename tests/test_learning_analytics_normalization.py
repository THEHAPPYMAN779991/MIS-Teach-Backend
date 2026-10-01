import unittest

from src.learning_analytics import _normalize_dimension_label


class LearningAnalyticsNormalizationTests(unittest.TestCase):
    def test_list_metadata_becomes_stable_grouping_label(self):
        self.assertEqual(
            _normalize_dimension_label(['作業系統', '核心', '作業系統'], '未知領域'),
            '作業系統、核心',
        )

    def test_dict_and_empty_metadata_are_normalized(self):
        self.assertEqual(
            _normalize_dimension_label({'category': '資料結構'}, '未知領域'),
            '資料結構',
        )
        self.assertEqual(_normalize_dimension_label([], '未知領域'), '未知領域')


if __name__ == '__main__':
    unittest.main()
