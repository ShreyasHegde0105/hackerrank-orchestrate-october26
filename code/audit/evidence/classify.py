"""Shared field semantics: systems, data categories, exposure and identity refs.

Every extractor hands its records to these functions, so a field such as
`email` or `restore_allowed` means the same thing in SQL, JSON, logs and PDFs.
"""
from __future__ import annotations

import re

from audit.evidence.model import Condition, Exposure, IdentityRef
from audit.identity import classify_identifier
from audit.models import IdentifierKind

RESOURCE_ID = re.compile(
    r"\b(account|search|support|analytics|export|backup|data_flow|audit_record|legal_hold|"
    r"tombstone|marketing|object|cache|email_delivery|telemetry)_\d+\b")
EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
USER_ID_IN_TEXT = re.compile(r"\buser_\d+\b")
PHONE_IN_TEXT = re.compile(r"\+\d[\d ()\-]{6,}\d")

SYSTEM_BY_PREFIX = {
    "account": "account_service",
    "search": "search_service",
    "support": "support_platform",
    "analytics": "analytics_platform",
    "export": "export_service",
    "backup": "backup_service",
    "data_flow": "integration_service",
    "audit_record": "security_service",
    "legal_hold": "legal_service",
    "tombstone": "identity_service",
    "marketing": "marketing_platform",
    "object": "object_storage",
    "cache": "cache_service",
    "email_delivery": "email_delivery_service",
    "telemetry": "telemetry_service",
}

# Category implied by the resource prefix alone: the weakest signal, used last.
CATEGORY_BY_PREFIX = {
    "account": "USER_PROFILE",
    "search": "SEARCH_INDEX_ENTRY",
    "analytics": "ANALYTICS_PROFILE",
    "export": "USER_EXPORT",
    "backup": "BACKUP_ARCHIVE",
    "data_flow": "DATA_FLOW",
    "audit_record": "SECURITY_AUDIT_LOG",
    "legal_hold": "LEGAL_HOLD_RECORD",
    "tombstone": "ACCOUNT_TOMBSTONE",
    "marketing": "MARKETING_PROFILE",
    "cache": "CACHE_ENTRY",
    "email_delivery": "EMAIL_DELIVERY_LOG",
    "telemetry": "PRODUCT_TELEMETRY",
    # support_ and object_ are deliberately absent: they can be messages or attachments
}

# Collections (tables, bundle collections, export sections) and what they hold.
COLLECTION_CATEGORY = {
    "customer_profiles": "USER_PROFILE",
    "customer_profiles_primary": "USER_PROFILE",
    "customer_profiles_failover_replica": "USER_PROFILE",
    "profile_lookups": "USER_PROFILE",
    "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "search_documents_primary": "SEARCH_INDEX_ENTRY",
    "index_queries": "SEARCH_INDEX_ENTRY",
    "customer_analytics_profiles": "ANALYTICS_PROFILE",
    "customer_analytics_reports": "ANALYTICS_PROFILE",
    "profiles": "ANALYTICS_PROFILE",
    "support_conversations": "SUPPORT_MESSAGE",
    "support_records": "SUPPORT_MESSAGE",
    "tickets": "SUPPORT_MESSAGE",
    "ticket_attachments": "SUPPORT_ATTACHMENT",
    "customer_exports": "USER_EXPORT",
    "customer_export_jobs": "USER_EXPORT",
    "exports": "USER_EXPORT",
    "connector_subscriptions": "DATA_FLOW",
    "connector_configurations": "DATA_FLOW",
    "connectors": "DATA_FLOW",
    "backup_restore_entries": "BACKUP_ARCHIVE",
    "backup_catalog": "BACKUP_ARCHIVE",
    "restore_entries": "BACKUP_ARCHIVE",
    "security_audit_records": "SECURITY_AUDIT_LOG",
    "security_audit_events": "SECURITY_AUDIT_LOG",
    "audit_records": "SECURITY_AUDIT_LOG",
    "retained_order_records": "LEGAL_HOLD_RECORD",
    "deleted_customer_references": "ACCOUNT_TOMBSTONE",
}

