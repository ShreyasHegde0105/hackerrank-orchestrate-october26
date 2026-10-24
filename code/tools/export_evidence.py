"""Extract all evidence and export the observations as JSONL.

Usage (from the repository root):
    python code/tools/export_evidence.py [--out artifacts/evidence_observations.jsonl]

Screenshots are read from the manual transcriptions (no API calls). The schema
is documented in code/docs/evidence_jsonl_schema.md. Output is deterministic:
re-running on the same inputs writes a byte-identical file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from audit.evidence.export import write_jsonl  # noqa: E402
from audit.evidence.extract import extract_evidence  # noqa: E402
from audit.loader import REPO_ROOT, load_dataset  # noqa: E402
from llm.vision import ScreenshotTranscriber  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "artifacts" / "evidence_observations.jsonl"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    result = extract_evidence(load_dataset(), transcriber=ScreenshotTranscriber.manual())
    count = write_jsonl(result.observations, args.out)
    failed = [f for f, r in result.files.items() if r.status.value in ("failed", "pending_ai")]
    print(f"wrote {count} observations to {args.out}")
    if failed:
        print(f"warning: {len(failed)} file(s) not extracted: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
