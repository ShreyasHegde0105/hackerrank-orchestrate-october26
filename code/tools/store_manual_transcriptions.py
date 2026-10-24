"""Store the manual screenshot transcriptions in the transcription cache.

Usage (from the repository root):
    python code/tools/store_manual_transcriptions.py          # write cache entries
    python code/tools/store_manual_transcriptions.py --check  # verify, write nothing

Source: code/llm/manual_transcriptions/screenshots_v1.json (hand transcriptions,
keyed by image file name). Each becomes a cache entry under the pseudo-model
"manual-transcription", keyed to the image's exact bytes, with method "manual",
the transcriber, and the image's SHA-256, so every transcription stays tied to
the PNG it describes. --check fails if any screenshot lacks a transcription or
an entry no longer matches its image or source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from audit.loader import load_dataset  # noqa: E402
from llm.vision import MANUAL_MODEL, PROMPT_VERSION, ScreenshotTranscriber, validate  # noqa: E402

SOURCE = CODE_DIR / "llm" / "manual_transcriptions" / "screenshots_v1.json"


def build_entries(dataset, source: dict) -> tuple[dict[str, dict], list[str]]:
    """cache key -> entry for every screenshot in the manifest; plus problems."""
    transcriber = ScreenshotTranscriber.manual()
    by_name = source["screenshots"]
    entries, problems = {}, []
    for evidence in sorted(dataset.evidence_files.values(), key=lambda f: f.file_id):
        if evidence.kind != "png":
            continue
        name = Path(evidence.file_path).name
        item = by_name.get(name)
        if item is None:
            problems.append(f"{evidence.file_id} ({name}) has no manual transcription")
            continue
        if item["file_id"] != evidence.file_id:
            problems.append(f"{name}: source says {item['file_id']}, manifest says {evidence.file_id}")
        image = (dataset.root / evidence.file_path).read_bytes()
        validate(item["result"])
        entries[transcriber.key(image)] = {
            "model": MANUAL_MODEL, "prompt_version": PROMPT_VERSION, "method": "manual",
            "transcriber": source["transcriber"], "transcribed_at": source["transcribed_at"],
            "subject": evidence.file_id, "image_file": name,
            "image_sha256": hashlib.sha256(image).hexdigest(),
            "usage": {"input_tokens": 0, "output_tokens": 0}, "result": item["result"]}
    for name in sorted(set(by_name) - {Path(f.file_path).name for f in dataset.evidence_files.values()}):
        problems.append(f"{name} is transcribed but is not in the evidence manifest")
    return entries, problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify only; write nothing")
    args = parser.parse_args(argv)
    dataset = load_dataset()
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    entries, problems = build_entries(dataset, source)
    cache = ScreenshotTranscriber.manual().cache
    for key, entry in sorted(entries.items(), key=lambda kv: kv[1]["subject"]):
        if args.check:
            if cache.get(key) != entry:
                problems.append(f"{entry['subject']}: cache entry missing or out of date")
        else:
            cache.put(key, entry)
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    verb = "verified" if args.check else "stored"
    print(f"{verb} {len(entries)} manual transcriptions; {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