# Fields whose non-empty value is a direct identifier of a person.
NAME_FIELDS = {"full_name", "name", "display_name", "requester_name"}
DIRECT_VALUE_FIELDS = {
    "email", "phone", "email_hash", "requester_email", "requester_phone", "owner_email",
    "owner_phone", "customer_email", "uploaded_by", "matched_value",
}
REFERENCE_FIELDS = {"user_id", "customer_reference", "subject_reference", "subject_ref"}
FREE_TEXT_FIELDS = {"message_text", "message_excerpt"}
CONTENT_FIELDS = NAME_FIELDS | DIRECT_VALUE_FIELDS | REFERENCE_FIELDS | FREE_TEXT_FIELDS

# Fields that name whom a lookup or process is about, without being record content.
QUERY_FIELDS = {"lookup_key", "match_key", "subject_key", "owner_key", "scan_key",
                "correlation_key", "lookup_value", "query", "subject_selector"}
IDENTITY_FIELDS = (DIRECT_VALUE_FIELDS | REFERENCE_FIELDS | QUERY_FIELDS) - {"phone_hash"}

NULLS = {None, "", "null", "None", "NULL"}


def resource_prefix(resource_id: str | None) -> str | None:
    if not resource_id:
        return None
    match = RESOURCE_ID.fullmatch(resource_id)
    return match.group(1) if match else None


def system_for_resource(resource_id: str | None) -> str | None:
    return SYSTEM_BY_PREFIX.get(resource_prefix(resource_id))


def is_null(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() in NULLS)


def classify_category(resource_id: str | None, collection: str | None,
                      fields: dict, record_type: str | None = None,
                      event: str | None = None) -> tuple[str | None, str]:
    """Decide the data category from the record itself, strongest signal first."""
    if record_type and not is_null(record_type):
        return record_type, f"record_type label {record_type}"
    if "email_opt_in" in fields:
        return "MARKETING_PROFILE", "content: email_opt_in (contact/marketing record)"
    file_name = str(fields.get("file_name") or fields.get("file") or "")
    event = event or ""
    service = str(fields.get("log_service") or "")
    is_support = resource_prefix(resource_id) == "support"
    if (fields.get("storage_class") == "ticket_attachment"
            or event.startswith("attachment") or "thumbnail" in event
            or (is_support and (file_name or service.startswith("attachment")))):
        return "SUPPORT_ATTACHMENT", "content: support file attachment"
    if is_support and any(f in fields for f in FREE_TEXT_FIELDS):
        return "SUPPORT_MESSAGE", "content: support message text"
    if is_support and (event.startswith("message_") or event.startswith("support_ticket_read")):
        return "SUPPORT_MESSAGE", f"event: {event} (support conversation)"
    if collection and collection in COLLECTION_CATEGORY:
        return COLLECTION_CATEGORY[collection], f"collection {collection}"
    prefix = resource_prefix(resource_id)
    if prefix in CATEGORY_BY_PREFIX:
        return CATEGORY_BY_PREFIX[prefix], f"resource prefix {prefix}_"
    return None, "undetermined: no label, content signal, collection or prefix"


def exposure_of(fields: dict) -> Exposure:
    """How identifiable a record is, judged only from content fields it shows."""
    shown = [f for f in fields if f in CONTENT_FIELDS]
    if not shown:
        return Exposure.UNKNOWN
    reference_only = False
    for name in shown:
        value = fields[name]
        if is_null(value):
            continue
        text = str(value)
        if name in NAME_FIELDS:
            return Exposure.IDENTIFIABLE
        if name in FREE_TEXT_FIELDS:
            if EMAIL_IN_TEXT.search(text) or PHONE_IN_TEXT.search(text):
                return Exposure.IDENTIFIABLE
            continue
        found = classify_identifier(text)
        if found and found[0] is IdentifierKind.USER_ID:
            reference_only = True
        elif found or name in DIRECT_VALUE_FIELDS:
            return Exposure.IDENTIFIABLE
    return Exposure.REFERENCE_ONLY if reference_only else Exposure.UNLINKED


