"""Turn one structured record (SQL row, JSON object, parsed log line) into an Observation."""
from __future__ import annotations

from datetime import datetime

from audit.evidence.classify import (
    RESOURCE_ID,
    classify_category,
    exposure_of,
    identity_refs,
    is_null,
    lookup_condition,
    state_condition,
    system_for_resource,
    unique_refs,
)
from audit.evidence.model import Condition, Exposure, IdentityRef, Observation, TimeBasis
from audit.models import EvidenceFile

NESTED_CONTENT = ("document", "profile")  # objects whose fields are the record's content
LABEL_FIELDS = ("file_name", "file", "ticket_id", "order_reference", "archive_path",
                "archive_key", "object_key")


def flatten(record: dict) -> dict:
    """Merge nested content objects (search `document`, lookup `profile`) into the record."""
    fields = {k: v for k, v in record.items() if k not in NESTED_CONTENT}
    for key in NESTED_CONTENT:
        nested = record.get(key)
        if isinstance(nested, dict):
            for name, value in nested.items():
                fields.setdefault(name, value)
    return fields


def observation_from_record(evidence: EvidenceFile, locator: str, record: dict, *,
                            observed_at: datetime, time_basis: TimeBasis,
                            system_id: str | None = None, collection: str | None = None,
                            replica: str | None = None, extractor: str,
                            default_condition: Condition = Condition.PRESENT,
                            extra_refs: list[IdentityRef] | None = None,
                            event: str | None = None) -> Observation | None:
    """Build an Observation, or None if the record carries no resource and no label."""
    fields = flatten(record)
    resource_id = fields.get("resource_id")
    resource_id = None if is_null(resource_id) else str(resource_id)
    labels = tuple(str(fields[f]) for f in LABEL_FIELDS if not is_null(fields.get(f)))
    if resource_id is None and not labels:
        return None

    record_type = fields.get("record_type")
    category, basis = classify_category(resource_id, collection, fields, record_type, event)
    stated_system = fields.get("system") if not is_null(fields.get("system")) else None
    system = stated_system or system_id or system_for_resource(resource_id)

    condition, exposure = _condition_and_exposure(fields, category, default_condition)

    related = tuple(sorted({
        match.group(0) for name, value in fields.items()
        if name != "resource_id" and isinstance(value, str)
        for match in RESOURCE_ID.finditer(value)} - {resource_id}))
    attributes = {k: v for k, v in fields.items()
                  if k != "resource_id" and not isinstance(v, (dict, list))}
    refs = identity_refs(fields, "same_record") + list(extra_refs or [])
    return Observation(
        evidence_id=evidence.file_id, source_kind=evidence.kind, locator=locator,
        observed_at=observed_at, time_basis=time_basis,
        file_captured_at=evidence.captured_at, system_id=system, resource_id=resource_id,
        data_category=category, category_basis=basis, condition=condition,
        exposure=exposure, identity_refs=unique_refs(refs), related_resources=related,
        labels=labels, replica=replica, collection=collection, extractor=extractor,
        attributes=attributes)


def _condition_and_exposure(fields: dict, category: str | None,
                            default: Condition) -> tuple[Condition, Exposure]:
    exposure = exposure_of(fields)
    if not is_null(fields.get("record_type")) and not is_null(fields.get("result")):
        mapped = lookup_condition(str(fields["record_type"]), str(fields["result"]))
        if mapped:
            condition, lookup_exposure = mapped
            return condition, lookup_exposure if lookup_exposure is not Exposure.UNKNOWN \
                else exposure
    status = fields.get("status_code", fields.get("http_status"))
    rows = fields.get("rows")
    if str(status) == "404" or str(rows) == "0" or str(fields.get("total_hits")) == "0" \
            or fields.get("result") in ("miss", "NOT_FOUND"):
        return Condition.ABSENT, Exposure.UNKNOWN
    state = state_condition(category, fields)
    if state is not None:
        return state, exposure
    return default, exposure
