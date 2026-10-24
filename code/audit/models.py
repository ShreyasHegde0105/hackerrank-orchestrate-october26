"""Typed records for the participant dataset.

Rows are parsed once in loader.py into these immutable records; later stages
never touch raw CSV dicts. Timestamps are timezone-aware UTC datetimes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class IdentifierKind(str, Enum):
    EMAIL = "email"
    PHONE = "phone"
    EMAIL_HASH = "email_hash"
    USER_ID = "user_id"


class Provenance(str, Enum):
    CURRENT = "current"        # value in the user_identities.csv snapshot
    HISTORICAL = "historical"  # value replaced according to user_audit_log.csv


@dataclass(frozen=True)
class DeletionRequest:
    case_id: str
    email: str
    requested_at: datetime
    reported_status: str
    reported_at: datetime
    audit_at: datetime
    request_text: str


@dataclass(frozen=True)
class UserRecord:
    """One row of the current identity snapshot."""
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: datetime


@dataclass(frozen=True)
class AuditEvent:
    """One identity-field change: old_value was replaced by new_value at event_time."""
    audit_id: str
    user_id: str
    event_time: datetime
    field_name: str  # email | phone | email_hash
    old_value: str
    new_value: str


@dataclass(frozen=True)
class Activity:
    activity_id: str
    event_time: datetime
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str  # a claim, never proof of state
    details: str


@dataclass(frozen=True)
class DeletionRule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str


@dataclass(frozen=True)
class EvidenceFile:
    file_id: str
    file_path: str  # relative to the dataset directory
    origin: str
    captured_at: datetime

    @property
    def kind(self) -> str:
        """File extension without the dot: sql, json, log, txt, pdf, png."""
        return self.file_path.rsplit(".", 1)[-1].lower()
