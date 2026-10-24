# Phase 2 baseline

Recorded 2026-10-24T14:30:05Z on commit `e7e465c` plus uncommitted Phase 1-2 files, Python 3.14.7.

## Comparison with Phase 1

| Check | Phase 1 | Phase 2 |
|---|---|---|
| Full test suite | 50 passed | 124 passed |
| Phase 1 tests run alone | 50 passed | 50 passed |
| Evaluator calibration (gold vs gold) | 100% everywhere, 0 structural issues | identical (`phase2_metrics.json` == `phase1_metrics.json`) |

As in Phase 1, the evaluator run is a calibration of the scorer, not a measure of audit accuracy: the pipeline does not produce predictions until Phase 5. Phase 2 logic is verified by its own unit and integration tests, listed below.

## Test suite: `python -m pytest code/tests -v`

```text
code/tests/integration/test_identity_real_data.py::test_real_dataset_loads_cleanly PASSED
code/tests/integration/test_identity_real_data.py::test_every_request_resolves_to_exactly_one_user[evaluation-518-418-100] PASSED
code/tests/integration/test_identity_real_data.py::test_every_request_resolves_to_exactly_one_user[samples-32-29-3] PASSED
code/tests/integration/test_identity_real_data.py::test_case_29_resolves_through_its_historical_email PASSED
code/tests/integration/test_identity_real_data.py::test_other_historical_sample_emails[case_10-user_10] PASSED
code/tests/integration/test_identity_real_data.py::test_other_historical_sample_emails[case_30-user_30] PASSED
code/tests/integration/test_identity_real_data.py::test_email_hash_is_sha256_of_every_known_email PASSED
code/tests/integration/test_identity_real_data.py::test_people_sharing_a_name_stay_separate PASSED
code/tests/integration/test_identity_real_data.py::test_catalog_links_are_parsed_and_trusted PASSED
code/tests/integration/test_identity_real_data.py::test_resolved_identities_reach_the_sample_answer_records PASSED
code/tests/integration/test_identity_report.py::test_identity_report_runs_and_reports_no_unresolved PASSED
code/tests/integration/test_profile.py::test_profile_runs_and_recognises_every_log_format PASSED
code/tests/unit/test_dataset_loader.py::test_loads_typed_records PASSED
code/tests/unit/test_dataset_loader.py::test_activity_is_sorted_by_time_not_file_order PASSED
code/tests/unit/test_dataset_loader.py::test_citable_time_for_files_activities_and_audit_rows PASSED
code/tests/unit/test_dataset_loader.py::test_malformed_inputs_fail_with_location[user_identities.csv-0-0-None-repeats user_id] PASSED
code/tests/unit/test_dataset_loader.py::test_malformed_inputs_fail_with_location[user_identities.csv-0-5-2026-06-01 00:00-invalid timestamp] PASSED
code/tests/unit/test_dataset_loader.py::test_malformed_inputs_fail_with_location[user_audit_log.csv-0-3-name-unknown field_name 'name'] PASSED
code/tests/unit/test_dataset_loader.py::test_malformed_inputs_fail_with_location[deletion_requests.csv-0-3-DONE-unknown reported_status] PASSED
code/tests/unit/test_dataset_loader.py::test_malformed_inputs_fail_with_location[deletion_requests.csv-0-0--empty case_id] PASSED
code/tests/unit/test_dataset_loader.py::test_malformed_inputs_fail_with_location[evidence_files.csv-0-1-evidence/missing.log-evidence file missing] PASSED
code/tests/unit/test_dataset_loader.py::test_malformed_inputs_fail_with_location[system_activity.csv-0-1-yesterday-invalid timestamp] PASSED
code/tests/unit/test_dataset_loader.py::test_audit_event_for_unknown_user_is_a_warning PASSED
code/tests/unit/test_dataset_loader.py::test_missing_dataset_file_is_reported PASSED
code/tests/unit/test_dataset_loader.py::test_store_reads_sql_json_and_text PASSED
code/tests/unit/test_dataset_loader.py::test_store_refuses_wrong_kinds_and_unknown_ids PASSED
code/tests/unit/test_dataset_loader.py::test_captured_by_filters_on_manifest_time PASSED
code/tests/unit/test_dataset_loader.py::test_invalid_json_is_a_clear_error PASSED
code/tests/unit/test_dataset_loader.py::test_loaded_snapshot_is_read_only PASSED
code/tests/unit/test_dataset_loader.py::test_untrusted_script_cannot_attach_files PASSED
code/tests/unit/test_dataset_loader.py::test_broken_sql_is_a_clear_error PASSED
code/tests/unit/test_evaluator.py::test_perfect_prediction_scores_100 PASSED
code/tests/unit/test_evaluator.py::test_wrong_status_only_hits_status_metrics PASSED
code/tests/unit/test_evaluator.py::test_wrong_evidence_hits_evidence_not_discovery PASSED
code/tests/unit/test_evaluator.py::test_dropped_step_lowers_recall_not_precision PASSED
code/tests/unit/test_evaluator.py::test_reordered_steps_hit_order_not_set_metrics PASSED
code/tests/unit/test_evaluator.py::test_missing_and_malformed_predictions_count_as_wrong PASSED
code/tests/unit/test_evaluator.py::test_cli_runs_and_reports PASSED
code/tests/unit/test_evaluator.py::test_cli_missing_prediction_file_is_a_clear_error PASSED
code/tests/unit/test_identity.py::test_phone_formats_normalise_to_the_same_digits[+1 (202) 555-0101] PASSED
code/tests/unit/test_identity.py::test_phone_formats_normalise_to_the_same_digits[1-202-555-0101] PASSED
code/tests/unit/test_identity.py::test_phone_formats_normalise_to_the_same_digits[001 202 555 0101] PASSED
code/tests/unit/test_identity.py::test_phone_formats_normalise_to_the_same_digits[12025550101] PASSED
code/tests/unit/test_identity.py::test_phone_formats_normalise_to_the_same_digits[ +1.202.555.0101 ] PASSED
code/tests/unit/test_identity.py::test_invalid_phones_are_rejected[] PASSED
code/tests/unit/test_identity.py::test_invalid_phones_are_rejected[None] PASSED
code/tests/unit/test_identity.py::test_invalid_phones_are_rejected[555-01] PASSED
code/tests/unit/test_identity.py::test_invalid_phones_are_rejected[call me] PASSED
code/tests/unit/test_identity.py::test_invalid_phones_are_rejected[+1 202 555 0101 ext 7] PASSED
code/tests/unit/test_identity.py::test_invalid_phones_are_rejected[1234567890123456] PASSED
code/tests/unit/test_identity.py::test_email_normalisation PASSED
code/tests/unit/test_identity.py::test_hash_and_user_id_normalisation PASSED
code/tests/unit/test_identity.py::test_classify_identifier_recognises_kinds_and_rejects_names PASSED
code/tests/unit/test_identity.py::test_exact_current_email PASSED
code/tests/unit/test_identity.py::test_case_and_whitespace_insensitive_email PASSED
code/tests/unit/test_identity.py::test_historical_email PASSED
code/tests/unit/test_identity.py::test_email_known_only_by_its_historical_hash PASSED
code/tests/unit/test_identity.py::test_subject_carries_all_current_and_historical_identifiers PASSED
code/tests/unit/test_identity.py::test_claim_validity_windows PASSED
code/tests/unit/test_identity.py::test_unknown_identity_is_unresolved PASSED
code/tests/unit/test_identity.py::test_missing_or_non_email_input_is_invalid[] PASSED
code/tests/unit/test_identity.py::test_missing_or_non_email_input_is_invalid[None] PASSED
code/tests/unit/test_identity.py::test_missing_or_non_email_input_is_invalid[Sam Lee] PASSED
code/tests/unit/test_identity.py::test_missing_or_non_email_input_is_invalid[user_3] PASSED
code/tests/unit/test_identity.py::test_missing_or_non_email_input_is_invalid[+1 202 555 0103] PASSED
code/tests/unit/test_identity.py::test_reused_email_is_ambiguous_without_a_time PASSED
code/tests/unit/test_identity.py::test_reuse_with_recorded_handover_is_separated_by_request_time PASSED
code/tests/unit/test_identity.py::test_reuse_with_unknown_takeover_date_stays_ambiguous_before_release PASSED
code/tests/unit/test_identity.py::test_simultaneous_owners_stay_ambiguous_even_with_a_time PASSED
code/tests/unit/test_identity.py::test_names_never_resolve_or_own_anything PASSED
code/tests/unit/test_identity.py::test_owners_by_any_identifier_kind_and_time PASSED
code/tests/unit/test_identity.py::test_missing_identifier_field_creates_no_claim_and_no_issue PASSED
code/tests/unit/test_identity.py::test_snapshot_disagreeing_with_audit_trail_keeps_both_values PASSED
code/tests/unit/test_identity.py::test_broken_audit_chain_is_a_conflict PASSED
code/tests/unit/test_identity.py::test_email_and_hash_histories_must_agree PASSED
code/tests/unit/test_identity.py::test_orphan_hash_is_reported_but_still_resolvable PASSED
code/tests/unit/test_identity.py::test_consistent_fixture_users_raise_no_conflicts PASSED
code/tests/unit/test_identity.py::test_audit_event_for_unknown_user_is_ignored_and_reported PASSED
code/tests/unit/test_identity.py::test_unnormalisable_value_is_reported_not_indexed PASSED
code/tests/unit/test_identity.py::test_consistent_link_is_trusted_and_time_filtered PASSED
code/tests/unit/test_identity.py::test_link_whose_email_belongs_to_someone_else_is_untrusted PASSED
code/tests/unit/test_identity.py::test_exclusive_attachment_link_is_detected PASSED
code/tests/unit/test_identity.py::test_unrecognised_link_text_is_reported PASSED
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
============================= 124 passed in 8.09s =============================
```
