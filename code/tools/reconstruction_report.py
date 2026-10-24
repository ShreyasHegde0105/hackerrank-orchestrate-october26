"""Run Phase 4 reconstruction and compare it with the 32 official sample answers.

Usage (from the repository root):  python code/tools/reconstruction_report.py

Writes code/docs/reconstruction_report.md. Sample answers are read here only
to measure the reconstruction; the reconstruction never reads them.

How a sample answer is turned into expected states (per case and record):
  * a FINDING states the record's state at audit_at:
      REPORT_REMAINING_RECORD -> PRESENT, REPORT_EXPOSED_PII -> PRESENT + IDENTIFIABLE,
      REPORT_ACTIVE_DATA_FLOW -> ACTIVE, REPORT_RESTORE_RISK -> RESTORE_ALLOWED;
  * in a COMPLETE case, a VERIFICATION states the proven state at audit_at:
      ABSENT, PRESENT, REDACTED (PRESENT + REFERENCE_ONLY), ANONYMIZED
      (PRESENT + UNLINKED), DISABLED, RESTORE_BLOCKED; its evidence_id is the
      expected citation;
  * in an INCOMPLETE case, a VERIFICATION of a record without a finding is a
    post-remediation check. Its state is compared but marked "verification
    only", because the answer does not say what was observed at audit_at.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from audit.evidence.extract import extract_evidence  # noqa: E402
from audit.evidence.model import Exposure  # noqa: E402
from audit.identity import IdentityIndex  # noqa: E402
from audit.loader import DATASET_DIR, load_dataset  # noqa: E402
from audit.reconstruct import ABSENT, PRESENT, Reconstructor  # noqa: E402
from llm.vision import ScreenshotTranscriber  # noqa: E402

OUT_PATH = CODE_DIR / "docs" / "reconstruction_report.md"

FINDING_STATE = {
    "REPORT_REMAINING_RECORD": (PRESENT, None, None),
    "REPORT_EXPOSED_PII": (PRESENT, Exposure.IDENTIFIABLE, None),
    "REPORT_ACTIVE_DATA_FLOW": (PRESENT, None, "ACTIVE"),
    "REPORT_RESTORE_RISK": (PRESENT, None, "RESTORE_ALLOWED"),
}
VERIFY_STATE = {
    "VERIFY_RECORD_ABSENT": (ABSENT, None, None),
    "VERIFY_RECORD_PRESENT": (PRESENT, None, None),
    "VERIFY_RECORD_REDACTED": (PRESENT, Exposure.REFERENCE_ONLY, None),
    "VERIFY_RECORD_ANONYMIZED": (PRESENT, Exposure.UNLINKED, None),
    "VERIFY_DATA_FLOW_DISABLED": (PRESENT, None, "DISABLED"),
    "VERIFY_RESTORE_BLOCKED": (PRESENT, None, "RESTORE_BLOCKED"),
}


@dataclass
class Expectation:
    case_id: str
    resource_id: str
    state: str
    exposure: Exposure | None
    process: str | None
    citation: str | None
    kind: str  # "finding", "verified (COMPLETE)", "verification only (INCOMPLETE)"


def load_expectations(path: Path = DATASET_DIR / "sample_output.csv") -> list[Expectation]:
    out = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            steps = json.loads(row["steps_json"])
            findings = {s["resource_id"] for s in steps if s["phase"] == "FINDING"}
            for s in steps:
                if s["phase"] == "FINDING":
                    state, exposure, process = FINDING_STATE[s["operation"]]
                    out.append(Expectation(row["case_id"], s["resource_id"], state, exposure,
                                           process, s["evidence_id"], "finding"))
                elif s["phase"] == "VERIFICATION" and s["resource_id"] not in findings:
                    state, exposure, process = VERIFY_STATE[s["operation"]]
                    complete = row["deletion_status"] == "COMPLETE"
                    out.append(Expectation(
                        row["case_id"], s["resource_id"], state, exposure, process,
                        s["evidence_id"] or None,
                        "verified (COMPLETE)" if complete else "verification only (INCOMPLETE)"))
    return out


def why_undiscovered(resource_id, user_id, rc: Reconstructor, audit_at) -> str:
    observations = rc.index.by_resource(resource_id)
    activity = rc.dataset.activity_by_resource.get(resource_id, [])
    if not observations:
        return "activity log only" if activity else "absent from all evidence and activity"
    if all(not o.identity_refs for o in observations):
        return "evidence carries no identifier (e.g. anonymized)"
    owners = {u for o in observations for u in rc.index.owners(o)}
    if user_id not in owners:
        return f"evidence identifiers belong to {sorted(owners) or 'nobody known'}"
    linking = [o for o in observations if user_id in rc.index.owners(o)]
    if all(o.observed_at > audit_at for o in linking):
        return "only evidence dated after audit_at links it to the subject"
    if all(rc.is_anomalous(o) for o in linking if o.observed_at <= audit_at):
        return "linking evidence has an untrustworthy time"
    return "linked evidence exists but was not usable"


def compare(cases, expectations, rc: Reconstructor) -> list[dict]:
    by_case = {c.case_id: c for c in cases}
    rows = []
    for e in expectations:
        case = by_case[e.case_id]
        got = case.resource(e.resource_id)
        row = {"case": e.case_id, "resource": e.resource_id, "kind": e.kind,
               "expected": _fmt(e.state, e.exposure, e.process), "expected_citation": e.citation}
        if got is None:
            row.update(actual="NOT DISCOVERED", citation=None, state_match=False,
                       citation_match=None,
                       explanation=why_undiscovered(e.resource_id, case.user_id, rc, case.audit_at),
                       activity_candidate=any(a.resource_id == e.resource_id
                                              for a in case.activity_candidates))
        else:
            ok = (got.state == e.state and (e.exposure is None or got.exposure is e.exposure)
                  and (e.process is None or got.process_state == e.process))
            row.update(actual=_fmt(got.state, got.exposure if e.exposure else None,
                                   got.process_state if e.process else None),
                       citation=got.citation, state_match=ok,
                       citation_match=(got.citation == e.citation) if e.citation else None,
                       explanation="" if ok else got.reason, activity_candidate=False)
        rows.append(row)
    return rows


def extras(cases, expectations) -> list[tuple]:
    expected = {(e.case_id, e.resource_id) for e in expectations}
    return [(c.case_id, r.resource_id, r.state, r.reason) for c in cases for r in c.resources
            if (c.case_id, r.resource_id) not in expected]


def _fmt(state, exposure, process):
    text = state
    if exposure is not None:
        text += f"+{exposure.value}"
    if process:
        text += f"/{process}"
    return text


def build(requests_file: str):
    dataset = load_dataset(requests_file=requests_file)
    rc = Reconstructor(dataset, IdentityIndex.from_dataset(dataset),
                       extract_evidence(dataset, transcriber=ScreenshotTranscriber.manual()))
    return rc, rc.reconstruct_all()


def main() -> int:
    rc, cases = build("sample_deletion_requests.csv")
    expectations = load_expectations()
    rows = compare(cases, expectations, rc)
    found = [r for r in rows if r["actual"] != "NOT DISCOVERED"]
    lines = ["# Phase 4 reconstruction report", "",
             "Generated by `python code/tools/reconstruction_report.py`. Sample answers are used "
             "only to measure the reconstruction.", "",
             "## 1. Comparison with the 32 official sample answers", ""]
    for kind in ("finding", "verified (COMPLETE)", "verification only (INCOMPLETE)"):
        part = [r for r in rows if r["kind"] == kind]
        disc = [r for r in part if r["actual"] != "NOT DISCOVERED"]
        cited = [r for r in disc if r["citation_match"] is not None]
        lines.append(f"- {kind}: {len(part)} expected records; discovered {len(disc)}; state "
                     f"matches {sum(r['state_match'] for r in disc)}/{len(disc)}; citation matches "
                     f"{sum(bool(r['citation_match']) for r in cited)}/{len(cited)}")
    lines += ["", f"Overall: {len(rows)} expected (case, record) states; {len(found)} discovered; "
              f"{sum(r['state_match'] for r in found)} state matches; "
              f"{sum(bool(r['citation_match']) for r in found if r['citation_match'] is not None)}/"
              f"{sum(1 for r in found if r['citation_match'] is not None)} citations match.", ""]
    undiscovered = Counter(r["explanation"] for r in rows if r["actual"] == "NOT DISCOVERED")
    lines += ["Not discovered, by reason: " + ", ".join(f"{k} = {v}" for k, v in
                                                       sorted(undiscovered.items())),
              f"Of these, listed as an unverified activity candidate: "
              f"{sum(1 for r in rows if r['activity_candidate'])}.", "",
              "| case | record | kind | expected | actual | citation (expected / actual) | match | "
              "explanation |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        match = "yes" if r["state_match"] and r["citation_match"] in (True, None) else "**no**"
        lines.append(f"| {r['case']} | {r['resource']} | {r['kind']} | {r['expected']} | "
                     f"{r['actual']} | {r['expected_citation'] or '-'} / {r['citation'] or '-'} | "
                     f"{match} | {r['explanation'][:160].replace('|', '/')} |")
    lines += ["", "## 2. Records reconstructed but absent from the sample answers", ""]
    for case_id, resource_id, state, reason in extras(cases, expectations):
        lines.append(f"- {case_id} {resource_id}: {state} ({reason[:160]})")
    lines.append("")

    eval_rc, eval_cases = build("deletion_requests.csv")
    states = Counter(r.state for c in eval_cases for r in c.resources)
    lines += ["## 3. Evaluation set (518 cases, no answers available)", "",
              f"- Cases reconstructed: {len(eval_cases)}; identity resolved: "
              f"{sum(c.user_id is not None for c in eval_cases)}",
              f"- Records: {sum(len(c.resources) for c in eval_cases)}; by state: "
              + ", ".join(f"{k}={v}" for k, v in sorted(states.items())),
              f"- Cases with at least one record: {sum(bool(c.resources) for c in eval_cases)}",
              f"- Records without a citable file: "
              f"{sum(1 for c in eval_cases for r in c.resources if r.state in ('PRESENT', 'ABSENT') and not r.citation)}",
              f"- Conflicted relationships: "
              + ", ".join(f"{c.case_id}:{r.resource_id}" for c in eval_cases for r in c.resources
                          if r.relationship == 'CONFLICTED'),
              f"- Subject observations excluded as after audit_at: "
              f"{sum(len(c.excluded_future) for c in eval_cases)}; as time anomalies: "
              f"{sum(len(c.excluded_anomalous) for c in eval_cases)}",
              f"- Cases with a unique activity candidate (unverified): "
              f"{sum(1 for c in eval_cases if any(a.unique_in_window for a in c.activity_candidates))}",
              ""]
    OUT_PATH.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
