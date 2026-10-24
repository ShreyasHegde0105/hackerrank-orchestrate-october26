# Phase 1 baseline

Recorded 2026-10-24T14:19:01Z on commit `e7e465c` plus uncommitted Phase 1 files, Python 3.14.7, pytest 9.1.1.

## What this baseline is, and is not

- **Test suite:** checks the Phase 1 tooling (file reading, contract validator, evaluator, profiler).
- **Evaluator run:** scores `dataset/sample_output.csv` against itself. It is a **calibration check** that the scorer reports 100% and 0 structural issues on the official answers. It is **not** a measure of audit accuracy: no audit logic exists yet, so nothing was predicted. Audit logic is tested by its own unit tests (Phase 2 onward); the evaluator measures end-to-end accuracy once the pipeline produces predictions (Phase 5 onward).

## Test suite: `python -m pytest code/tests -v`

```text
code/tests/integration/test_profile.py::test_profile_runs_and_recognises_every_log_format PASSED
code/tests/unit/test_evaluator.py::test_perfect_prediction_scores_100 PASSED
code/tests/unit/test_evaluator.py::test_wrong_status_only_hits_status_metrics PASSED
code/tests/unit/test_evaluator.py::test_wrong_evidence_hits_evidence_not_discovery PASSED
code/tests/unit/test_evaluator.py::test_dropped_step_lowers_recall_not_precision PASSED
code/tests/unit/test_evaluator.py::test_reordered_steps_hit_order_not_set_metrics PASSED
code/tests/unit/test_evaluator.py::test_missing_and_malformed_predictions_count_as_wrong PASSED
code/tests/unit/test_evaluator.py::test_cli_runs_and_reports PASSED
code/tests/unit/test_evaluator.py::test_cli_missing_prediction_file_is_a_clear_error PASSED
code/tests/unit/test_loader.py::test_dataset_files_load_with_expected_shape[deletion_requests.csv] PASSED
code/tests/unit/test_loader.py::test_dataset_files_load_with_expected_shape[deletion_rules.csv] PASSED
code/tests/unit/test_loader.py::test_dataset_files_load_with_expected_shape[evidence_files.csv] PASSED
code/tests/unit/test_loader.py::test_dataset_files_load_with_expected_shape[sample_deletion_requests.csv] PASSED
code/tests/unit/test_loader.py::test_dataset_files_load_with_expected_shape[sample_output.csv] PASSED
code/tests/unit/test_loader.py::test_dataset_files_load_with_expected_shape[system_activity.csv] PASSED
code/tests/unit/test_loader.py::test_dataset_files_load_with_expected_shape[user_audit_log.csv] PASSED
code/tests/unit/test_loader.py::test_dataset_files_load_with_expected_shape[user_identities.csv] PASSED
code/tests/unit/test_loader.py::test_quoted_request_text_with_commas_survives PASSED
code/tests/unit/test_loader.py::test_every_sample_steps_json_parses_as_array PASSED
code/tests/unit/test_loader.py::test_every_manifest_file_exists PASSED
code/tests/unit/test_loader.py::test_missing_file_raises_clear_error PASSED
code/tests/unit/test_loader.py::test_missing_column_raises_clear_error PASSED
code/tests/unit/test_loader.py::test_empty_file_raises_clear_error PASSED
code/tests/unit/test_loader.py::test_ragged_row_raises_clear_error[1,2,3\n-more fields] PASSED
code/tests/unit/test_loader.py::test_ragged_row_raises_clear_error[1\n-fewer fields] PASSED
code/tests/unit/test_validator.py::test_reference_sample_output_is_structurally_valid PASSED
code/tests/unit/test_validator.py::test_reference_step_order_matches_contract_sort_key PASSED
code/tests/unit/test_validator.py::test_case_sort_is_numeric_not_lexicographic PASSED
code/tests/unit/test_validator.py::test_every_operation_has_one_state PASSED
code/tests/unit/test_validator.py::test_good_incomplete_passes PASSED
code/tests/unit/test_validator.py::test_incomplete_mutations_are_caught[<lambda>-step_no 2; expected 1] PASSED
code/tests/unit/test_validator.py::test_incomplete_mutations_are_caught[<lambda>-expected 'PRESENT'] PASSED
code/tests/unit/test_validator.py::test_incomplete_mutations_are_caught[<lambda>-puts DELETE_RECORD in phase FINDING] PASSED
code/tests/unit/test_validator.py::test_incomplete_mutations_are_caught[<lambda>-unknown operation] PASSED
code/tests/unit/test_validator.py::test_incomplete_mutations_are_caught[<lambda>-unknown phase] PASSED
code/tests/unit/test_validator.py::test_incomplete_mutations_are_caught[<lambda>-must cite evidence] PASSED
code/tests/unit/test_validator.py::test_incomplete_mutations_are_caught[<lambda>-not a file, activity or audit ID] PASSED
code/tests/unit/test_validator.py::test_incomplete_mutations_are_caught[<lambda>-must use evidence_id0] PASSED
code/tests/unit/test_validator.py::test_incomplete_mutations_are_caught[<lambda>-must use evidence_id1] PASSED
code/tests/unit/test_validator.py::test_incomplete_mutations_are_caught[<lambda>-wrong fields0] PASSED
code/tests/unit/test_validator.py::test_incomplete_mutations_are_caught[<lambda>-wrong fields1] PASSED
code/tests/unit/test_validator.py::test_incomplete_mutations_are_caught[<lambda>-not in phase] PASSED
code/tests/unit/test_validator.py::test_incomplete_mutations_are_caught[<lambda>-no REMEDIATION step] PASSED
code/tests/unit/test_validator.py::test_within_phase_sorting_is_enforced PASSED
code/tests/unit/test_validator.py::test_duplicate_step_is_caught PASSED
code/tests/unit/test_validator.py::test_complete_rules PASSED
code/tests/unit/test_validator.py::test_not_applicable_rules PASSED
code/tests/unit/test_validator.py::test_bad_status_and_bad_json PASSED
code/tests/unit/test_validator.py::test_evidence_after_audit_and_unknown_evidence PASSED
code/tests/unit/test_validator.py::test_file_level_checks PASSED
============================= 50 passed in 2.53s ==============================
```

## Evaluator calibration: `python code/evaluation/main.py --pred dataset/sample_output.csv`

```text
Structural issues          0
Cases evaluated            32
Status accuracy            100.00%
Exact case accuracy        100.00%
Step P / R / F1            100.00% 100.00% 100.00%
Discovery P / R / F1       100.00% 100.00% 100.00%
Evidence accuracy          100.00%
Field accuracy             100.00%
Sequence accuracy          100.00%
Order accuracy (same set)  100.00%
Field accuracy by field    step_no=100.0%, phase=100.0%, system_id=100.0%, resource_id=100.0%, operation=100.0%, state=100.0%, evidence_id=100.0%
Status confusion (gold -> predicted): COMPLETE -> COMPLETE: 10, INCOMPLETE -> INCOMPLETE: 22
Mismatched cases           0
```

Machine-readable metrics: `phase1_metrics.json`.
