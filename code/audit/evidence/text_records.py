"""Plain-text records: shift handovers made of pipe-separated record lookups.

    Snapshot: 2026-10-01T12:00:00Z
    - 2026-10-01T12:00:00Z | account_service | account_146 | USER_PROFILE | NOT_FOUND | e.l@yahoo.com
"""
from __future__ import annotations

import re

from audit.evidence.model import ExtractionIssue, FileReport, FileStatus, Observation, TimeBasis
from audit.evidence.records import observation_from_record
from audit.loader import DatasetError, parse_timestamp
from audit.models import EvidenceFile

_ROW = re.compile(r"^-\s+(?P<ts>\S+)\s*\|\s*(?P<system>[\w]+)\s*\|\s*(?P<resource>\S+)\s*\|"
                  r"\s*(?P<type>[A-Z_]+)\s*\|\s*(?P<result>[A-Z_]+)\s*\|\s*(?P<subject>.*\S)\s*$")


def extract_text(evidence: EvidenceFile, lines: list[str]) -> tuple[
        list[Observation], list[ExtractionIssue], FileReport]:
    observations, issues, units = [], [], 0
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue  # titles, the snapshot header and blank lines carry no records
        units += 1
        match = _ROW.match(stripped)
        if not match:
            issues.append(ExtractionIssue(evidence.file_id, f"line {number}", "warning",
                                          "list line is not a record lookup"))
            continue
        try:
            when = parse_timestamp(match.group("ts"), f"line {number}")
        except DatasetError as exc:
            issues.append(ExtractionIssue(evidence.file_id, f"line {number}", "warning", str(exc)))
            continue
        observation = observation_from_record(
            evidence, f"line {number}",
            {"resource_id": match.group("resource"), "system": match.group("system"),
             "record_type": match.group("type"), "result": match.group("result"),
             "subject_reference": match.group("subject")},
            observed_at=when, time_basis=TimeBasis.ITEM, extractor="text/record_lookup")
        observations.append(observation)
    status = FileStatus.PARTIAL if issues else FileStatus.PARSED
    if units == 0:
        status = FileStatus.FAILED
        issues.append(ExtractionIssue(evidence.file_id, "$", "error", "no record lines found"))
    report = FileReport(evidence.file_id, evidence.kind, "text", status,
                        observations=len(observations), units_seen=units)
    return observations, issues, report
