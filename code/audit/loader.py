"""Reading the participant-facing dataset files into typed, indexed records.

`read_csv` is the strict low-level reader; `load_dataset` parses every CSV into
the records in models.py, validates keys, timestamps and enumerations, and
builds the lookup indexes later stages need. Any malformed input raises
DatasetError naming the file and line instead of being skipped silently.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from audit.models import (
    Activity,
    AuditEvent,
    DeletionRequest,
    DeletionRule,
    EvidenceFile,
    UserRecord,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "dataset"

REPORTED_STATUSES = {"COMPLETE", "PARTIALLY_COMPLETE", "NOT_STARTED"}
AUDITED_FIELDS = {"email", "phone", "email_hash"}
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class DatasetError(Exception):
    """An input file is missing or does not have the expected shape."""


def read_csv(path: Path | str, required_columns: tuple[str, ...] = ()) -> list[dict[str, str]]:
    """Read a UTF-8 CSV into dicts, failing loudly on missing files or columns.

    Uses the csv module so quoted fields with commas, quotes and newlines
    (request_text, steps_json) survive intact.
    """
    path = Path(path)
    if not path.is_file():
        raise DatasetError(f"Input file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise DatasetError(f"{path} is empty; expected a header row")
        missing = [c for c in required_columns if c not in reader.fieldnames]
        if missing:
            raise DatasetError(
                f"{path} is missing required column(s) {missing}; found {reader.fieldnames}"
            )
        rows = []
        for line_no, row in enumerate(reader, start=2):
            if None in row:
                raise DatasetError(f"{path}:{line_no} has more fields than the header")
            if any(row[c] is None for c in reader.fieldnames):
                raise DatasetError(f"{path}:{line_no} has fewer fields than the header")
            rows.append(row)
    return rows


def parse_timestamp(value: str, where: str = "") -> datetime:
    """Parse the dataset's only timestamp format, YYYY-MM-DDTHH:MM:SSZ, as UTC."""
    try:
        return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise DatasetError(f"{where}: invalid timestamp {value!r}; "
                           f"expected YYYY-MM-DDTHH:MM:SSZ") from None


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


@dataclass
class Dataset:
    """All participant-facing tables, parsed and indexed."""
    root: Path
    requests: list[DeletionRequest]
    users: dict[str, UserRecord]
    audit_events: list[AuditEvent]        # sorted by (event_time, audit_id)
    activities: list[Activity]            # sorted by (event_time, activity_id)
    rules: dict[str, DeletionRule]        # by data_category
    evidence_files: dict[str, EvidenceFile]
    warnings: list[str] = field(default_factory=list)
    audit_by_user: dict[str, list[AuditEvent]] = field(default_factory=dict)
    activity_by_resource: dict[str, list[Activity]] = field(default_factory=dict)
    activity_by_id: dict[str, Activity] = field(default_factory=dict)
    audit_by_id: dict[str, AuditEvent] = field(default_factory=dict)

    def __post_init__(self) -> None:
        by_user = defaultdict(list)
        for event in self.audit_events:
            by_user[event.user_id].append(event)
        self.audit_by_user = dict(by_user)
        by_resource = defaultdict(list)
        for activity in self.activities:
            by_resource[activity.resource_id].append(activity)
        self.activity_by_resource = dict(by_resource)
        self.activity_by_id = {a.activity_id: a for a in self.activities}
        self.audit_by_id = {e.audit_id: e for e in self.audit_events}

    def evidence_path(self, file_id: str) -> Path:
        return self.root / self.evidence_files[file_id].file_path

    def citable_time(self, evidence_id: str) -> datetime | None:
        """When a citable ID (file, activity or audit row) came into existence."""
        if evidence_id in self.evidence_files:
            return self.evidence_files[evidence_id].captured_at
        if evidence_id in self.activity_by_id:
            return self.activity_by_id[evidence_id].event_time
        if evidence_id in self.audit_by_id:
            return self.audit_by_id[evidence_id].event_time
        return None


def _unique(rows: list, key: str, path: Path) -> None:
    seen = set()
    for line_no, row in enumerate(rows, start=2):
        if row[key] in seen:
            raise DatasetError(f"{path}:{line_no} repeats {key} {row[key]!r}")
        if not row[key]:
            raise DatasetError(f"{path}:{line_no} has an empty {key}")
        seen.add(row[key])


