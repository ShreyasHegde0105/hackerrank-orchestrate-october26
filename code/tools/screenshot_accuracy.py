"""Compare extracted screenshot observations with the hand-checked ground truth.

Usage (from the repository root):  python code/tools/screenshot_accuracy.py

Ground truth: code/tests/fixtures/screenshot_ground_truth.json (test-only, never
read by the pipeline). The observations compared are the pipeline's real
output for the 11 PNGs, produced from the manual transcriptions.

Checked facts, each counted separately:
  record_found        a record with the ground-truth key (resource ID, or label) exists
  identifiers         the set of normalised identifiers is identical
  person_name_shown   whether a person's name appeared on screen
  condition           PRESENT / ACTIVE / ... as decided by screenshots.py
  related             related resource IDs (only IDs; ticket/order numbers excluded)
  status / retention_end / email_opt_in   where the ground truth records one
  extra_records       observations with no ground-truth record (each one a mismatch)
  screen_time         the page's on-screen time, where the ground truth has one
Note: ground truth and transcription were both made by the same agent in
separate passes, so agreement shows the pipeline reads the transcriptions
correctly and the two passes are consistent; it does not validate automated
(model) screenshot extraction.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from audit.evidence.classify import RESOURCE_ID  # noqa: E402
from audit.evidence.screenshots import screen_time  # noqa: E402
from audit.identity import classify_identifier  # noqa: E402

GROUND_TRUTH = CODE_DIR / "tests" / "fixtures" / "screenshot_ground_truth.json"


def _ids(values) -> set:
    return {classify_identifier(v) for v in values} - {None}


def compare(observations, ground_truth: dict | None = None) -> list[dict]:
    """One row per checked fact: {file, record, fact, expected, actual, match}."""
    truth = ground_truth or json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    rows = []
    for file_id, expected in sorted(truth["files"].items()):
        observed = [o for o in observations if o.evidence_id == file_id and o.locator != "page"]
        by_key = {}
        for obs in observed:
            by_key[obs.resource_id or (obs.labels[0] if obs.labels else obs.locator)] = obs

        def row(record, fact, want, got):
            rows.append({"file": file_id, "record": record, "fact": fact,
                         "expected": want, "actual": got, "match": want == got})

        page_text = observed[0].attr("captured_at_text") if observed else None
        if expected.get("screen_time"):
            got = screen_time(page_text)
            row("(page)", "screen_time", expected["screen_time"],
                got.strftime("%Y-%m-%dT%H:%M:%SZ") if got else None)
        for record in expected["records"]:
            key = record["key"]
            obs = by_key.get(key)
            row(key, "record_found", True, obs is not None)
            if obs is None:
                continue
            row(key, "identifiers", sorted(map(str, _ids(record["identifiers"]))),
                sorted(map(str, {(r.kind, r.value) for r in obs.identity_refs})))
            row(key, "person_name_shown", record["person_name_shown"], obs.attr("person_name_shown"))
            row(key, "condition", record["condition"], obs.condition.value)
            row(key, "related", sorted(record["related"]),
                sorted(r for r in obs.related_resources if RESOURCE_ID.fullmatch(r)))
            if "status" in record:
                row(key, "status", record["status"], obs.attr("status_text"))
            if "retention_end" in record:
                row(key, "retention_end", record["retention_end"], obs.attr("retention_end"))
            if "email_opt_in" in record:
                row(key, "email_opt_in", record["email_opt_in"], obs.attr("email_opt_in"))
        for key in sorted(set(by_key) - {r["key"] for r in expected["records"]}):
            row(key, "extra_record", "absent from ground truth", "present")
    return rows


def main() -> int:
    from audit.evidence.extract import extract_evidence
    from audit.loader import load_dataset
    from llm.vision import ScreenshotTranscriber
    result = extract_evidence(load_dataset(), transcriber=ScreenshotTranscriber.manual())
    rows = compare(result.observations)
    matched = sum(r["match"] for r in rows)
    print(f"{matched}/{len(rows)} facts match ({100 * matched / len(rows):.1f}%)")
    for r in rows:
        if not r["match"]:
            print(f"MISMATCH {r['file']} {r['record']} {r['fact']}: expected {r['expected']!r}, "
                  f"got {r['actual']!r}")
    return 0 if matched == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
