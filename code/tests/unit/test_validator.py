import json

import pytest

from audit.contract import OPERATION_STATE, case_sort_key, step_sort_key
from audit.validator import validate_output_file, validate_steps
from conftest import SAMPLE_OUTPUT, SAMPLE_REQUESTS, write_output


def _messages(issues):
    return " | ".join(str(i) for i in issues)


def test_reference_sample_output_is_structurally_valid():
    assert validate_output_file(SAMPLE_OUTPUT, SAMPLE_REQUESTS) == []


def test_reference_step_order_matches_contract_sort_key(gold_rows):
    for row in gold_rows:
        steps = json.loads(row["steps_json"])
        assert steps == sorted(steps, key=step_sort_key), row["case_id"]


def test_case_sort_is_numeric_not_lexicographic():
    assert sorted(["case_100", "case_33", "case_9"], key=case_sort_key) == [
        "case_9", "case_33", "case_100"]


def test_every_operation_has_one_state():
    assert OPERATION_STATE["DELETE_RECORD"] == "ABSENT"
    assert OPERATION_STATE["REPORT_RESTORE_RISK"] == "ACTIVE"
    assert len(OPERATION_STATE) == 15


def _step(no, phase, op, state, evidence="", system="account_service", resource="account_1"):
    return {"step_no": no, "phase": phase, "system_id": system, "resource_id": resource,
            "operation": op, "state": state, "evidence_id": evidence}


GOOD_INCOMPLETE = [
    _step(1, "FINDING", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_54"),
    _step(2, "REMEDIATION", "DELETE_RECORD", "ABSENT"),
    _step(3, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT"),
]


def _check(status, steps, **kw):
    return _messages(validate_steps("case_x", status, json.dumps(steps), **kw))


def test_good_incomplete_passes():
    assert _check("INCOMPLETE", GOOD_INCOMPLETE) == ""


@pytest.mark.parametrize("mutate,expected", [
    (lambda s: s[0].update(step_no=2), "step_no 2; expected 1"),
    (lambda s: s[0].update(state="ACTIVE"), "expected 'PRESENT'"),
    (lambda s: s[0].update(operation="DELETE_RECORD"), "puts DELETE_RECORD in phase FINDING"),
    (lambda s: s[0].update(operation="PURGE"), "unknown operation"),
    (lambda s: s[0].update(phase="CLEANUP"), "unknown phase"),
    (lambda s: s[0].update(evidence_id=""), "must cite evidence"),
    (lambda s: s[0].update(evidence_id="file_9"), "not a file, activity or audit ID"),
    (lambda s: s[1].update(evidence_id="evidence_54"), "must use evidence_id"),
    (lambda s: s[2].update(evidence_id="evidence_54"), "must use evidence_id"),
    (lambda s: s[0].pop("state"), "wrong fields"),
    (lambda s: s[0].update(extra="x"), "wrong fields"),
    (lambda s: s.reverse(), "not in phase"),
    (lambda s: s.pop(1), "no REMEDIATION step"),
])
def test_incomplete_mutations_are_caught(mutate, expected):
    steps = json.loads(json.dumps(GOOD_INCOMPLETE))
    mutate(steps)
    for i, step in enumerate(steps, start=1):  # renumber unless numbering is under test
        if "step_no" in step and "step_no 2" not in expected:
            step["step_no"] = i
    assert expected in _check("INCOMPLETE", steps)


def test_within_phase_sorting_is_enforced():
    steps = [_step(1, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_1", "search_service", "s_1"),
             _step(2, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_1", "account_service", "a_1")]
    assert "not in phase" in _check("COMPLETE", steps)


def test_duplicate_step_is_caught():
    steps = [_step(1, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_1"),
             _step(2, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_1")]
    assert "duplicate" in _check("COMPLETE", steps)


def test_complete_rules():
    assert "only contain VERIFICATION" in _check("COMPLETE", GOOD_INCOMPLETE[:1])
    assert "must cite evidence" in _check(
        "COMPLETE", [_step(1, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT")])
    assert "has no steps" in _check("COMPLETE", [])


def test_not_applicable_rules():
    assert _check("NOT_APPLICABLE", []) == ""
    assert "steps_json=[]" in _check("NOT_APPLICABLE", GOOD_INCOMPLETE)


def test_bad_status_and_bad_json():
    assert "not one of" in _check("DONE", GOOD_INCOMPLETE)
    assert "not valid JSON" in _messages(validate_steps("case_x", "COMPLETE", "[{"))
    assert "not a JSON array" in _messages(validate_steps("case_x", "COMPLETE", "{}"))


def test_evidence_after_audit_and_unknown_evidence():
    times = {"evidence_54": "2026-09-02T23:00:00Z"}
    early = _check("INCOMPLETE", GOOD_INCOMPLETE, audit_at="2026-09-01T00:00:00Z",
                   evidence_times=times)
    assert "after audit_at" in early
    assert _check("INCOMPLETE", GOOD_INCOMPLETE, audit_at="2026-09-03T00:00:00Z",
                  evidence_times=times) == ""
    assert "unknown evidence" in _check("INCOMPLETE", GOOD_INCOMPLETE,
                                        audit_at="2026-09-03T00:00:00Z", evidence_times={})


def test_evidence_timestamp_offsets_are_compared_as_instants():
    before = _check("INCOMPLETE", GOOD_INCOMPLETE,
                    audit_at="2026-09-03T00:00:00Z",
                    evidence_times={"evidence_54": "2026-09-03T01:00:00+02:00"})
    after = _check("INCOMPLETE", GOOD_INCOMPLETE,
                   audit_at="2026-09-03T00:00:00Z",
                   evidence_times={"evidence_54": "2026-09-02T23:30:00-01:00"})
    assert "after audit_at" not in before
    assert "after audit_at" in after


def test_file_level_checks(tmp_path, gold_rows):
    rows = list(gold_rows)
    swapped = write_output(tmp_path / "a.csv", [rows[1], rows[0]] + rows[2:])
    assert "not sorted" in _messages(validate_output_file(swapped, SAMPLE_REQUESTS))

    missing = write_output(tmp_path / "b.csv", rows[1:])
    text = _messages(validate_output_file(missing, SAMPLE_REQUESTS))
    assert "case_01: has no output row" in text and "31 data rows; expected 32" in text

    dup = write_output(tmp_path / "c.csv", rows[:1] + rows)
    assert "appears more than once" in _messages(validate_output_file(dup, SAMPLE_REQUESTS))

    header = write_output(tmp_path / "d.csv", [[r["case_id"], r["deletion_status"], r["steps_json"]]
                                              for r in rows],
                          header=("case_id", "status", "steps_json"))
    assert "header is" in _messages(validate_output_file(header, SAMPLE_REQUESTS))

    assert "not found" in _messages(validate_output_file(tmp_path / "none.csv"))
