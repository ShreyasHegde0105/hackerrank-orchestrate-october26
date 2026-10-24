"""Ghost Data Still Exists: deletion audit. Writes output.csv.

Usage (from the repository root):
    python code/main.py                                   # all 518 requests -> output.csv
                                                          # (approved policy: P1 on, P3 SECONDARY_IDENTIFIER)
    python code/main.py --requests sample_deletion_requests.csv --out artifacts/sample_predictions.csv
    python code/main.py --diagnostics artifacts/diagnostics.json

Pipeline: load dataset (Phase 2) -> extract evidence, screenshots from the
manual transcriptions (Phase 3) -> reconstruct each case at audit_at
(Phase 4) -> apply deletion_rules.csv and build steps (Phase 5) -> write
output.csv sorted by case_id, then validate it against the output contract.
No network calls; no API keys needed.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from audit.contract import OUTPUT_COLUMNS, case_sort_key  # noqa: E402
from audit.decide import APPROVED, DecisionEngine, PolicyOptions  # noqa: E402
from audit.evidence.extract import extract_evidence  # noqa: E402
from audit.identity import IdentityIndex  # noqa: E402
from audit.loader import DATASET_DIR, REPO_ROOT, DatasetError, load_dataset  # noqa: E402
from audit.reconstruct import Reconstructor  # noqa: E402
from audit.validator import validate_output_file  # noqa: E402
from llm.vision import ScreenshotTranscriber  # noqa: E402


def run(requests_file: str, options: PolicyOptions):
    dataset = load_dataset(requests_file=requests_file)
    result = extract_evidence(dataset, transcriber=ScreenshotTranscriber.manual())
    reconstructor = Reconstructor(dataset, IdentityIndex.from_dataset(dataset), result)
    return result, DecisionEngine(dataset, reconstructor, options).decide_all()


def write_output(decisions, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted((d.row() for d in decisions), key=lambda r: case_sort_key(r["case_id"]))
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(OUTPUT_COLUMNS), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Deletion audit: writes output.csv")
    parser.add_argument("--requests", default="deletion_requests.csv",
                        help="requests file inside dataset/")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "output.csv")
    parser.add_argument("--diagnostics", type=Path, help="also write per-case diagnostics (JSON)")
    parser.add_argument("--use-activity-candidates", dest="use_activity_candidates",
                        action="store_true", default=APPROVED.use_activity_candidates,
                        help="policy P1 (default on): verify the single account deletion near "
                             "reported_at against a citable SQL snapshot")
    parser.add_argument("--no-activity-candidates", dest="use_activity_candidates",
                        action="store_false", help="policy P1 off: activity candidates stay diagnostic")
    parser.add_argument("--empty-case-status", default=APPROVED.empty_case_status,
                        choices=["COMPLETE", "INCOMPLETE"],
                        help="policy P2: status of a deletion request with no step")
    parser.add_argument("--uncategorised-support-category",
                        default=APPROVED.uncategorised_support_category,
                        help="policy P3 (default SECONDARY_IDENTIFIER): category for contact-index "
                             "support rows of undetermined category; 'none' for no steps")
    args = parser.parse_args(argv)
    category = args.uncategorised_support_category
    options = PolicyOptions(args.use_activity_candidates, args.empty_case_status,
                            None if str(category).lower() == "none" else category)

    try:
        result, decisions = run(args.requests, options)
    except (DatasetError, OSError) as exc:
        print(f"input or pipeline error: {exc}", file=sys.stderr)
        return 1
    not_extracted = [f for f, r in result.files.items() if r.status.value in ("failed", "pending_ai")]
    write_output(decisions, args.out)
    issues = validate_output_file(args.out, DATASET_DIR / args.requests)

    counts = {}
    for d in decisions:
        counts[d.status] = counts.get(d.status, 0) + 1
    print(f"wrote {len(decisions)} rows to {args.out}: "
          + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity != "error"]
    print(f"evidence files not extracted: {len(not_extracted)}; contract issues: {len(errors)}"
          + (f"; warnings (stricter than the contract): {len(warnings)}" if warnings else ""))
    for issue in (errors + warnings)[:20]:
        print(f"  {issue}")
    if args.diagnostics:
        args.diagnostics.parent.mkdir(parents=True, exist_ok=True)
        args.diagnostics.write_text(json.dumps(
            {d.case_id: {"status": d.status, "intent": d.intent.rule,
                         "rules": [x.rule for x in d.decisions], "diagnostics": d.diagnostics}
             for d in decisions}, indent=1, ensure_ascii=False), encoding="utf-8")
    # Warnings (checks stricter than the official contract, e.g. an authorized
    # case with no evidence-backed step) are reported but do not fail the run.
    return 0 if not not_extracted and not errors else 1


if __name__ == "__main__":
    sys.exit(main())