def identity_refs(fields: dict, basis: str) -> list[IdentityRef]:
    """Identifiers in identity-bearing fields, plus emails, phones and user IDs
    written inside free text. Names are never turned into refs."""
    refs = []
    for name, value in fields.items():
        if is_null(value) or not isinstance(value, (str, int)):
            continue
        text = str(value)
        if name in IDENTITY_FIELDS:
            found = classify_identifier(text)
            if found:
                refs.append(IdentityRef(found[0], found[1], text, name, basis))
        elif name in FREE_TEXT_FIELDS:
            refs.extend(identity_refs_in_text(text, name, basis))
    return refs


def identity_refs_in_text(text: str, field: str, basis: str) -> list[IdentityRef]:
    refs = []
    for pattern in (EMAIL_IN_TEXT, USER_ID_IN_TEXT, PHONE_IN_TEXT):
        for match in pattern.finditer(text):
            found = classify_identifier(match.group(0))
            if found:
                refs.append(IdentityRef(found[0], found[1], match.group(0), field, basis))
    return refs


def unique_refs(refs) -> tuple[IdentityRef, ...]:
    seen, out = set(), []
    for ref in refs:
        key = (ref.kind, ref.value, ref.field, ref.basis)
        if key not in seen:
            seen.add(key)
            out.append(ref)
    return tuple(out)


def flag(value) -> bool | None:
    """Interpret true/false-like values from any source; None if unknown."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "allowed"}:
            return True
        if lowered in {"false", "0", "no", "denied"}:
            return False
    return None


def state_condition(category: str | None, fields: dict) -> Condition | None:
    """Condition for processes and archives, from the state fields they carry."""
    if "enabled" in fields and flag(fields["enabled"]) is not None:
        return Condition.ACTIVE if flag(fields["enabled"]) else Condition.DISABLED
    if "restore_allowed" in fields and flag(fields["restore_allowed"]) is not None:
        return Condition.RESTORE_ALLOWED if flag(fields["restore_allowed"]) else \
            Condition.RESTORE_BLOCKED
    if fields.get("decision") in ("deny", "denied"):
        return Condition.RESTORE_BLOCKED
    if "deletion_block" in fields and flag(fields["deletion_block"]) is not None:
        blocked = flag(fields["deletion_block"]) or flag(fields.get("eligible")) is False
        return Condition.RESTORE_BLOCKED if blocked else Condition.RESTORE_ALLOWED
    return None


# record_lookup results (ledgers, operational history, handover, two log files)
LOOKUP_RESULTS = {
    "FOUND": (Condition.PRESENT, Exposure.UNKNOWN),
    "NOT_FOUND": (Condition.ABSENT, Exposure.UNKNOWN),
    "IDENTIFIABLE": (Condition.PRESENT, Exposure.IDENTIFIABLE),
    "REDACTED": (Condition.PRESENT, Exposure.REFERENCE_ONLY),
    "ANONYMIZED": (Condition.PRESENT, Exposure.UNLINKED),
    "ACTIVE": (None, Exposure.UNKNOWN),    # depends on category, see lookup_condition
    "DISABLED": (Condition.DISABLED, Exposure.UNKNOWN),
    "BLOCKED": (Condition.RESTORE_BLOCKED, Exposure.UNKNOWN),
}


def lookup_condition(record_type: str, result: str) -> tuple[Condition, Exposure] | None:
    if result not in LOOKUP_RESULTS:
        return None
    condition, exposure = LOOKUP_RESULTS[result]
    if result == "ACTIVE":
        condition = Condition.RESTORE_ALLOWED if record_type == "BACKUP_ARCHIVE" \
            else Condition.ACTIVE
    return condition, exposure
