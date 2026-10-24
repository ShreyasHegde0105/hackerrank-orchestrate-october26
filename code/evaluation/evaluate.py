"""Score predicted output against reference answers (by default the 32 samples).

Usage (from the repository root):
    python code/evaluation/evaluate.py --pred <predicted.csv>
        [--gold dataset/sample_output.csv]
        [--requests dataset/sample_deletion_requests.csv]
        [--details] [--json-out <path>]

Metrics (all computed over the cases in the gold file):
- status accuracy: share of cases whose deletion_status matches.
- step precision / recall / F1: a step is the 6 fields other than step_no;
  predicted and gold steps are matched as multisets, micro-averaged.
- step discovery F1: the same, on (phase, system_id, resource_id, operation)
  only, so a wrong evidence_id doesn't hide a correctly found record.
- evidence accuracy: among discovery-matched steps that cite evidence, share
  with the same evidence_id.
- field accuracy: steps aligned by position; each of the 7 fields is compared
  and missing or extra steps count as wrong. Reported overall and per field.
- sequence accuracy: share of cases whose ordered step list is identical.
- order accuracy: among cases whose step multiset matches, share in the same order.
- exact case accuracy: status and steps both identical.
A case missing from the predictions counts as wrong everywhere.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from audit.contract import STEP_FIELDS, case_sort_key  # noqa: E402
from audit.loader import DATASET_DIR, DatasetError, read_csv  # noqa: E402
from audit.validator import validate_output_file  # noqa: E402

STEP_MATCH_FIELDS = tuple(f for f in STEP_FIELDS if f != "step_no")
DISCOVERY_FIELDS = ("phase", "system_id", "resource_id", "operation")


@dataclass
class CaseResult:
    case_id: str
    gold_status: str
    pred_status: str | None
    gold_steps: list[dict]
    pred_steps: list[dict]
    parse_error: str | None = None

    @property
    def status_ok(self) -> bool:
        return self.pred_status == self.gold_status

    @property
    def sequence_ok(self) -> bool:
        return self.parse_error is None and self.pred_steps == self.gold_steps


@dataclass
class Totals:
    cases: int = 0
    status_ok: int = 0
    exact_ok: int = 0
    sequence_ok: int = 0
    same_set: int = 0
    same_set_and_order: int = 0
    step_tp: int = 0
    step_pred: int = 0
    step_gold: int = 0
    disc_tp: int = 0
    disc_pred: int = 0
    disc_gold: int = 0
    evidence_ok: int = 0
    evidence_total: int = 0
    field_ok: Counter = field(default_factory=Counter)
    field_slots: int = 0
    confusion: Counter = field(default_factory=Counter)


def _load_answers(path: Path) -> dict[str, tuple[str, list[dict] | None, str | None]]:
    answers = {}
    for row in read_csv(path, ("case_id", "deletion_status", "steps_json")):
        try:
            steps = json.loads(row["steps_json"])
            if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
                raise ValueError("steps_json is not an array of objects")
            answers[row["case_id"]] = (row["deletion_status"], steps, None)
        except ValueError as exc:  # JSONDecodeError is a ValueError
            answers[row["case_id"]] = (row["deletion_status"], None, str(exc))
    return answers


def _project(steps: list[dict], fields: tuple[str, ...]) -> Counter:
    return Counter(tuple(s.get(f) for f in fields) for s in steps)


def compare(gold_path: Path, pred_path: Path) -> tuple[list[CaseResult], Totals]:
    gold = _load_answers(gold_path)
    pred = _load_answers(pred_path)
    results, totals = [], Totals()
    for case_id in sorted(gold, key=case_sort_key):
        gold_status, gold_steps, gold_error = gold[case_id]
        if gold_error:
            raise DatasetError(f"gold answer for {case_id} is unreadable: {gold_error}")
        pred_status, pred_steps, pred_error = pred.get(case_id, (None, [], "missing row"))
        result = CaseResult(case_id, gold_status, pred_status, gold_steps,
                            pred_steps or [], pred_error)
        results.append(result)
        _accumulate(totals, result)
    return results, totals


def _accumulate(t: Totals, r: CaseResult) -> None:
    t.cases += 1
    t.status_ok += r.status_ok
    t.sequence_ok += r.sequence_ok
    t.exact_ok += r.status_ok and r.sequence_ok
    t.confusion[(r.gold_status, r.pred_status or "<missing>")] += 1

    gold_full, pred_full = _project(r.gold_steps, STEP_MATCH_FIELDS), _project(r.pred_steps, STEP_MATCH_FIELDS)
    t.step_tp += sum((gold_full & pred_full).values())
    t.step_gold += sum(gold_full.values())
    t.step_pred += sum(pred_full.values())
    if gold_full == pred_full and r.parse_error is None:
        t.same_set += 1
        t.same_set_and_order += r.sequence_ok

    gold_disc, pred_disc = _project(r.gold_steps, DISCOVERY_FIELDS), _project(r.pred_steps, DISCOVERY_FIELDS)
    t.disc_tp += sum((gold_disc & pred_disc).values())
    t.disc_gold += sum(gold_disc.values())
    t.disc_pred += sum(pred_disc.values())

    pred_evidence = {tuple(s.get(f) for f in DISCOVERY_FIELDS): s.get("evidence_id")
                     for s in r.pred_steps}
    for step in r.gold_steps:
        if step["evidence_id"]:
            key = tuple(step[f] for f in DISCOVERY_FIELDS)
            if key in pred_evidence:
                t.evidence_total += 1
                t.evidence_ok += pred_evidence[key] == step["evidence_id"]

    slots = max(len(r.gold_steps), len(r.pred_steps))
    t.field_slots += slots
    for gold_step, pred_step in zip(r.gold_steps, r.pred_steps):
        for f in STEP_FIELDS:
            t.field_ok[f] += gold_step.get(f) == pred_step.get(f)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _f1(tp: int, n_pred: int, n_gold: int) -> tuple[float, float, float]:
    precision, recall = _ratio(tp, n_pred), _ratio(tp, n_gold)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def metrics(t: Totals) -> dict:
    precision, recall, f1 = _f1(t.step_tp, t.step_pred, t.step_gold)
    d_precision, d_recall, d_f1 = _f1(t.disc_tp, t.disc_pred, t.disc_gold)
    per_field = {f: _ratio(t.field_ok[f], t.field_slots) for f in STEP_FIELDS}
    return {
        "cases": t.cases,
        "status_accuracy": _ratio(t.status_ok, t.cases),
        "exact_case_accuracy": _ratio(t.exact_ok, t.cases),
        "step_precision": precision,
        "step_recall": recall,
        "step_f1": f1,
        "discovery_precision": d_precision,
        "discovery_recall": d_recall,
        "discovery_f1": d_f1,
        "evidence_accuracy": _ratio(t.evidence_ok, t.evidence_total),
        "field_accuracy": _ratio(sum(t.field_ok.values()), t.field_slots * len(STEP_FIELDS)),
        "field_accuracy_by_field": per_field,
        "sequence_accuracy": _ratio(t.sequence_ok, t.cases),
        "order_accuracy_when_steps_match": _ratio(t.same_set_and_order, t.same_set),
        "status_confusion": {f"{g} -> {p}": n for (g, p), n in sorted(t.confusion.items())},
    }


def format_report(m: dict, results: list[CaseResult], details: bool) -> str:
    pct = lambda x: f"{100 * x:6.2f}%"  # noqa: E731
    lines = [
        f"Cases evaluated            {m['cases']}",
        f"Status accuracy            {pct(m['status_accuracy'])}",
        f"Exact case accuracy        {pct(m['exact_case_accuracy'])}",
        f"Step P / R / F1            {pct(m['step_precision'])} {pct(m['step_recall'])} {pct(m['step_f1'])}",
        f"Discovery P / R / F1       {pct(m['discovery_precision'])} {pct(m['discovery_recall'])} {pct(m['discovery_f1'])}",
        f"Evidence accuracy          {pct(m['evidence_accuracy'])}",
        f"Field accuracy             {pct(m['field_accuracy'])}",
        f"Sequence accuracy          {pct(m['sequence_accuracy'])}",
        f"Order accuracy (same set)  {pct(m['order_accuracy_when_steps_match'])}",
        "Field accuracy by field    " + ", ".join(
            f"{f}={100 * v:.1f}%" for f, v in m["field_accuracy_by_field"].items()),
        "Status confusion (gold -> predicted): " + ", ".join(
            f"{k}: {v}" for k, v in m["status_confusion"].items()),
    ]
    mismatched = [r for r in results if not (r.status_ok and r.sequence_ok)]
    lines.append(f"Mismatched cases           {len(mismatched)}"
                 + (": " + ", ".join(r.case_id for r in mismatched) if mismatched else ""))
    if details:
        for r in mismatched:
            lines.append("")
            lines.append(f"--- {r.case_id}: gold {r.gold_status}, predicted {r.pred_status}"
                         + (f" ({r.parse_error})" if r.parse_error else ""))
            gold_set = _project(r.gold_steps, STEP_MATCH_FIELDS)
            pred_set = _project(r.pred_steps, STEP_MATCH_FIELDS)
            for step in sorted((gold_set - pred_set).elements()):
                lines.append("  missing  " + " | ".join(step))
            for step in sorted((pred_set - gold_set).elements()):
                lines.append("  extra    " + " | ".join(step))
            if gold_set == pred_set and not r.sequence_ok:
                lines.append("  same steps, different order or numbering")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--pred", required=True, type=Path, help="predicted output CSV")
    parser.add_argument("--gold", type=Path, default=DATASET_DIR / "sample_output.csv")
    parser.add_argument("--requests", type=Path,
                        default=DATASET_DIR / "sample_deletion_requests.csv",
                        help="requests behind the gold file, for structural checks")
    parser.add_argument("--details", action="store_true", help="list per-case differences")
    parser.add_argument("--json-out", type=Path, help="also write metrics as JSON")
    args = parser.parse_args(argv)

    try:
        issues = validate_output_file(args.pred, args.requests)
        results, totals = compare(args.gold, args.pred)
    except DatasetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    m = metrics(totals)
    m["structural_issues"] = len(issues)
    print(f"Structural issues          {len(issues)}")
    for issue in issues[:20]:
        print(f"  {issue}")
    if len(issues) > 20:
        print(f"  ... {len(issues) - 20} more")
    print(format_report(m, results, args.details))
    if args.json_out:
        args.json_out.write_text(json.dumps(m, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
