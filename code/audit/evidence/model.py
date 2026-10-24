"""The common evidence model shared by every extractor.

An Observation is one thing one evidence file says about one record at one
moment: which system and resource, what condition it was in, which
identifiers appeared with it, and exactly where in the file that came from.
A Coverage says a file captured a whole collection at a moment, which is
what lets a missing row count as proof of absence.

Extractors record only what the source states. They never resolve
identifiers to users (identity.py does that later) and never fill a field the
source leaves out: unknown stays None or UNKNOWN.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from audit.models import IdentifierKind


class Condition(str, Enum):
    PRESENT = "PRESENT"                  # the record exists
    ABSENT = "ABSENT"                    # a lookup positively found no record
    ACTIVE = "ACTIVE"                    # a data flow is enabled
    DISABLED = "DISABLED"                # a data flow is disabled
    RESTORE_ALLOWED = "RESTORE_ALLOWED"  # a backup can still be restored
    RESTORE_BLOCKED = "RESTORE_BLOCKED"  # restore of a backup is blocked
    MENTIONED = "MENTIONED"              # referenced, but no state is stated


class Exposure(str, Enum):
    IDENTIFIABLE = "IDENTIFIABLE"      # a name, email, phone or email hash is present
    REFERENCE_ONLY = "REFERENCE_ONLY"  # only a stable internal reference (user_id) remains
    UNLINKED = "UNLINKED"              # identifying fields are present but all empty
    UNKNOWN = "UNKNOWN"                # the source does not show the record's contents


class TimeBasis(str, Enum):
    """Where an observation's time came from, most specific first."""
    ITEM = "item"                      # the item's own observed_at / recorded_at
    INNER_SNAPSHOT = "inner_snapshot"  # captured_at of a snapshot nested in a bundle
    LOG_LINE = "log_line"              # the log line's timestamp
    SQL_CAPTURE = "sql_capture"        # snapshot_scope row or "Captured at" header
    DOCUMENT = "document"              # a capture time printed in a PDF or screenshot
    MANIFEST = "manifest"              # only the manifest's captured_at was available


class Confidence(str, Enum):
    PARSED = "parsed"                                # read deterministically from structured content
    AI_EXTRACTED = "ai_extracted"                    # read from a screenshot by a vision model
    MANUAL_TRANSCRIPTION = "manual_transcription"    # a screenshot transcribed by hand


@dataclass(frozen=True)
class IdentityRef:
    """An identifier that appeared in the evidence alongside a record."""
    kind: IdentifierKind
    value: str   # normalised
    raw: str     # as written in the source
    field: str   # the source field it came from
    basis: str   # "same_record", "trace:<trace_id>", "same_document" or "query"


@dataclass(frozen=True)
class Observation:
    evidence_id: str
    source_kind: str            # sql | json | log | txt | pdf | png
    locator: str                # where in the file, e.g. "line 42" or "snapshots[3].records[0]"
    observed_at: datetime
    time_basis: TimeBasis
    file_captured_at: datetime  # the manifest time: what a citation is dated by
    system_id: str | None
    resource_id: str | None     # None when the source shows a record only by a label
    data_category: str | None
    category_basis: str         # why that category was chosen
    condition: Condition
    exposure: Exposure
    identity_refs: tuple[IdentityRef, ...] = ()
    related_resources: tuple[str, ...] = ()  # other resource IDs the record points to
    labels: tuple[str, ...] = ()             # non-ID references: file names, statement numbers
    replica: str | None = None               # primary, read_replica, failover_replica, ...
    collection: str | None = None
    extractor: str = ""
    confidence: Confidence = Confidence.PARSED
    attributes: dict = field(default_factory=dict, hash=False, compare=True)

    def attr(self, name: str, default=None):
        return self.attributes.get(name, default)

    @property
    def obs_id(self) -> str:
        return f"{self.evidence_id}#{self.locator}"


@dataclass(frozen=True)
class Coverage:
    """A file captured every record of `collection` in `system_id` at observed_at.
    A resource of that collection missing from `resource_ids` was absent then."""
    evidence_id: str
    locator: str
    observed_at: datetime
    time_basis: TimeBasis
    file_captured_at: datetime
    system_id: str
    collection: str
    replica: str | None
    resource_ids: frozenset[str]


@dataclass(frozen=True)
class ExtractionIssue:
    evidence_id: str
    locator: str
    severity: str  # error | warning | info
    message: str

    def __str__(self) -> str:
        return f"{self.severity} {self.evidence_id} {self.locator}: {self.message}"


class FileStatus(str, Enum):
    PARSED = "parsed"          # every meaningful unit was understood
    PARTIAL = "partial"        # parsed, with some units not understood (see issues)
    PENDING_AI = "pending_ai"  # needs a vision call that has not been made
    FAILED = "failed"          # could not be read at all


@dataclass
class FileReport:
    evidence_id: str
    kind: str
    parser: str
    status: FileStatus
    observations: int = 0
    coverages: int = 0
    units_seen: int = 0      # lines, records or rows inspected
    units_skipped: int = 0   # operational noise deliberately ignored (heartbeats, pool stats)
    details: dict = field(default_factory=dict)  # parser-specific facts, e.g. log layouts


@dataclass
class ExtractionResult:
    observations: list[Observation] = field(default_factory=list)
    coverages: list[Coverage] = field(default_factory=list)
    issues: list[ExtractionIssue] = field(default_factory=list)
    files: dict[str, FileReport] = field(default_factory=dict)

    def for_resource(self, resource_id: str) -> list[Observation]:
        return [o for o in self.observations if o.resource_id == resource_id]
