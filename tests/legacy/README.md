# Legacy Test Archive

The Python files in this directory are intentionally retained for historical
research traceability but are not part of the public release test suite run by
`python -m unittest discover -s tests -v`.

- `test_explainable_evaluator.py` requires the excluded research-only
  `tool/grade_triple_compare.py` evaluator.
- `test_rag_evidence_rescore_v2.py` requires the excluded research-only
  `tool/rescore_rag_evidence_v2.py` tool.
- `test_remedial_learning_state.py` verifies the retired stateful
  core/prerequisite planner. The public tutoring contract uses the documented
  one-shot candidate-evidence policy instead.

These tests are not deleted and do not imply that the excluded tools should be
restored to the public package.
