"""JSONL export of observations: one JSON object per line, deterministic.

Schema (version 1) is documented in code/docs/evidence_jsonl_schema.md.
Lines are ordered by (evidence number, locator); keys are sorted; timestamps
are UTC 'YYYY-MM-DDTHH:MM:SSZ'; enums are their string values; non-ASCII text
is written as UTF-8, not escaped. The same observations always produce
byte-identical files, and read_jsonl(write_jsonl(x)) == x.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from audit.evidence.model import (
    Condition,
    Confidence,
    Exposure,
    IdentityRef,
    Observation,
    TimeBasis,
)
from audit.loader import format_timestamp, parse_timestamp
from audit.models import IdentifierKind

SCHEMA_VERSION = 1


def _sort_key(obs: Observation) -> tuple:
    number = obs.evidence_id.rsplit("_", 1)[-1]
    return (int(number) if number.isdigit() else 0, obs.evidence_id, obs.locator)


def observation_to_dict(obs: Observation) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "obs_id": obs.obs_id,
        "evidence_id": obs.evidence_id,
        "source_kind": obs.source_kind,
        "locator": obs.locator,
        "observed_at": format_timestamp(obs.observed_at),
        "time_basis": obs.time_basis.value,
        "file_captured_at": format_timestamp(obs.file_captured_at),
        "system_id": obs.system_id,
        "resource_id": obs.resource_id,
        "data_category": obs.data_category,
        "category_basis": obs.category_basis,
        "condition": obs.condition.value,
        "exposure": obs.exposure.value,
        "identity_refs": [{"kind": r.kind.value, "value": r.value, "raw": r.raw,
                           "field": r.field, "basis": r.basis} for r in obs.identity_refs],
        "related_resources": list(obs.related_resources),
        "labels": list(obs.labels),
        "replica": obs.replica,
        "collection": obs.collection,
        "extractor": obs.extractor,
        "confidence": obs.confidence.value,
        "attributes": obs.attributes,
    }


def observation_from_dict(data: dict) -> Observation:
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {data.get('schema_version')!r}")
    return Observation(
        evidence_id=data["evidence_id"], source_kind=data["source_kind"],
        locator=data["locator"], observed_at=parse_timestamp(data["observed_at"]),
        time_basis=TimeBasis(data["time_basis"]),
        file_captured_at=parse_timestamp(data["file_captured_at"]),
        system_id=data["system_id"], resource_id=data["resource_id"],
        data_category=data["data_category"], category_basis=data["category_basis"],
        condition=Condition(data["condition"]), exposure=Exposure(data["exposure"]),
        identity_refs=tuple(IdentityRef(IdentifierKind(r["kind"]), r["value"], r["raw"],
                                        r["field"], r["basis"]) for r in data["identity_refs"]),
        related_resources=tuple(data["related_resources"]), labels=tuple(data["labels"]),
        replica=data["replica"], collection=data["collection"], extractor=data["extractor"],
        confidence=Confidence(data["confidence"]), attributes=data["attributes"])


def _reject_unserialisable(value):
    if isinstance(value, datetime):
        return format_timestamp(value)
    raise TypeError(f"attribute value of type {type(value).__name__} is not JSON-serialisable")


def dumps(obs: Observation) -> str:
    return json.dumps(observation_to_dict(obs), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=_reject_unserialisable)


def write_jsonl(observations, path: Path | str) -> int:
    """Write observations in canonical order; return the number of lines.
    Writes to a temporary file first, so a failure never leaves a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [dumps(obs) for obs in sorted(observations, key=_sort_key)]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(line + "\n" for line in lines), encoding="utf-8", newline="\n")
    tmp.replace(path)
    return len(lines)


def read_jsonl(path: Path | str) -> list[Observation]:
    observations = []
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                observations.append(observation_from_dict(json.loads(line)))
            except (ValueError, KeyError) as exc:
                raise ValueError(f"{path}:{number}: invalid observation ({exc})") from None
    return observations
