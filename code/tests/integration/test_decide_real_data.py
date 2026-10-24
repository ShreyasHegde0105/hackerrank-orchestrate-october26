"""Phase 5 end to end on the real dataset: output contract, evaluator results, determinism."""
import csv
import importlib.util
import json
from pathlib import Path

import pytest

import evaluate
from audit.decide import PolicyOptions

# code/main.py shares its module name with code/evaluation/main.py, so load it by path
_spec = importlib.util.spec_from_file_location(
    "audit_entry_main", Path(__file__).resolve().parents[2] / "main.py")
main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(main)
from audit.loader import DATASET_DIR
from audit.validator import validate_output_file

from audit.decide import APPROVED

RECOMMENDED = APPROVED
NON_DELETION = {f"case_{n}" for n in range(533, 543)}
EMPTY_COMPLETE = {"case_35", "case_50", "case_55", "case_61", "case_63", "case_416", "case_417"}


def write(decisions, path):
    main.write_output(decisions, path)
    return path


@pytest.fixture(scope="module")
def evaluation_output(tmp_path_factory):
    _, decisions = main.run("deletion_requests.csv", APPROVED)
    return decisions, write(decisions, tmp_path_factory.mktemp("out") / "output.csv")


def score(options, tmp_path):
    _, decisions = main.run("sample_deletion_requests.csv", options)
    path = write(decisions, tmp_path / "sample.csv")
    _, totals = evaluate.compare(DATASET_DIR / "sample_output.csv", path)
    return evaluate.metrics(totals), decisions


def test_output_covers_every_request_in_contract_form(evaluation_output):
    decisions, path = evaluation_output
    with path.open(encoding="utf-8") as handle:
        header = handle.readline().strip()
        rows = list(csv.DictReader([header + "\n"] + handle.readlines()))
    assert header == "case_id,deletion_status,steps_json" and len(rows) == 518
    issues = validate_output_file(path, DATASET_DIR / "deletion_requests.csv")
    # the only contract complaints are COMPLETE rows with nothing evidence-backed (policy P2)
    assert {i.message for i in issues} <= {"COMPLETE case has no steps"}
    empty = {i.case_id for i in issues}
    assert empty == {d.case_id for d in decisions if d.status == "COMPLETE" and not d.steps}
    assert empty == EMPTY_COMPLETE  # P2: nothing evidence-backed exists for these (see report)
    assert {r["case_id"] for r in rows if r["deletion_status"] == "NOT_APPLICABLE"} == NON_DELETION
    assert all(r["steps_json"] == "[]" for r in rows if r["case_id"] in NON_DELETION)


def test_every_step_has_a_rule_and_traceable_evidence(evaluation_output):
    decisions, _ = evaluation_output
    from audit.loader import load_dataset
    dataset = load_dataset()
    audit_at = {r.case_id: r.audit_at for r in dataset.requests}
    for d in decisions:
        decided = {x.resource_id for x in d.decisions}
        for step in d.steps:
            assert step["resource_id"] in decided
            if step["evidence_id"]:
                captured = dataset.evidence_files[step["evidence_id"]].captured_at
                assert captured <= audit_at[d.case_id], (d.case_id, step)


def test_unresolved_relationships_never_produce_steps(evaluation_output):
    decisions, _ = evaluation_output
    case_63 = next(d for d in decisions if d.case_id == "case_63")
    assert all(s["resource_id"] != "marketing_001" for s in case_63.steps)
    assert any("marketing_001" in x and "CONFLICT" in x for x in case_63.diagnostics)


def test_activity_candidates_are_used_only_with_snapshot_proof(evaluation_output):
    decisions, _ = evaluation_output
    from audit.loader import load_dataset
    files = load_dataset().evidence_files
    used = [(d, x) for d in decisions for x in d.decisions if "via activity candidate" in x.rule]
    assert len(used) == 10
    for d, x in used:
        assert x.verification == "VERIFY_RECORD_ABSENT" and not x.finding
        assert x.verification_evidence and files[x.verification_evidence].kind == "sql"
        assert "the only deletion_worker account deletion" in x.rule


def test_conservative_sample_score(tmp_path):
    m, _ = score(PolicyOptions(), tmp_path)
    assert m["status_accuracy"] == pytest.approx(31 / 32)
    assert m["evidence_accuracy"] == 1.0 and m["step_precision"] > 0.99


def test_recommended_sample_score(tmp_path):
    m, decisions = score(RECOMMENDED, tmp_path)
    assert m["status_accuracy"] == 1.0 and m["evidence_accuracy"] == 1.0
    assert m["step_precision"] > 0.99 and m["step_recall"] > 0.89
    extra = [(d.case_id, s["resource_id"]) for d in decisions for s in d.steps
             if s["resource_id"] == "account_6523"]
    assert extra == [("case_17", "account_6523")]  # the one known lookup-only extra


def test_output_is_byte_identical_across_runs(evaluation_output, tmp_path):
    _, first = evaluation_output
    _, decisions = main.run("deletion_requests.csv", APPROVED)
    assert write(decisions, tmp_path / "again.csv").read_bytes() == first.read_bytes()


def test_p3_applies_only_to_contact_index_rows(tmp_path):
    _, decisions = main.run("sample_deletion_requests.csv", APPROVED)
    case_10 = next(d for d in decisions if d.case_id == "case_10")
    assert any(x.resource_id == "support_002" and x.rule == "rule_02" for x in case_10.decisions)


def test_cli_defaults_to_the_approved_policy(tmp_path, capsys):
    out = tmp_path / "o.csv"
    assert main.main(["--requests", "sample_deletion_requests.csv", "--out", str(out),
                      "--diagnostics", str(tmp_path / "d.json")]) == 0
    printed = capsys.readouterr().out
    assert "wrote 32 rows" in printed and "COMPLETE=10, INCOMPLETE=22" in printed
    diagnostics = json.loads((tmp_path / "d.json").read_text(encoding="utf-8"))
    assert diagnostics["case_10"]["status"] == "INCOMPLETE"
    assert main.main(["--requests", "sample_deletion_requests.csv", "--out", str(out),
                      "--no-activity-candidates", "--uncategorised-support-category", "none"]) == 0
    assert "COMPLETE=11, INCOMPLETE=21" in capsys.readouterr().out
