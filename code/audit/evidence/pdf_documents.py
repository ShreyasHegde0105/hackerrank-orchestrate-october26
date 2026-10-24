"""PDF documents, read deterministically from their text layer (pdfplumber).

The PDFs are business documents laid out as label/value tables: a delivery
correction form, a customer statement, a compliance journal extract and a
finance register. Nothing is inferred from layout beyond this:
  * every resource ID in the document becomes an observation that the record
    existed at the document's time, carrying the document's identifiers;
  * a document with identifiers but no resource ID becomes one observation
    keyed by its reference labels (statement or order numbers), so it can
    be joined to a resource later through a catalog link;
  * each pipe-separated table row (one entry per row, as in a register) is
    its own observation, so one row's identifiers never attach to another row.
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
    Exposure,
    ExtractionIssue,
    FileReport,
    FileStatus,
    Observation,
    TimeBasis,
)
from audit.models import EvidenceFile, IdentifierKind

LABEL = re.compile(r"\b(?:NST-\d{4}-\d{4}-\d+|ORD-\d+|CS-\d+|CJX-\d+-\d+|FIN-\d+)\b")
FILE_NAME = re.compile(r"\b[\w\-]+\.(?:pdf|zip|png|jpg|csv)\b")
DATED = re.compile(r"\b(?P<what>captured|issued|generated|recorded|received|submitted|"
                   r"retain until)\s+(?P<day>\d{1,2}) (?P<mon>[A-Z][a-z]{2}) (?P<year>\d{4})"
                   r"(?: (?P<hh>\d{2}) (?P<mm>\d{2}) UTC)?", re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
DOCUMENT_TIME_WORDS = ("captured", "issued", "generated")


def read_pdf_pages(path) -> list[str]:
    import pdfplumber  # imported lazily: only PDF extraction needs it
    with pdfplumber.open(path) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def _dates(text: str) -> dict[str, datetime]:
    found = {}
    for match in DATED.finditer(text):
        month = _MONTHS.get(match.group("mon").title())
        if not month:
            continue
        moment = datetime(int(match.group("year")), month, int(match.group("day")),
                          int(match.group("hh") or 0), int(match.group("mm") or 0),
                          tzinfo=timezone.utc)
        found.setdefault(match.group("what").lower(), moment)
    return found


def extract_pdf(evidence: EvidenceFile, pages: list[str]) -> tuple[
        list[Observation], list[ExtractionIssue], FileReport]:
    issues: list[ExtractionIssue] = []
    if not any(page.strip() for page in pages):
        issues.append(ExtractionIssue(evidence.file_id, "$", "error", "PDF has no text layer"))
        return [], issues, FileReport(evidence.file_id, evidence.kind, "pdf", FileStatus.FAILED)

    title = pages[0].strip().splitlines()[0]
    body_lines, row_lines = [], []
    for page_no, page in enumerate(pages, start=1):
        for line_no, line in enumerate(page.splitlines(), start=1):
            (row_lines if line.count("|") >= 2 else body_lines).append(
                (f"page {page_no} line {line_no}", line))
    body = "\n".join(line for _, line in body_lines)
    dates = _dates(body)
    doc_time = next((dates[w] for w in DOCUMENT_TIME_WORDS if w in dates), None)
    if doc_time and doc_time > evidence.captured_at:
        issues.append(ExtractionIssue(evidence.file_id, "$", "warning",
                                      f"document time {doc_time} is after the manifest capture"))
        doc_time = None
    observed_at = doc_time or evidence.captured_at
    basis = TimeBasis.DOCUMENT if doc_time else TimeBasis.MANIFEST
    attributes = {"document_title": title}
    for what, moment in dates.items():
        attributes[what.replace(" ", "_")] = moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    refs = unique_refs(identity_refs_in_text(body, "document_text", "same_document"))
    labels = tuple(sorted(set(LABEL.findall(body)) | set(FILE_NAME.findall(body))))
    exposure = _exposure(refs)
    observations = []

    resources = sorted({m.group(0) for m in RESOURCE_ID.finditer(body)})
    for resource_id in resources:
        fields = {"file_name": next(iter(FILE_NAME.findall(body)), "")} \
            if resource_id.startswith("support_") and FILE_NAME.search(body) else {}
        category, category_basis = classify_category(resource_id, None, fields)
        observations.append(Observation(
            evidence.file_id, evidence.kind, f"document:{resource_id}", observed_at, basis,
            evidence.captured_at, system_for_resource(resource_id), resource_id, category,
            category_basis, Condition.PRESENT, exposure, refs,
            tuple(r for r in resources if r != resource_id), labels, extractor="pdf/document",
            attributes=dict(attributes)))
    if not resources and (refs or labels):
        observations.append(Observation(
            evidence.file_id, evidence.kind, "document", observed_at, basis,
            evidence.captured_at, None, None, None,
            "undetermined: document names no resource; join through its labels",
            Condition.MENTIONED, exposure, refs, (), labels, extractor="pdf/document",
            attributes=dict(attributes)))

    for locator, line in row_lines:
        row_refs = unique_refs(identity_refs_in_text(line, "table_row", "same_record"))
        row_resources = sorted({m.group(0) for m in RESOURCE_ID.finditer(line)})
        row_labels = tuple(sorted(set(LABEL.findall(line))))
        row_attributes = {"document_title": title, "row_text": line.strip()}
        for what, moment in _dates(line).items():
            row_attributes[what.replace(" ", "_")] = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        observations.append(Observation(
            evidence.file_id, evidence.kind, locator, observed_at, basis, evidence.captured_at,
            system_for_resource(row_resources[0]) if row_resources else None,
            row_resources[0] if row_resources else None, None,
            "undetermined: register row", Condition.MENTIONED, _exposure(row_refs), row_refs,
            tuple(row_resources[1:]), row_labels, extractor="pdf/table_row",
            attributes=row_attributes))

    if not observations:
        issues.append(ExtractionIssue(evidence.file_id, "$", "warning",
                                      "document names no resource, identifier or label"))
    status = FileStatus.PARTIAL if issues else FileStatus.PARSED
    report = FileReport(evidence.file_id, evidence.kind, "pdf", status,
                        observations=len(observations),
                        units_seen=len(body_lines) + len(row_lines),
                        details={"title": title, "pages": len(pages)})
    return observations, issues, report


def _exposure(refs) -> Exposure:
    kinds = {ref.kind for ref in refs}
    if kinds & {IdentifierKind.EMAIL, IdentifierKind.PHONE, IdentifierKind.EMAIL_HASH}:
        return Exposure.IDENTIFIABLE
    if IdentifierKind.USER_ID in kinds:
        return Exposure.REFERENCE_ONLY
    return Exposure.UNKNOWN
