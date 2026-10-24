"""JSON evidence: four families, recognised by structure rather than file name.

    bundle      {"snapshots": [{"captured_at", "data_store", "collection", "records": [..]}]}
    collection  {"captured_at", "collection", "records": [..]}
    ledger      {"records": [{"recorded_at", "system", "resource_id", "record_type", "result", ..}]}
    service     {"service_exports": {system: {section: [{"observed_at", "resource_id", ..}]}}}

Times come from the most specific level available: an item's own
recorded_at/observed_at, else the inner snapshot's captured_at, else the
file's. A bundle's outer export time is never pushed onto its inner snapshots.
"""
from __future__ import annotations

from audit.evidence.classify import identity_refs, is_null
from audit.evidence.model import (
    Condition,
    Coverage,
    ExtractionIssue,
    FileReport,
    FileStatus,
    Observation,
    TimeBasis,
)
from audit.evidence.records import observation_from_record
from audit.loader import DatasetError, parse_timestamp
from audit.models import EvidenceFile

# collection name -> owning system, for single-collection files without a data_store
COLLECTION_SYSTEM = {
    "customer_analytics_reports": "analytics_platform",
    "customer_export_jobs": "export_service",
}
LOOKUP_HISTORY = "operational_record_history"  # record_lookup rows, not a system snapshot


class _Ctx:
    def __init__(self, evidence: EvidenceFile):
        self.evidence = evidence
        self.observations: list[Observation] = []
        self.coverages: list[Coverage] = []
        self.issues: list[ExtractionIssue] = []
        self.units = 0

    def issue(self, locator: str, message: str, severity: str = "warning") -> None:
        self.issues.append(ExtractionIssue(self.evidence.file_id, locator, severity, message))

    def time(self, value, locator: str):
        try:
            return parse_timestamp(value, locator)
        except DatasetError as exc:
            self.issue(locator, str(exc))
            return None


def extract_json(evidence: EvidenceFile, data) -> tuple[
        list[Observation], list[Coverage], list[ExtractionIssue], FileReport]:
    ctx = _Ctx(evidence)
    family = _family(data)
    if family == "bundle":
        _bundle(ctx, data)
    elif family == "collection":
        _collection(ctx, data)
    elif family == "ledger":
        _ledger(ctx, data["records"], "records", None, None)
    elif family == "service":
        _service(ctx, data)
    else:
        ctx.issue("$", "unrecognised JSON structure", "error")
    status = FileStatus.FAILED if family is None else \
        FileStatus.PARTIAL if ctx.issues else FileStatus.PARSED
    report = FileReport(evidence.file_id, evidence.kind, f"json/{family}", status,
                        observations=len(ctx.observations), coverages=len(ctx.coverages),
                        units_seen=ctx.units)
    return ctx.observations, ctx.coverages, ctx.issues, report


def _family(data) -> str | None:
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("snapshots"), list):
        return "bundle"
    if isinstance(data.get("service_exports"), dict):
        return "service"
    records = data.get("records")
    if isinstance(records, list):
        if "collection" in data and "captured_at" in data:
            return "collection"
        if all(isinstance(r, dict) and "record_type" in r for r in records):
            return "ledger"
    return None


def _bundle(ctx: _Ctx, data: dict) -> None:
    outer = ctx.time(data.get("exported_at"), "exported_at") if data.get("exported_at") else None
    for i, snapshot in enumerate(data["snapshots"]):
        where = f"snapshots[{i}]"
        when = ctx.time(snapshot.get("captured_at"), f"{where}.captured_at")
        if when is None:
            continue
        if outer and when > outer:
            ctx.issue(where, f"inner snapshot captured {snapshot['captured_at']} after the "
                             f"bundle export {data['exported_at']}")
        store, collection = snapshot.get("data_store"), snapshot.get("collection")
        records = snapshot.get("records")
        if not isinstance(records, list):
            ctx.issue(where, "snapshot has no records list")
            continue
        if collection == LOOKUP_HISTORY:
            _ledger(ctx, records, f"{where}.records", when, store)
            continue
        _snapshot_records(ctx, records, f"{where}.records", when, TimeBasis.INNER_SNAPSHOT,
                          store, collection)


