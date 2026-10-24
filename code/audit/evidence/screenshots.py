"""Turn a screenshot transcription (llm/vision.py) into Observations.

A transcription (manual or by a vision model) only copies text off the
screen. Everything here is deterministic: IDs are checked against the
resource-ID pattern, identifiers are normalised, and states come from fixed
whole-word rules. Every observation is marked MANUAL_TRANSCRIPTION or
AI_EXTRACTED, so later stages know how the screenshot was read. On-screen
text is treated as data: an instruction-like string is just a value.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from audit.evidence.classify import (
    RESOURCE_ID,
    classify_category,
    identity_refs_in_text,
    system_for_resource,
    unique_refs,
)
from audit.evidence.model import (
    Condition,
    Confidence,
    Exposure,
    ExtractionIssue,
    IdentityRef,
    Observation,
    TimeBasis,
)
from audit.identity import classify_identifier
from audit.models import EvidenceFile, IdentifierKind

_SCREEN_TIME = re.compile(r"(?P<day>\d{1,2}) (?P<mon>[A-Z][a-z]{2}) (?P<year>\d{4})"
                          r"(?: (?P<hh>\d{1,2}):(?P<mm>\d{2})(?::(?P<ss>\d{2}))?)?"
                          r"(?: (?P<tz>UTC))?")
_ISO_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\b")
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
KIND_HINTS = (  # (keyword in the on-screen description, category), first match wins
    ("attachment", "SUPPORT_ATTACHMENT"), ("export", "USER_EXPORT"),
    ("marketing", "MARKETING_PROFILE"), ("audience", "MARKETING_PROFILE"),
    ("retained reference", "ACCOUNT_TOMBSTONE"), ("deleted account", "ACCOUNT_TOMBSTONE"),
    ("customer reference", "ACCOUNT_TOMBSTONE"), ("tombstone", "ACCOUNT_TOMBSTONE"),
    ("backup", "BACKUP_ARCHIVE"), ("archive", "BACKUP_ARCHIVE"), ("connector", "DATA_FLOW"),
    ("analytics", "ANALYTICS_PROFILE"), ("audit", "SECURITY_AUDIT_LOG"),
    ("security event", "SECURITY_AUDIT_LOG"), ("search", "SEARCH_INDEX_ENTRY"),
    ("ticket", "SUPPORT_MESSAGE"), ("customer profile", "USER_PROFILE"),
)
# Whole words only: "on" must not match inside "Continuous" or "connector".
OFF_WORDS = {"disabled", "paused", "stopped", "inactive", "off", "suspended"}
ON_WORDS = {"enabled", "active", "live", "running", "healthy", "continuous", "on"}


def _field_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


def screen_time(text: str | None) -> datetime | None:
    """A full date on screen ('01 Sep 2026 06:00:14 UTC' or ISO), with seconds when
    shown. Times in other zones (e.g. 'JST') and dates without a year are not
    converted: they return None rather than a guess."""
    if not text:
        return None
    iso = _ISO_TIME.search(text)
    if iso:
        return datetime.strptime(iso.group(0), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    match = _SCREEN_TIME.search(text)
    if not match or match.group("mon") not in _MONTHS:
        return None
    rest = text[match.end():].strip()
    if match.group("hh") and not match.group("tz") and rest[:3].isalpha():
        return None  # a time in a named non-UTC zone
    return datetime(int(match.group("year")), _MONTHS[match.group("mon")], int(match.group("day")),
                    int(match.group("hh") or 0), int(match.group("mm") or 0),
                    int(match.group("ss") or 0), tzinfo=timezone.utc)


PAST_EVENT_WORDS = {"created", "update", "updated", "modified", "last", "recorded",
                    "indexed", "refreshed", "executed", "seen", "captured", "delivered"}


def _is_past_event_field(name: str) -> bool:
    """Fields that record something that already happened (so a value after the
    capture time is impossible), as opposed to deadlines like expires or retention_end."""
    return bool(set(name.split("_")) & PAST_EVENT_WORDS)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


STATE_FIELD_WORDS = {"status", "state", "mode", "sync", "enabled"}


def _condition(category: str | None, status: str, fields: dict) -> Condition:
    """State comes only from the status badge and state-like fields (status,
    mode, sync, enabled), never from free text such as notes or messages, so
    instruction-like words elsewhere on screen cannot change it."""
    state_values = [str(v) for k, v in fields.items() if set(k.split("_")) & STATE_FIELD_WORDS]
    words = _words(" ".join([status] + state_values))
    if category == "DATA_FLOW":
        for key, value in fields.items():
            if "enabled" in key and _words(str(value)) & {"yes", "true"}:
                return Condition.ACTIVE
            if "enabled" in key and _words(str(value)) & {"no", "false"}:
                return Condition.DISABLED
        if words & OFF_WORDS:
            return Condition.DISABLED
        if words & ON_WORDS:
            return Condition.ACTIVE
    if category == "BACKUP_ARCHIVE":
        restore = " ".join(str(v) for k, v in fields.items() if "restore" in k).lower().split()
        if any(w in restore for w in ("blocked", "denied", "no", "false")):
            return Condition.RESTORE_BLOCKED
        if any(w in restore for w in ("allowed", "yes", "true", "eligible")):
            return Condition.RESTORE_ALLOWED
    return Condition.PRESENT


def observations_from_transcription(evidence: EvidenceFile, transcription: dict,
                                    confidence: Confidence = Confidence.AI_EXTRACTED) -> tuple[
        list[Observation], list[ExtractionIssue]]:
    """`confidence` records how the transcription was made: AI_EXTRACTED for a
    vision model, MANUAL_TRANSCRIPTION for a hand transcription."""
    issues: list[ExtractionIssue] = []
    extractor = "png/manual" if confidence is Confidence.MANUAL_TRANSCRIPTION else "png/vision"
    shown_time = screen_time(transcription.get("captured_at_text"))
    if shown_time and shown_time > evidence.captured_at:
        issues.append(ExtractionIssue(
            evidence.file_id, "captured_at_text", "warning",
            f"on-screen time {shown_time:%Y-%m-%dT%H:%M:%SZ} is after the manifest capture "
            f"{evidence.captured_at:%Y-%m-%dT%H:%M:%SZ}; the manifest time is used"))
        shown_time = None
    observed_at = shown_time or evidence.captured_at
    basis = TimeBasis.DOCUMENT if shown_time else TimeBasis.MANIFEST
    page = {"page_title": transcription.get("page_title"),
            "application": transcription.get("application"),
            "captured_at_text": transcription.get("captured_at_text")}
    observations = []
    for i, record in enumerate(transcription.get("records", [])):
        locator = f"records[{i}]"
        resource_id = (record.get("resource_id") or "").strip() or None
        labels = [record["label"].strip()] if record.get("label") else []
        if resource_id and not RESOURCE_ID.fullmatch(resource_id):
            issues.append(ExtractionIssue(evidence.file_id, locator, "info",
                                          f"{resource_id!r} is not a resource ID; kept as label"))
            labels.append(resource_id)
            resource_id = None
        refs, named = [], False
        for pair in record.get("identifiers", []):
            value = str(pair.get("value", ""))
            found = classify_identifier(value)
            if found:
                refs.append(IdentityRef(found[0], found[1], value, _field_key(pair["field"]),
                                        "same_record"))
            elif value.strip():
                named = True  # a person's name is on screen: identifying, never matched
        fields = {_field_key(p["field"]): p.get("value") for p in record.get("fields", [])}
        for name, value in fields.items():
            if not _is_past_event_field(name):
                continue  # expiry, retention or expected dates are meant to be in the future
            shown = screen_time(str(value))
            if shown and shown > evidence.captured_at:
                issues.append(ExtractionIssue(
                    evidence.file_id, f"{locator}.{name}", "warning",
                    f"screen shows {name} {shown:%Y-%m-%dT%H:%M:%SZ}, after the manifest "
                    f"capture {evidence.captured_at:%Y-%m-%dT%H:%M:%SZ}"))
        if labels and re.search(r"\.\w{2,4}$", labels[0]):
            fields.setdefault("file_name", labels[0])
        category, category_basis = classify_category(resource_id, None, fields)
        if category_basis.startswith(("resource prefix", "undetermined")):
            hint = (record.get("kind_hint") or "").lower()
            hinted = next((c for word, c in KIND_HINTS if word in hint), None)
            if hinted and (category is None or category_basis.startswith("undetermined")
                           or hinted == category):
                category, category_basis = hinted, f"screen description: {hint}"
        kinds = {ref.kind for ref in refs}
        if named or kinds & {IdentifierKind.EMAIL, IdentifierKind.PHONE, IdentifierKind.EMAIL_HASH}:
            exposure = Exposure.IDENTIFIABLE
        elif IdentifierKind.USER_ID in kinds:
            exposure = Exposure.REFERENCE_ONLY
        else:
            exposure = Exposure.UNKNOWN
        status = record.get("status_text") or ""
        related = sorted({m.group(0) for ref in record.get("related_references", [])
                          for m in RESOURCE_ID.finditer(str(ref))} - {resource_id})
        observations.append(Observation(
            evidence.file_id, evidence.kind, locator, observed_at, basis, evidence.captured_at,
            system_for_resource(resource_id), resource_id, category, category_basis,
            _condition(category, status, fields), exposure, unique_refs(refs), tuple(related),
            tuple(labels), extractor=extractor, confidence=confidence,
            attributes={**page, **fields, "status_text": status or None,
                        "person_name_shown": named,
                        "kind_hint": record.get("kind_hint"),
                        "related_references": record.get("related_references", [])}))
    loose = [ref for value in transcription.get("other_identifiers", [])
             for ref in identity_refs_in_text(str(value), "page", "same_document")]
    if loose:
        observations.append(Observation(
            evidence.file_id, evidence.kind, "page", observed_at, basis, evidence.captured_at,
            None, None, None, "undetermined: identifiers not tied to a record",
            Condition.MENTIONED, Exposure.IDENTIFIABLE, unique_refs(loose),
            extractor=extractor, confidence=confidence, attributes=dict(page)))
    if not observations:
        issues.append(ExtractionIssue(evidence.file_id, "$", "warning",
                                      "transcription contains no records"))
    return observations, issues
