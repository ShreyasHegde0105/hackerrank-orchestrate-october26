"""SQL snapshot extraction.

Three shapes occur:
  * inventory   - one table per collection plus `snapshot_scope`, which lists the
                  systems the capture covered and when. Only those systems are covered.
  * replicated  - `customer_data_primary` beside one or more replica tables
                  (`customer_data_read_replica`, `..._replica_eu_west`, ...), rows
                  holding the record as JSON with its origin_system and collection.
  * profile/search primaries - `customer_profiles_primary` with a failover replica,
                  or `search_documents_primary`.
Every row records which copy (primary or which replica) it came from, so a row
that survives only in a replica is visible as replication drift.
"""
from __future__ import annotations

import json
import re

from audit.evidence.model import (
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

_HEADER_TIME = re.compile(r"^--\s*Captured at:\s*(\S+)", re.MULTILINE)

INVENTORY_TABLE_SYSTEM = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}
# table -> (system, collection, replica) for the profile/search primary snapshots
DIRECT_TABLES = {
    "customer_profiles_primary": ("account_service", "customer_profiles", "primary"),
    "customer_profiles_failover_replica": ("account_service", "customer_profiles",
                                           "failover_replica"),
    "search_documents_primary": ("search_service", "customer_search_documents", "primary"),
}
_REPLICATED = re.compile(r"^customer_data_(primary|.*replica.*)$")


def replica_name(table: str) -> str:
    """customer_data_primary -> primary; customer_data_replica_eu_west -> replica_eu_west."""
    match = _REPLICATED.match(table)
    return match.group(1) if match else table


def extract_sql(evidence: EvidenceFile, script: str, tables: dict[str, list[dict]]) -> tuple[
        list[Observation], list[Coverage], list[ExtractionIssue], FileReport]:
    observations: list[Observation] = []
    coverages: list[Coverage] = []
    issues: list[ExtractionIssue] = []

    header = _HEADER_TIME.search(script)
    capture = parse_timestamp(header.group(1), "Captured at") if header else evidence.captured_at
    basis = TimeBasis.SQL_CAPTURE if header else TimeBasis.MANIFEST
    if header and capture != evidence.captured_at:
        issues.append(ExtractionIssue(evidence.file_id, "header", "info",
                                      f"header capture {header.group(1)} differs from manifest"))

    scope = {}
    for row in tables.get("snapshot_scope", []):
        try:
            scope[row["origin_system"]] = parse_timestamp(row["captured_at"], "snapshot_scope")
        except DatasetError as exc:
            issues.append(ExtractionIssue(evidence.file_id, "snapshot_scope", "warning", str(exc)))

    units, understood = 0, set()
    for table, rows in sorted(tables.items()):
        if table == "snapshot_scope":
            understood.add(table)
            continue
        if table in INVENTORY_TABLE_SYSTEM:
            understood.add(table)
            system = INVENTORY_TABLE_SYSTEM[table]
            if scope and system not in scope:
                if rows:
                    issues.append(ExtractionIssue(evidence.file_id, f"table {table}", "warning",
                                                  f"{len(rows)} rows for {system}, which the "
                                                  "snapshot scope does not list"))
                continue
            when = scope.get(system, capture)
            present = set()
            for i, row in enumerate(rows):
                units += 1
                obs = observation_from_record(
                    evidence, f"{table}[{i}]", row, observed_at=when,
                    time_basis=TimeBasis.SQL_CAPTURE, system_id=system, collection=table,
                    replica="primary", extractor="sql/inventory")
                if obs:
                    observations.append(obs)
                    present.add(obs.resource_id)
            coverages.append(Coverage(evidence.file_id, f"table {table}", when,
                                      TimeBasis.SQL_CAPTURE, evidence.captured_at, system,
                                      table, "primary", frozenset(present)))
        elif table in DIRECT_TABLES:
            understood.add(table)
            system, collection, replica = DIRECT_TABLES[table]
            present = set()
            for i, row in enumerate(rows):
                units += 1
                # source_resource_id (search -> account) lands in related_resources
                obs = observation_from_record(
                    evidence, f"{table}[{i}]", dict(row), observed_at=capture, time_basis=basis,
                    system_id=system, collection=collection, replica=replica,
                    extractor="sql/primary_replica")
                if obs:
                    observations.append(obs)
                    present.add(obs.resource_id)
            coverages.append(Coverage(evidence.file_id, f"table {table}", capture, basis,
                                      evidence.captured_at, system, collection, replica,
                                      frozenset(present)))
        elif _REPLICATED.match(table):
            understood.add(table)
            replica = replica_name(table)
            present: dict[tuple[str, str], set[str]] = {}
            for i, row in enumerate(rows):
                units += 1
                locator = f"{table}[{i}]"
                try:
                    record = json.loads(row["record_json"])
                except (KeyError, TypeError, json.JSONDecodeError) as exc:
                    issues.append(ExtractionIssue(evidence.file_id, locator, "warning",
                                                  f"unreadable record_json ({exc})"))
                    continue
                record.setdefault("resource_id", row.get("resource_id"))
                obs = observation_from_record(
                    evidence, locator, record, observed_at=capture, time_basis=basis,
                    system_id=row.get("origin_system"), collection=row.get("collection_name"),
                    replica=replica, extractor="sql/replicated")
                if obs:
                    obs.attributes["row_updated_at"] = row.get("updated_at")
                    observations.append(obs)
                    key = (row.get("origin_system"), row.get("collection_name"))
                    present.setdefault(key, set()).add(obs.resource_id)
            # a copy covers each collection that appears anywhere in this snapshot
            for system, collection in _collections_in_file(tables):
                coverages.append(Coverage(
                    evidence.file_id, f"table {table}", capture, basis, evidence.captured_at,
                    system, collection, replica,
                    frozenset(present.get((system, collection), set()))))
    for table in sorted(set(tables) - understood):
        issues.append(ExtractionIssue(evidence.file_id, f"table {table}", "warning",
                                      "unrecognised table; rows not extracted"))
    status = FileStatus.PARTIAL if any(i.severity != "info" for i in issues) else FileStatus.PARSED
    report = FileReport(evidence.file_id, evidence.kind, "sql", status,
                        observations=len(observations), coverages=len(coverages),
                        units_seen=units,
                        details={"tables": sorted(tables), "scope": sorted(scope)})
    return observations, coverages, issues, report


def _collections_in_file(tables) -> set[tuple[str, str]]:
    found = set()
    for table, rows in tables.items():
        if _REPLICATED.match(table):
            for row in rows:
                found.add((row.get("origin_system"), row.get("collection_name")))
    return found
