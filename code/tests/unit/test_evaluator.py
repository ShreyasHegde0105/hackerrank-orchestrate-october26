import json

import pytest

import evaluate
from conftest import SAMPLE_OUTPUT, write_output


def _score(tmp_path, rows):
    pred = write_output(tmp_path / "pred.csv", rows)
    results, totals = evaluate.compare(SAMPLE_OUTPUT, pred)
    return evaluate.metrics(totals), results


def _copy(rows):
    return [dict(r) for r in rows]


def _edit_steps(row, fn):
    steps = json.loads(row["steps_json"])
    fn(steps)
    row["steps_json"] = json.dumps(steps)


def test_perfect_prediction_scores_100(tmp_path, gold_rows):
    m, _ = _score(tmp_path, gold_rows)
    for key in ("status_accuracy", "exact_case_accuracy", "step_f1", "discovery_f1",
                "evidence_accuracy", "field_accuracy", "sequence_accuracy"):
        assert m[key] == 1.0, key


def test_wrong_status_only_hits_status_metrics(tmp_path, gold_rows):
    rows = _copy(gold_rows)
    rows[0]["deletion_status"] = "INCOMPLETE"  # case_01 is COMPLETE
    m, results = _score(tmp_path, rows)
    assert m["status_accuracy"] == pytest.approx(31 / 32)
    assert m["step_f1"] == 1.0 and m["sequence_accuracy"] == 1.0
    assert m["status_confusion"]["COMPLETE -> INCOMPLETE"] == 1
    assert not results[0].status_ok


def test_wrong_evidence_hits_evidence_not_discovery(tmp_path, gold_rows):
    rows = _copy(gold_rows)
    _edit_steps(rows[0], lambda s: s[0].update(evidence_id="evidence_01"))
    m, _ = _score(tmp_path, rows)
    assert m["discovery_f1"] == 1.0
    assert m["step_f1"] < 1.0
    assert m["evidence_accuracy"] < 1.0
    assert m["field_accuracy_by_field"]["evidence_id"] < 1.0


def test_dropped_step_lowers_recall_not_precision(tmp_path, gold_rows):
    rows = _copy(gold_rows)
    case_11 = next(r for r in rows if r["case_id"] == "case_11")

    def drop_last(steps):
        steps.pop()
    _edit_steps(case_11, drop_last)
    m, _ = _score(tmp_path, rows)
    assert m["step_precision"] == 1.0
    assert m["step_recall"] < 1.0
    assert m["sequence_accuracy"] == pytest.approx(31 / 32)


def test_reordered_steps_hit_order_not_set_metrics(tmp_path, gold_rows):
    rows = _copy(gold_rows)
    case_11 = next(r for r in rows if r["case_id"] == "case_11")

    def swap(steps):
        steps[0], steps[1] = steps[1], steps[0]
        steps[0]["step_no"], steps[1]["step_no"] = 1, 2
    _edit_steps(case_11, swap)
    m, _ = _score(tmp_path, rows)
    assert m["step_f1"] == 1.0
    assert m["sequence_accuracy"] == pytest.approx(31 / 32)
    assert m["order_accuracy_when_steps_match"] == pytest.approx(31 / 32)
    assert m["field_accuracy"] < 1.0


def test_missing_and_malformed_predictions_count_as_wrong(tmp_path, gold_rows):
    rows = _copy(gold_rows)[1:]       # case_01 missing
    rows[0]["steps_json"] = "[{oops"  # case_02 malformed
    m, results = _score(tmp_path, rows)
    assert m["status_confusion"]["COMPLETE -> <missing>"] == 1
    assert results[1].parse_error is not None and not results[1].sequence_ok
    assert m["sequence_accuracy"] == pytest.approx(30 / 32)


def test_cli_runs_and_reports(tmp_path, gold_rows, capsys):
    pred = write_output(tmp_path / "pred.csv", gold_rows)
    assert evaluate.main(["--pred", str(pred), "--json-out", str(tmp_path / "m.json")]) == 0
    out = capsys.readouterr().out
    assert "Status accuracy            100.00%" in out
    assert json.loads((tmp_path / "m.json").read_text())["structural_issues"] == 0


def test_cli_missing_prediction_file_is_a_clear_error(tmp_path, capsys):
    assert evaluate.main(["--pred", str(tmp_path / "missing.csv")]) == 2
    assert "not found" in capsys.readouterr().err
