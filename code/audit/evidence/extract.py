"""Run the right extractor on every evidence file and collect the results.

One bad file never stops the run: its failure is recorded as a FAILED
FileReport with an error issue. After extraction, cross-file checks flag
timestamp problems and system mismatches; they report, they do not repair.
"""
from __future__ import annotations

from audit.evidence.classify import system_for_resource
from audit.evidence.duplicates import analyse_duplicates
from audit.evidence.json_records import extract_json
from audit.evidence.logs import extract_log
from audit.evidence.model import (
    Confidence,
    ExtractionIssue,
    ExtractionResult,
    FileReport,
    FileStatus,
)
from audit.evidence.pdf_documents import extract_pdf, read_pdf_pages
from audit.evidence.screenshots import observations_from_transcription
from audit.evidence.sql_snapshots import extract_sql
from audit.evidence.text_records import extract_text
from audit.evidence_store import EvidenceStore
from audit.loader import Dataset, DatasetError


def extract_evidence(dataset: Dataset, store: EvidenceStore | None = None,
                     transcriber=None) -> ExtractionResult:
    """`transcriber` is an llm.vision.ScreenshotTranscriber; without one,
    screenshots are reported as PENDING_AI rather than guessed at."""
    store = store or EvidenceStore(dataset)
    result = ExtractionResult()
    for evidence in sorted(dataset.evidence_files.values(), key=lambda f: f.file_id):
        try:
            _extract_one(evidence, store, transcriber, result)
        except Exception as exc:  # isolate per file; the reason is kept, nothing is guessed
            result.issues.append(ExtractionIssue(evidence.file_id, "$", "error",
                                                 f"{type(exc).__name__}: {exc}"))
            result.files[evidence.file_id] = FileReport(evidence.file_id, evidence.kind,
                                                        "none", FileStatus.FAILED)
    _cross_checks(result)
    return result


def _extract_one(evidence, store, transcriber, result: ExtractionResult) -> None:
    kind = evidence.kind
    coverages = []
    if kind == "sql":
        observations, coverages, issues, report = extract_sql(
            evidence, store.text(evidence.file_id), store.sql_tables(evidence.file_id))
    elif kind == "json":
        observations, coverages, issues, report = extract_json(evidence,
                                                               store.json(evidence.file_id))
    elif kind == "log":
        observations, issues, report = extract_log(evidence, store.lines(evidence.file_id))
    elif kind == "txt":
        observations, issues, report = extract_text(evidence, store.lines(evidence.file_id))
    elif kind == "pdf":
        observations, issues, report = extract_pdf(evidence,
                                                   read_pdf_pages(store.path(evidence.file_id)))
    elif kind == "png":
        observations, issues, report = _extract_png(evidence, store, transcriber)
    else:
        raise DatasetError(f"no extractor for .{kind} files")
    result.observations.extend(observations)
    result.coverages.extend(coverages)
    result.issues.extend(issues)
    result.files[evidence.file_id] = report


def _extract_png(evidence, store, transcriber):
    from llm.vision import VisionError, VisionUnavailable
    if transcriber is None:
        return [], [ExtractionIssue(evidence.file_id, "$", "info",
                                    "screenshot not transcribed: no vision transcriber")], \
            FileReport(evidence.file_id, "png", "png/vision", FileStatus.PENDING_AI)
    try:
        transcription, meta = transcriber.transcribe_with_meta(
            store.path(evidence.file_id).read_bytes(), subject=evidence.file_id)
    except VisionUnavailable as exc:
        return [], [ExtractionIssue(evidence.file_id, "$", "info",
                                    f"screenshot not transcribed: {exc}")], \
            FileReport(evidence.file_id, "png", "png/vision", FileStatus.PENDING_AI)
    except VisionError as exc:
        return [], [ExtractionIssue(evidence.file_id, "$", "error", f"vision failed: {exc}")], \
            FileReport(evidence.file_id, "png", "png/vision", FileStatus.FAILED)
    manual = meta.get("method") == "manual"
    confidence = Confidence.MANUAL_TRANSCRIPTION if manual else Confidence.AI_EXTRACTED
    observations, issues = observations_from_transcription(evidence, transcription, confidence)
    # date anomalies on screen are warnings about the data, not parse failures
    status = FileStatus.PARTIAL if any(i.severity == "error" or "not a resource ID" in i.message
                                       for i in issues) else FileStatus.PARSED
    return observations, issues, FileReport(
        evidence.file_id, "png", "png/manual" if manual else "png/vision", status,
        observations=len(observations), units_seen=len(transcription.get("records", [])),
        details={"transcription": meta.get("method"), "transcriber": meta.get("transcriber"),
                 "transcribed_at": meta.get("transcribed_at")})


def _cross_checks(result: ExtractionResult) -> None:
    for group in analyse_duplicates(result.observations).within_file:
        evidence_id, _, locator = group[0].partition("#")
        result.issues.append(ExtractionIssue(
            evidence_id, locator, "warning",
            f"{len(group)} identical observations in one file (kept): {', '.join(group)}"))
    for obs in result.observations:
        if obs.observed_at > obs.file_captured_at:
            result.issues.append(ExtractionIssue(
                obs.evidence_id, obs.locator, "warning",
                f"observed_at {obs.observed_at:%Y-%m-%dT%H:%M:%SZ} is after the file's "
                f"capture {obs.file_captured_at:%Y-%m-%dT%H:%M:%SZ}"))
        implied = system_for_resource(obs.resource_id)
        if implied and obs.system_id and implied != obs.system_id:
            result.issues.append(ExtractionIssue(
                obs.evidence_id, obs.locator, "warning",
                f"{obs.resource_id} is reported in {obs.system_id}, but its ID belongs to "
                f"{implied}"))
