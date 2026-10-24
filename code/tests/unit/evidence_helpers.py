"""Small builders shared by the evidence unit tests."""
from datetime import datetime, timezone

from audit.models import EvidenceFile


def ts(day: int, hour: int = 0, minute: int = 0, month: int = 9, second: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, second, tzinfo=timezone.utc)


def evidence(kind: str, captured: datetime | None = None, file_id: str = "evidence_99") -> EvidenceFile:
    return EvidenceFile(file_id, f"evidence/x/file.{kind}", "test_origin", captured or ts(20))


def by_resource(observations, resource_id):
    return [o for o in observations if o.resource_id == resource_id]
