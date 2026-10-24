"""Run evidence extraction and write code/docs/evidence_report.md.

Usage (from the repository root):
    python code/tools/evidence_report.py [--vision]

By default screenshots are read from the manual transcriptions
(code/llm/manual_transcriptions/, stored in the cache by
tools/store_manual_transcriptions.py). --vision uses the vision model
instead (needs the anthropic package and credentials).

The report covers per-file parser coverage, observation counts, extraction
issues, identity-linking checks (identifiers that resolve to nobody, or to
more than one user in one observation) and the linkage of sample-answer
records. Sample answers are used here only to measure coverage; extraction
never reads them.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from audit.evidence.extract import extract_evidence  # noqa: E402
from audit.evidence.model import FileStatus  # noqa: E402
from audit.identity import IdentityIndex  # noqa: E402
from audit.loader import DATASET_DIR, load_dataset  # noqa: E402
from llm.cache import UsageLedger  # noqa: E402
from llm.vision import ScreenshotTranscriber  # noqa: E402

OUT_PATH = CODE_DIR / "docs" / "evidence_report.md"


def link_routes(observation, user_id: str, index: IdentityIndex) -> set[str]:
    """How an observation ties to user_id: by which basis of identity ref."""
    routes = set()
    for ref in observation.identity_refs:
        if user_id in {c.user_id for c in index.claims(ref.kind, ref.value)}:
            routes.add(ref.basis.split(":")[0])
    return routes


def sample_linkage(dataset, result, index):
    """For each sample-answer record: the routes linking it to the case subject
    using observations made at or before audit_at."""
    with (DATASET_DIR / "sample_output.csv").open(encoding="utf-8") as handle:
        gold = {r["case_id"]: json.loads(r["steps_json"]) for r in csv.DictReader(handle)}
    by_resource = defaultdict(list)
    for obs in result.observations:
        if obs.resource_id:
            by_resource[obs.resource_id].append(obs)
        for related in obs.related_resources:
            by_resource[("related", related)].append(obs)
    rows = []
    for request in dataset.requests:
        subject = index.resolve_request(request).subject
        for resource_id in sorted({s["resource_id"] for s in gold[request.case_id]}):
            routes = set()
            for obs in by_resource.get(resource_id, []):
                if obs.observed_at <= request.audit_at:
                    routes |= link_routes(obs, subject.user_id, index)
            for obs in by_resource.get(("related", resource_id), []):
                if obs.observed_at <= request.audit_at and link_routes(obs, subject.user_id, index):
                    routes.add("related_resource")
            if any(l.resource_id == resource_id for l in index.links_for(subject.user_id,
                                                                         request.audit_at)):
                routes.add("catalog_link")
            seen = [o for o in by_resource.get(resource_id, []) if o.observed_at <= request.audit_at]
            rows.append((request.case_id, subject.user_id, resource_id, sorted(routes), len(seen)))
    return rows


def extra_sections(result, identities) -> list[str]:
    """Duplicates, lookup-index attribution, catalog-link conflicts, screenshots."""
    from audit.evidence.duplicates import analyse_duplicates
    from audit.evidence.index import EvidenceIndex
    import screenshot_accuracy
    lines = []
    summary = analyse_duplicates(result.observations).summary()
    lines += ["## 7. Duplicate accounting", "",
              "Definitions are in `audit/evidence/duplicates.py`. Nothing is removed.", ""]
    lines += [f"- {k}: {v}" for k, v in summary.items()]
    index = EvidenceIndex(result, identities)
    lines += ["", "## 8. Lookup index attribution", ""]
    lines += [f"- {k}: {v}" for k, v in index.counts().items()]
    conflicts = index.link_conflicts()
    lines += ["", f"Catalog links contradicted by evidence: {len(conflicts)}"]
    for link, obs, owners in conflicts:
        lines.append(f"- {link.activity_id} links {link.resource_id} to {link.user_id}, but "
                     f"{obs.obs_id} shows {[r.raw for r in obs.identity_refs]} "
                     f"(known owners: {list(owners) or 'none'})")
    png = {f: r for f, r in result.files.items() if r.kind == "png"}
    methods = Counter(str(r.details.get("transcription")) for r in png.values())
    rows = screenshot_accuracy.compare(result.observations)
    matched = sum(r["match"] for r in rows)
    lines += ["", "## 9. Screenshots", "",
              f"{len(png)} screenshots; transcription method: "
              + ", ".join(f"{k}={v}" for k, v in sorted(methods.items()))
              + ". Manual transcriptions were made by the coding agent viewing each PNG; they "
              "are not automated runtime extraction.",
              f"Ground-truth comparison (test-only fixture): {matched}/{len(rows)} facts match.", ""]
    for r in rows:
        if not r["match"]:
            lines.append(f"- MISMATCH {r['file']} {r['record']} {r['fact']}: expected "
                         f"{r['expected']!r}, got {r['actual']!r}")
    lines.append("")
    return lines


def timing_attribution_check(dataset) -> list[str]:
    """Measure whether activity timing alone identifies a case's own account:
    the deletion_worker account deletion nearest to reported_at, within
    [reported_at - 2h, reported_at + 10min]. Not used by extraction."""
    from datetime import timedelta
    with (DATASET_DIR / "sample_output.csv").open(encoding="utf-8") as handle:
        gold = {r["case_id"]: json.loads(r["steps_json"]) for r in csv.DictReader(handle)}
    outcome = Counter()
    unique_right = unique_total = 0
    for request in dataset.requests:
        accounts = {s["resource_id"] for s in gold[request.case_id]
                    if s["system_id"] == "account_service"}
        low = request.reported_at - timedelta(hours=2)
        high = request.reported_at + timedelta(minutes=10)
        candidates = [a for a in dataset.activities
                      if a.event_type == "DELETE_RECORD" and a.source_system == "deletion_worker"
                      and a.target_system == "account_service" and low <= a.event_time <= high]
        if not candidates:
            outcome["no candidate"] += 1
            continue
        nearest = min(candidates, key=lambda a: abs(a.event_time - request.reported_at))
        right = nearest.resource_id in accounts
        if len(candidates) == 1:
            unique_total += 1
            unique_right += right
        outcome["right" if right and len(candidates) == 1 else
                "right among several candidates" if right else "wrong"] += 1
    return ["## 5b. Can activity timing attribute an account to a case?", "",
            "Rule tested: the account deletion by deletion_worker nearest to the case's "
            "reported_at (window -2h/+10min). Result on the 32 samples: "
            + ", ".join(f"{k}={v}" for k, v in sorted(outcome.items())) + ". "
            f"When exactly one candidate exists it was right {unique_right}/{unique_total}. "
            "Requests reported at the same time compete for the same events, so timing alone "
            "is not used to link identity.", ""]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vision", action="store_true",
                        help="read screenshots with the vision model (needs credentials) "
                             "instead of the manual transcriptions")
    args = parser.parse_args(argv)

    dataset = load_dataset(requests_file="sample_deletion_requests.csv")
    index = IdentityIndex.from_dataset(dataset)
    usage = UsageLedger()
    transcriber = (ScreenshotTranscriber(usage=usage, allow_api=True) if args.vision
                   else ScreenshotTranscriber.manual(usage=usage))
    result = extract_evidence(dataset, transcriber=transcriber)

    lines = ["# Evidence extraction report", "",
             "Generated by `python code/tools/evidence_report.py`. Sample answers are used "
             "only to measure linkage in section 5; extraction never reads them.", ""]

    statuses = Counter(f.status.value for f in result.files.values())
    lines += ["## 1. Parser coverage", "",
              f"{len(result.files)} of {len(dataset.evidence_files)} evidence files processed: "
              + ", ".join(f"{k}={v}" for k, v in sorted(statuses.items())) + ".", "",
              "| file_id | kind | parser | status | observations | coverages | units | skipped |"
              " details |", "|---|---|---|---|---|---|---|---|---|"]
    for file_id, report in sorted(result.files.items(), key=lambda kv: int(kv[0].split("_")[1])):
        details = ", ".join(f"{k}={v}" for k, v in report.details.items() if k != "tables")
        lines.append(f"| {file_id} | {report.kind} | {report.parser} | {report.status.value} | "
                     f"{report.observations} | {report.coverages} | {report.units_seen} | "
                     f"{report.units_skipped} | {details} |")
    unrecognised = [f for f, r in result.files.items()
                    if r.status in (FileStatus.FAILED, FileStatus.PENDING_AI)]
    lines += ["", f"Unrecognised or unprocessed files: {', '.join(unrecognised) or 'none'}.", ""]

    obs = result.observations
    lines += ["## 2. Observations", "",
              f"{len(obs)} observations, {len(result.coverages)} coverage records.", ""]
    for title, counter in (
            ("By source and condition", Counter((o.source_kind, o.condition.value) for o in obs)),
            ("By data category", Counter(str(o.data_category) for o in obs)),
            ("By exposure", Counter(o.exposure.value for o in obs)),
            ("By time basis", Counter(o.time_basis.value for o in obs)),
            ("By replica", Counter(str(o.replica) for o in obs)),
            ("Identity refs by basis", Counter(r.basis.split(":")[0] for o in obs
                                               for r in o.identity_refs))):
        lines.append(f"- {title}: " + ", ".join(f"{k}={v}" for k, v in sorted(
            counter.items(), key=lambda kv: (-kv[1], str(kv[0])))))
    undetermined = Counter(o.extractor for o in obs if o.data_category is None)
    lines += [f"- Category undetermined: {sum(undetermined.values())} "
              f"({', '.join(f'{k}={v}' for k, v in undetermined.items())})", ""]

    lines += ["## 3. Extraction issues", ""]
    by_severity = Counter(i.severity for i in result.issues)
    lines.append(", ".join(f"{k}={v}" for k, v in sorted(by_severity.items())) or "none")
    lines.append("")
    for issue in result.issues:
        lines.append(f"- {issue}")
    lines.append("")

    unknown, multi = Counter(), []
    for o in obs:
        owners = set()
        for ref in o.identity_refs:
            users = {c.user_id for c in index.claims(ref.kind, ref.value)}
            if not users:
                unknown[ref.kind.value] += 1
            owners |= users
        if len(owners) > 1:
            multi.append((o, sorted(owners)))
    lines += ["## 4. Identity-linking checks", "",
              f"- Identity refs that match no known user (decoys or unknown people): "
              f"{sum(unknown.values())} ({', '.join(f'{k}={v}' for k, v in unknown.items())})",
              f"- Observations whose refs point to more than one user: {len(multi)}"]
    for o, owners in multi[:30]:
        lines.append(f"  - {o.obs_id} {o.resource_id}: {owners} "
                     f"({', '.join(sorted({r.basis for r in o.identity_refs}))})")
    lines.append("")

    rows = sample_linkage(dataset, result, index)
    linked = [r for r in rows if r[3]]
    lines += ["## 5. Sample-answer records linked to their subject", "",
              f"{len(linked)} of {len(rows)} distinct (case, resource) pairs are linked to the "
              "resolved subject by evidence observed at or before audit_at.", "",
              "Routes: " + ", ".join(f"{k}={v}" for k, v in sorted(
                  Counter(route for r in rows for route in r[3]).items())), "",
              "Unlinked pairs (no identifier for the subject in any admissible observation):", "",
              "| case | user | resource | admissible observations |", "|---|---|---|---|"]
    for case_id, user_id, resource_id, routes, seen in rows:
        if not routes:
            lines.append(f"| {case_id} | {user_id} | {resource_id} | {seen} |")
    lines.append("")

    lines += timing_attribution_check(dataset)
    lines += extra_sections(result, index)

    calls = usage.api_calls()
    lines += ["## 10. Model usage in this run", "",
              f"Vision API calls: {len(calls)}; transcriptions served from the cache: {len(usage.records) - len(calls)} (methods: {dict(Counter(r.method for r in usage.records))}); "
              f"input tokens {sum(r.input_tokens for r in calls)}, output tokens "
              f"{sum(r.output_tokens for r in calls)}.", ""]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
