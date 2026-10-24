"""A content-addressed JSON cache and a ledger of real model usage.

Cache entries are keyed by a hash of everything that determines the answer
(model, prompt version, input bytes), so a changed prompt or input never
reuses a stale result, and a rerun never pays twice.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = CODE_DIR / "llm" / "cache"


def cache_key(*parts: bytes | str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        data = part.encode("utf-8") if isinstance(part, str) else part
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


class JsonCache:
    def __init__(self, directory: Path | str = DEFAULT_CACHE_DIR, namespace: str = "default"):
        self.directory = Path(directory) / namespace

    def path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> dict | None:
        path = self.path(key)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None  # a corrupt entry is treated as a miss and overwritten

    def put(self, key: str, value: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.path(key).with_suffix(".tmp")
        tmp.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.path(key))


@dataclass
class UsageRecord:
    purpose: str            # e.g. "screenshot_extraction"
    model: str
    input_tokens: int
    output_tokens: int
    cache_hit: bool         # True when served from the local cache (no API call)
    subject: str = ""       # e.g. the evidence file id


@dataclass
class UsageLedger:
    records: list[UsageRecord] = field(default_factory=list)

    def add(self, record: UsageRecord) -> None:
        self.records.append(record)

    def api_calls(self) -> list[UsageRecord]:
        return [r for r in self.records if not r.cache_hit]

    def to_json(self) -> list[dict]:
        return [asdict(r) for r in self.records]