def load_requests(path: Path) -> list[DeletionRequest]:
    rows = read_csv(path, ("case_id", "email", "requested_at", "reported_status",
                           "reported_at", "audit_at", "request_text"))
    _unique(rows, "case_id", path)
    requests = []
    for line_no, r in enumerate(rows, start=2):
        where = f"{path}:{line_no}"
        if r["reported_status"] not in REPORTED_STATUSES:
            raise DatasetError(f"{where}: unknown reported_status {r['reported_status']!r}")
        requests.append(DeletionRequest(
            case_id=r["case_id"],
            email=r["email"],
            requested_at=parse_timestamp(r["requested_at"], where),
            reported_status=r["reported_status"],
            reported_at=parse_timestamp(r["reported_at"], where),
            audit_at=parse_timestamp(r["audit_at"], where),
            request_text=r["request_text"],
        ))
    return requests


def load_dataset(dataset_dir: Path | str = DATASET_DIR,
                 requests_file: str = "deletion_requests.csv") -> Dataset:
    """Load and validate the dataset. `requests_file` selects which requests to
    audit, e.g. "sample_deletion_requests.csv" for development."""
    root = Path(dataset_dir)
    warnings: list[str] = []

    users_path = root / "user_identities.csv"
    user_rows = read_csv(users_path, ("user_id", "name", "email", "phone",
                                      "email_hash", "updated_at"))
    _unique(user_rows, "user_id", users_path)
    users = {
        r["user_id"]: UserRecord(r["user_id"], r["name"], r["email"], r["phone"],
                                 r["email_hash"],
                                 parse_timestamp(r["updated_at"], f"{users_path}:{i}"))
        for i, r in enumerate(user_rows, start=2)
    }

    audit_path = root / "user_audit_log.csv"
    audit_rows = read_csv(audit_path, ("audit_id", "user_id", "event_time", "field_name",
                                       "old_value", "new_value"))
    _unique(audit_rows, "audit_id", audit_path)
    audit_events = []
    for i, r in enumerate(audit_rows, start=2):
        where = f"{audit_path}:{i}"
        if r["field_name"] not in AUDITED_FIELDS:
            raise DatasetError(f"{where}: unknown field_name {r['field_name']!r}")
        if r["user_id"] not in users:
            warnings.append(f"{where}: audit event for unknown user {r['user_id']}")
        audit_events.append(AuditEvent(r["audit_id"], r["user_id"],
                                       parse_timestamp(r["event_time"], where),
                                       r["field_name"], r["old_value"], r["new_value"]))
    audit_events.sort(key=lambda e: (e.event_time, e.audit_id))

    activity_path = root / "system_activity.csv"
    activity_rows = read_csv(activity_path, ("activity_id", "event_time", "event_type",
                                             "source_system", "target_system", "resource_id",
                                             "reported_result", "details"))
    _unique(activity_rows, "activity_id", activity_path)
    activities = [
        Activity(r["activity_id"], parse_timestamp(r["event_time"], f"{activity_path}:{i}"),
                 r["event_type"], r["source_system"], r["target_system"], r["resource_id"],
                 r["reported_result"], r["details"])
        for i, r in enumerate(activity_rows, start=2)
    ]
    # The file is not in time order (10 rows step back in time), so sort explicitly.
    activities.sort(key=lambda a: (a.event_time, a.activity_id))

    rules_path = root / "deletion_rules.csv"
    rule_rows = read_csv(rules_path, ("rule_id", "data_category", "condition", "required_action"))
    _unique(rule_rows, "data_category", rules_path)
    rules = {r["data_category"]: DeletionRule(r["rule_id"], r["data_category"], r["condition"],
                                              r["required_action"]) for r in rule_rows}

    manifest_path = root / "evidence_files.csv"
    manifest_rows = read_csv(manifest_path, ("file_id", "file_path", "origin", "captured_at"))
    _unique(manifest_rows, "file_id", manifest_path)
    evidence_files = {}
    for i, r in enumerate(manifest_rows, start=2):
        evidence = EvidenceFile(r["file_id"], r["file_path"], r["origin"],
                                parse_timestamp(r["captured_at"], f"{manifest_path}:{i}"))
        if not (root / evidence.file_path).is_file():
            raise DatasetError(f"{manifest_path}:{i}: evidence file missing: {evidence.file_path}")
        evidence_files[evidence.file_id] = evidence

    return Dataset(
        root=root,
        requests=load_requests(root / requests_file),
        users=users,
        audit_events=audit_events,
        activities=activities,
        rules=rules,
        evidence_files=evidence_files,
        warnings=warnings,
    )
