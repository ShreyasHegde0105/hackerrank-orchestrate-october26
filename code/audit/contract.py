"""The output contract from problem_statement.md, as data.

Every other module (planner, serializer, validator, evaluator) takes its
statuses, phases, operations, states and ordering from here, so the rules
exist in exactly one place.
"""
from __future__ import annotations

import re

OUTPUT_COLUMNS = ("case_id", "deletion_status", "steps_json")

STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")

PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")

STEP_FIELDS = (
    "step_no",
    "phase",
    "system_id",
    "resource_id",
    "operation",
    "state",
    "evidence_id",
)

# Operation -> required state, listed per phase in the official table order.
# The list order is the required within-phase step order.
OPERATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "FINDING": (
        ("REPORT_REMAINING_RECORD", "PRESENT"),
        ("REPORT_EXPOSED_PII", "PRESENT"),
        ("REPORT_ACTIVE_DATA_FLOW", "ACTIVE"),
        ("REPORT_RESTORE_RISK", "ACTIVE"),
    ),
    "REMEDIATION": (
        ("DISABLE_DATA_FLOW", "DISABLED"),
        ("BLOCK_RESTORE", "BLOCKED"),
        ("DELETE_RECORD", "ABSENT"),
        ("REDACT_RECORD", "REDACTED"),
        ("ANONYMIZE_RECORD", "ANONYMIZED"),
    ),
    "VERIFICATION": (
        ("VERIFY_RECORD_ABSENT", "ABSENT"),
        ("VERIFY_RECORD_PRESENT", "PRESENT"),
        ("VERIFY_RECORD_REDACTED", "REDACTED"),
        ("VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
        ("VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
        ("VERIFY_RESTORE_BLOCKED", "BLOCKED"),
    ),
}

OPERATION_PHASE = {op: phase for phase, ops in OPERATIONS.items() for op, _ in ops}
OPERATION_STATE = {op: state for ops in OPERATIONS.values() for op, state in ops}
OPERATION_RANK = {op: i for ops in OPERATIONS.values() for i, (op, _) in enumerate(ops)}
PHASE_RANK = {phase: i for i, phase in enumerate(PHASES)}

# Evidence IDs a step may cite: a manifest file, an activity row or an audit-log row.
EVIDENCE_ID_PATTERN = re.compile(r"^(evidence|activity|audit)_\d+$")

_CASE_NUMBER = re.compile(r"^case_(\d+)$")


def step_sort_key(step: dict) -> tuple:
    """Official order: phase, operation table order, then system_id, resource_id."""
    return (
        PHASE_RANK[step["phase"]],
        OPERATION_RANK[step["operation"]],
        step["system_id"],
        step["resource_id"],
    )


def case_sort_key(case_id: str) -> tuple:
    """Numeric case order (case_33 < case_100), matching deletion_requests.csv.

    The IDs are not zero-padded, so a plain string sort would disagree with the
    input file order. Unexpected IDs sort after numbered ones, by string.
    """
    match = _CASE_NUMBER.match(case_id)
    return (0, int(match.group(1)), "") if match else (1, 0, case_id)