def _collection(ctx: _Ctx, data: dict) -> None:
    when = ctx.time(data["captured_at"], "captured_at")
    if when is None:
        return
    collection = data["collection"]
    system = data.get("data_store") or COLLECTION_SYSTEM.get(collection)
    if system is None:
        ctx.issue("collection", f"unknown owning system for collection {collection}")
    _snapshot_records(ctx, data["records"], "records", when, TimeBasis.INNER_SNAPSHOT,
                      system, collection)


def _snapshot_records(ctx, records, where, when, basis, system, collection) -> None:
    present = set()
    for j, record in enumerate(records):
        ctx.units += 1
        locator = f"{where}[{j}]"
        if not isinstance(record, dict):
            ctx.issue(locator, "record is not an object")
            continue
        observation = observation_from_record(
            ctx.evidence, locator, record, observed_at=when, time_basis=basis,
            system_id=system, collection=collection, extractor="json/snapshot")
        if observation is None:
            ctx.issue(locator, "record has no resource_id or label")
            continue
        ctx.observations.append(observation)
        if observation.resource_id:
            present.add(observation.resource_id)
    if system and collection:
        ctx.coverages.append(Coverage(ctx.evidence.file_id, where.rsplit(".", 1)[0], when,
                                      basis, ctx.evidence.captured_at, system, collection,
                                      None, frozenset(present)))


def _ledger(ctx, records, where, fallback_time, source_store) -> None:
    for j, record in enumerate(records):
        ctx.units += 1
        locator = f"{where}[{j}]"
        stamp = record.get("recorded_at") if isinstance(record, dict) else None
        when = ctx.time(stamp, f"{locator}.recorded_at") if stamp else fallback_time
        if when is None:
            ctx.issue(locator, "lookup record has no usable time")
            continue
        observation = observation_from_record(
            ctx.evidence, locator, record, observed_at=when,
            time_basis=TimeBasis.ITEM if stamp else TimeBasis.INNER_SNAPSHOT,
            extractor="json/record_lookup")
        if observation is None:
            ctx.issue(locator, "lookup record has no resource_id")
            continue
        if source_store:
            observation.attributes["reported_by"] = source_store
        ctx.observations.append(observation)


def _service(ctx: _Ctx, data: dict) -> None:
    for system, sections in data["service_exports"].items():
        for section, items in sections.items():
            for j, item in enumerate(items):
                ctx.units += 1
                locator = f"service_exports.{system}.{section}[{j}]"
                when = ctx.time(item.get("observed_at"), f"{locator}.observed_at")
                if when is None:
                    continue
                if section == "index_queries":
                    _index_query(ctx, item, locator, when, system)
                    continue
                observation = observation_from_record(
                    ctx.evidence, locator, item, observed_at=when, time_basis=TimeBasis.ITEM,
                    system_id=system, collection=section, extractor=f"json/service/{section}")
                if observation is None:
                    ctx.issue(locator, "item has no resource_id")
                    continue
                ctx.observations.append(observation)


def _index_query(ctx, item, locator, when, system) -> None:
    """A search for an identifier: each hit is a present document; a query that
    names a resource but returns no hit shows that resource absent."""
    query_refs = identity_refs({"query": item.get("query")}, "query")
    hits = (item.get("hits") or {}).get("documents") or []
    found = set()
    for k, document in enumerate(hits):
        observation = observation_from_record(
            ctx.evidence, f"{locator}.hits.documents[{k}]", document, observed_at=when,
            time_basis=TimeBasis.ITEM, system_id=system, collection="index_queries",
            extractor="json/service/index_queries", extra_refs=query_refs)
        if observation is not None:
            ctx.observations.append(observation)
            found.add(observation.resource_id)
    resource_id = item.get("resource_id")
    if not is_null(resource_id) and resource_id not in found:
        ctx.observations.append(observation_from_record(
            ctx.evidence, locator, {"resource_id": resource_id, "total_hits": "0"},
            observed_at=when, time_basis=TimeBasis.ITEM, system_id=system,
            collection="index_queries", extractor="json/service/index_queries",
            default_condition=Condition.ABSENT, extra_refs=query_refs))
