"""Structural validation of an output.csv against the official contract.

This checks form, not correctness: statuses, JSON, step numbering, allowed
operation/state pairs, phase and operation order, sorting, evidence-ID shape,
evidence existence, and that no cited evidence postdates the case's audit_at.
Whether the right records were found is the evaluator's job.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from audit.contract import (
    EVIDENCE_ID_PATTERN,
    OPERATION_PHASE,
    OPERATION_STATE,
    OUTPUT_COLUMNS,
    PHASES,
    STATUSES,
    STEP_FIELDS,
    case_sort_key,
    step_sort_key,
)
from audit.loader import DATASET_DIR, DatasetError, read_csv


@dataclass(frozen=True)
class Issue:
    case_id: str  # "" for file-level issues
    message: str

    def __str__(self) -> str:
        return f"{self.case_id or '<file>'}: {self.message}"


def load_evidence_times(dataset_dir: Path = DATASET_DIR) -> dict[str, str]:
    """Every citable evidence ID mapped to the time it came into existence."""
    times = {}
    for row in read_csv(dataset_dir / "evidence_files.csv", ("file_id", "captured_at")):
        times[row["file_id"]] = row["captured_at"]
    for row in read_csv(dataset_dir / "system_activity.csv", ("activity_id", "event_time")):
        times[row["activity_id"]] = row["event_time"]
    for row in read_csv(dataset_dir / "user_audit_log.csv", ("audit_id", "event_time")):
        times[row["audit_id"]] = row["event_time"]
    return times


def read_output_rows(path: Path | str) -> tuple[list[str], list[dict[str, str]]]:
    """Return (header, rows) of an output CSV without interpreting steps_json."""
    rows = read_csv(path)
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        header = handle.readline().strip().split(",")
    return header, rows


def validate_steps(case_id: str, status: str, steps_json: str,
                   audit_at: str | None = None,
                   evidence_times: dict[str, str] | None = None) -> list[Issue]:
    issues: list[Issue] = []

    def issue(message: str) -> None:
        issues.append(Issue(case_id, message))

    if status not in STATUSES:
        issue(f"deletion_status {status!r} is not one of {STATUSES}")
    try:
        steps = json.loads(steps_json)
    except json.JSONDecodeError as exc:
        issue(f"steps_json is not valid JSON ({exc.msg})")
        return issues
    if not isinstance(steps, list):
        issue("steps_json is not a JSON array")
        return issues

    if status == "NOT_APPLICABLE":
        if steps:
            issue("NOT_APPLICABLE must have steps_json=[]")
        return issues
    if not steps:
        issue(f"{status} case has no steps")
        return issues

    well_formed = []
    for index, step in enumerate(steps, start=1):
        where = f"step {index}"
        if not isinstance(step, dict):
            issue(f"{where} is not a JSON object")
            continue
        keys = set(step)
        if keys != set(STEP_FIELDS):
            missing = sorted(set(STEP_FIELDS) - keys)
            extra = sorted(keys - set(STEP_FIELDS))
            issue(f"{where} has wrong fields (missing={missing}, extra={extra})")
            continue
        if type(step["step_no"]) is not int or step["step_no"] != index:
            issue(f"{where} has step_no {step['step_no']!r}; expected {index}")
        if any(not isinstance(step[f], str) for f in STEP_FIELDS[1:]):
            issue(f"{where} has a non-string text field")
            continue
        if step["phase"] not in PHASES:
            issue(f"{where} has unknown phase {step['phase']!r}")
            continue
        operation = step["operation"]
        if operation not in OPERATION_PHASE:
            issue(f"{where} has unknown operation {operation!r}")
            continue
        if OPERATION_PHASE[operation] != step["phase"]:
            issue(f"{where} puts {operation} in phase {step['phase']}")
            continue
        if step["state"] != OPERATION_STATE[operation]:
            issue(f"{where} {operation} has state {step['state']!r}; "
                  f"expected {OPERATION_STATE[operation]!r}")
        if not step["system_id"] or not step["resource_id"]:
            issue(f"{where} has an empty system_id or resource_id")
        _check_evidence(step, status, where, audit_at, evidence_times, issue)
        well_formed.append(step)

    phases = {s["phase"] for s in well_formed}
    if status == "COMPLETE" and phases - {"VERIFICATION"}:
        issue("COMPLETE case may only contain VERIFICATION steps")
    if status == "INCOMPLETE":
        for required in PHASES:
            if required not in phases:
                issue(f"INCOMPLETE case has no {required} step")

    keys = [step_sort_key(s) for s in well_formed]
    for before, after in zip(keys, keys[1:]):
        if after < before:
            issue("steps are not in phase / operation / system_id / resource_id order")
            break
    if len(set(keys)) != len(keys):
        issue("steps contain a duplicate (phase, operation, system_id, resource_id)")
    return issues


def _check_evidence(step, status, where, audit_at, evidence_times, issue) -> None:
    evidence_id = step["evidence_id"]
    needs_evidence = step["phase"] == "FINDING" or status == "COMPLETE"
    if not needs_evidence:
        if evidence_id:
            issue(f"{where} is a proposed {step['phase'].lower()} step and must use "
                  f"evidence_id=\"\" (got {evidence_id!r})")
        return
    if not evidence_id:
        issue(f"{where} must cite evidence")
        return
    if not EVIDENCE_ID_PATTERN.match(evidence_id):
        issue(f"{where} cites {evidence_id!r}, which is not a file, activity or audit ID")
        return
    if evidence_times is None:
        return
    if evidence_id not in evidence_times:
        issue(f"{where} cites unknown evidence {evidence_id!r}")
    elif audit_at and evidence_times[evidence_id] > audit_at:
        issue(f"{where} cites {evidence_id} from {evidence_times[evidence_id]}, "
              f"after audit_at {audit_at}")


def validate_output_file(output_path: Path | str,
                         requests_path: Path | str | None = None,
                         dataset_dir: Path = DATASET_DIR) -> list[Issue]:
    """Validate a whole output file. With requests_path, also check coverage,
    row order, and that cited evidence existed by each case's audit_at."""
    issues: list[Issue] = []
    try:
        header, rows = read_output_rows(output_path)
    except DatasetError as exc:
        return [Issue("", str(exc))]
    if tuple(header) != OUTPUT_COLUMNS:
        issues.append(Issue("", f"header is {header}; expected {list(OUTPUT_COLUMNS)}"))
        return issues

    audit_at: dict[str, str] = {}
    evidence_times = None
    if requests_path is not None:
        requests = read_csv(requests_path, ("case_id", "audit_at"))
        audit_at = {r["case_id"]: r["audit_at"] for r in requests}
        evidence_times = load_evidence_times(dataset_dir)

    case_ids = [r["case_id"] for r in rows]
    seen = set()
    for case_id in case_ids:
        if case_id in seen:
            issues.append(Issue(case_id, "appears more than once"))
        seen.add(case_id)
    if case_ids != sorted(case_ids, key=case_sort_key):
        issues.append(Issue("", "rows are not sorted by case_id"))
    if requests_path is not None:
        for case_id in sorted(set(audit_at) - seen, key=case_sort_key):
            issues.append(Issue(case_id, "has no output row"))
        for case_id in sorted(seen - set(audit_at), key=case_sort_key):
            issues.append(Issue(case_id, "is not a requested case"))
        if len(rows) != len(audit_at):
            issues.append(Issue("", f"{len(rows)} data rows; expected {len(audit_at)}"))

    for row in rows:
        issues.extend(validate_steps(row["case_id"], row["deletion_status"],
                                     row["steps_json"], audit_at.get(row["case_id"]),
                                     evidence_times))
    return issues
