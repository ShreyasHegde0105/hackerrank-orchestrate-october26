"""Profile the participant dataset and write code/docs/data_profile.md.

Usage (from the repository root):  python code/tools/profile_dataset.py

Everything in the report is computed from dataset/ at run time; nothing is
hand-entered. Hand-written interpretation lives in code/docs/data_assumptions.md.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from audit.loader import DATASET_DIR, read_csv  # noqa: E402

OUT_PATH = CODE_DIR / "docs" / "data_profile.md"
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
RESOURCE_ID = re.compile(r"\b([a-z]+(?:_[a-z]+)*)_(\d+)\b")
LOG_LINE = re.compile(r"^(\S+)\s+(?:level=)?(\w+)\s")
LOG_PAIR = re.compile(r'(\w+)=("[^"]*"|\S+)')
PIPE_HEADER = re.compile(
    r"^\S+ \| \w+ \| (?P<service>\S+) \| \S+ \| (?P<event>\w+) \| (?P<rest>.*)$")
BRACKET_HEADER = re.compile(
    r"^\S+ \[\w+\] \[(?P<service>[^\]]+)\] \[[^\]]+\] (?P<event>\w+): (?P<rest>.*)$")
CITABLE_PREFIXES = {"evidence", "activity", "audit"}

CSV_FILES = [
    "deletion_requests.csv", "sample_deletion_requests.csv", "sample_output.csv",
    "user_identities.csv", "user_audit_log.csv", "system_activity.csv",
    "deletion_rules.csv", "evidence_files.csv", "output.csv",
]
TIME_COLUMNS = {"requested_at", "reported_at", "audit_at", "updated_at", "event_time", "captured_at"}


def table(headers: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |" for row in rows]
    return out + [""]


def json_shape(value, depth: int = 0):
    """Key structure of a JSON value, merging list items, values replaced by types."""
    if isinstance(value, dict):
        return {k: json_shape(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        merged: dict = {}
        for item in value:
            shape = json_shape(item, depth + 1)
            if isinstance(shape, dict):
                for k, v in shape.items():
                    merged.setdefault(k, v)
            else:
                return [shape]
        return [merged] if merged else []
    return type(value).__name__


def profile_csvs(lines: list[str]) -> None:
    lines += ["## 1. CSV files", ""]
    rows_out = []
    bad_times = []
    for name in CSV_FILES:
        rows = read_csv(DATASET_DIR / name)
        with (DATASET_DIR / name).open(encoding="utf-8-sig") as handle:
            header = handle.readline().strip()
        for row in rows:
            for col in TIME_COLUMNS & set(row):
                if not TIMESTAMP.match(row[col]):
                    bad_times.append((name, col, row[col]))
        rows_out.append([f"`{name}`", len(rows), f"`{header}`"])
    lines += table(["File", "Data rows", "Header"], rows_out)
    lines.append(f"All timestamp columns use `YYYY-MM-DDTHH:MM:SSZ` (UTC): "
                 f"{'yes' if not bad_times else f'NO, {len(bad_times)} exceptions'}.")
    lines.append("")


def profile_requests(lines: list[str]) -> None:
    lines += ["## 2. Requests and identities", ""]
    identities = read_csv(DATASET_DIR / "user_identities.csv")
    audit = read_csv(DATASET_DIR / "user_audit_log.csv")
    current = {r["email"].lower(): r["user_id"] for r in identities}
    historical = defaultdict(set)
    for r in audit:
        if r["field_name"] == "email":
            historical[r["old_value"].lower()].add(r["user_id"])
    owners = defaultdict(set)
    for email, uid in current.items():
        owners[email].add(uid)
    for email, uids in historical.items():
        owners[email] |= uids

    for name in ("sample_deletion_requests.csv", "deletion_requests.csv"):
        reqs = read_csv(DATASET_DIR / name)
        ids = [r["case_id"] for r in reqs]
        resolved = Counter(
            "current" if r["email"].lower() in current
            else "historical only" if r["email"].lower() in historical
            else "unresolved" for r in reqs)
        non_ascii = sum(any(ord(ch) > 127 for ch in r["request_text"]) for r in reqs)
        lines += [
            f"**`{name}`**: {len(reqs)} cases, IDs `{ids[0]}` .. `{ids[-1]}` "
            f"(file order is numeric; lexicographically sorted: {ids == sorted(ids)}).",
            f"- reported_status: {dict(Counter(r['reported_status'] for r in reqs))}",
            f"- audit_at range: {min(r['audit_at'] for r in reqs)} .. {max(r['audit_at'] for r in reqs)}",
            f"- requested_at <= reported_at <= audit_at for "
            f"{sum(r['requested_at'] <= r['reported_at'] <= r['audit_at'] for r in reqs)}/{len(reqs)}",
            f"- request email resolves via: {dict(resolved)}",
            f"- request_text containing non-ASCII characters (other languages / typography): {non_ascii}",
            "",
        ]
    lines += [
        f"Identities: {len(identities)} users; emails owned by more than one user "
        f"(current or historical): {sum(len(v) > 1 for v in owners.values())}.",
        f"Audit-log changes by field: {dict(Counter(r['field_name'] for r in audit))}; "
        f"users with any change: {len({r['user_id'] for r in audit})}.",
        "",
    ]


def profile_activity(lines: list[str]) -> None:
    rows = read_csv(DATASET_DIR / "system_activity.csv")
    times = [r["event_time"] for r in rows]
    lines += ["## 3. System activity", "",
              f"{len(rows)} events, {min(times)} .. {max(times)}. "
              f"File is in time order: {times == sorted(times)} "
              f"({sum(a > b for a, b in zip(times, times[1:]))} adjacent rows go back in time). "
              f"reported_result: {dict(Counter(r['reported_result'] for r in rows))}.", ""]
    by_type = defaultdict(Counter)
    for r in rows:
        by_type[r["event_type"]][f"{r['source_system']} -> {r['target_system']}"] += 1
    lines += table(["event_type", "Events", "Source -> target (top 3)"], [
        [f"`{t}`", sum(c.values()), "; ".join(f"{k} ({n})" for k, n in c.most_common(3))]
        for t, c in sorted(by_type.items())])


def profile_evidence(lines: list[str]) -> dict[str, str]:
    manifest = read_csv(DATASET_DIR / "evidence_files.csv")
    by_path = {r["file_path"]: r for r in manifest}
    on_disk = {p.relative_to(DATASET_DIR).as_posix()
               for p in (DATASET_DIR / "evidence").rglob("*") if p.is_file()}
    lines += ["## 4. Evidence manifest", "",
              f"{len(manifest)} manifest rows; {len(on_disk)} files on disk; "
              f"listed but missing: {sorted(set(by_path) - on_disk) or 'none'}; "
              f"on disk but unlisted: {sorted(on_disk - set(by_path)) or 'none'}.",
              f"captured_at range: {min(r['captured_at'] for r in manifest)} .. "
              f"{max(r['captured_at'] for r in manifest)}.", ""]
    kinds = Counter(Path(p).suffix for p in by_path)
    lines += table(["Extension", "Files"], [[k, v] for k, v in sorted(kinds.items())])
    lines += table(["Origin", "Files"], [[f"`{k}`", v] for k, v in
                                         sorted(Counter(r["origin"] for r in manifest).items())])
    texts = {}
    for path, row in by_path.items():
        if Path(path).suffix in {".sql", ".json", ".log", ".txt"}:
            texts[row["file_id"]] = (DATASET_DIR / path).read_text(encoding="utf-8")
    return texts


def profile_sql(lines: list[str]) -> None:
    lines += ["## 5. SQL snapshots", "",
              "Each file is executed in an isolated in-memory SQLite database.", ""]
    manifest = {r["file_path"]: r for r in read_csv(DATASET_DIR / "evidence_files.csv")}
    rows_out, schemas = [], defaultdict(set)
    for path in sorted(p for p in manifest if p.endswith(".sql")):
        text = (DATASET_DIR / path).read_text(encoding="utf-8")
        topology = re.search(r"Database topology:\s*(\S+)", text)
        db = sqlite3.connect(":memory:")
        db.executescript(text)
        tables = [r[0] for r in db.execute("select name from sqlite_master where type='table'")]
        counts = []
        for t in tables:
            cols = tuple(r[1] for r in db.execute(f'pragma table_info("{t}")'))
            schemas[t].add(cols)
            counts.append(f"{t}={db.execute(f'select count(*) from \"{t}\"').fetchone()[0]}")
        scope = ""
        if "snapshot_scope" in tables:
            systems = [r[0] for r in db.execute("select origin_system from snapshot_scope")]
            scope = f"{len(systems)} systems" if len(systems) > 1 else f"only {systems}"
        if "customer_data_primary" in tables:
            mixed = db.execute("select origin_system, count(*) from customer_data_primary group by 1")
            scope = ", ".join(f"{s}:{n}" for s, n in mixed)
        db.close()
        rows_out.append([manifest[path]["file_id"], f"`{Path(path).name}`",
                         topology.group(1) if topology else "", ", ".join(counts), scope])
    lines += table(["file_id", "File", "Topology", "Rows per table", "Scope / origin systems"], rows_out)
    lines += ["Table schemas (identical across files unless several are listed):", ""]
    lines += table(["Table", "Columns"], [[f"`{t}`", "; ".join(", ".join(c) for c in sorted(s))]
                                          for t, s in sorted(schemas.items())])


def profile_json(lines: list[str]) -> None:
    lines += ["## 6. JSON records", ""]
    manifest = read_csv(DATASET_DIR / "evidence_files.csv")
    families = defaultdict(list)
    for row in manifest:
        if row["file_path"].endswith(".json"):
            data = json.loads((DATASET_DIR / row["file_path"]).read_text(encoding="utf-8"))
            families[json.dumps(json_shape(data), sort_keys=True)].append(row["file_id"])
    for i, (shape, ids) in enumerate(sorted(families.items(), key=lambda kv: kv[1]), start=1):
        lines += [f"**Shape {i}** ({len(ids)} files: {', '.join(ids)})", "", "```json",
                  json.dumps(json.loads(shape), indent=1)[:2500], "```", ""]


def profile_text_logs(lines: list[str]) -> None:
    lines += ["## 7. Logs and text records", ""]
    manifest = read_csv(DATASET_DIR / "evidence_files.csv")
    rows_out = []
    for row in manifest:
        path = row["file_path"]
        if not path.endswith((".log", ".txt")):
            continue
        text_lines = (DATASET_DIR / path).read_text(encoding="utf-8").splitlines()
        keys, events, styles = Counter(), Counter(), Counter()
        for line in text_lines:
            stamp, _, body = line.partition(" ")
            pipe = PIPE_HEADER.match(line)
            bracket = BRACKET_HEADER.match(line)
            if body.startswith("{"):
                styles["timestamp + JSON object"] += 1
                pairs = {k: str(v) for k, v in json.loads(body).items()}
            elif pipe or bracket:
                header = pipe or bracket
                styles["pipe header + key=value" if pipe else "bracket header + key=value"] += 1
                pairs = {k: v.strip('"') for k, v in LOG_PAIR.findall(header.group("rest"))}
                pairs.update(service=header.group("service"), event=header.group("event"))
            elif " | " in line:
                styles["pipe table"] += 1
                continue
            else:
                # Some exporters wrap the real key=value fields in an escaped message="..."
                if 'message="' in body:
                    styles["key=value, nested in message"] += 1
                    body = body.replace('\\"', '"')
                elif "=" in body:
                    styles["key=value"] += 1
                pairs = {k: v.strip('"') for k, v in LOG_PAIR.findall(body)}
            keys.update(pairs.keys())
            if "event" in pairs:
                events[pairs["event"]] += 1
        style = ", ".join(s for s, _ in styles.most_common()) or "free text"
        rows_out.append([row["file_id"], f"`{Path(path).name}`", len(text_lines), style,
                         ", ".join(k for k, _ in keys.most_common(12)),
                         ", ".join(f"{e}({n})" for e, n in events.most_common(6))])
    lines += table(["file_id", "File", "Lines", "Format", "Most common keys", "Top events"], rows_out)


def profile_binary(lines: list[str]) -> None:
    lines += ["## 8. PDFs and images", "",
              "Not machine-readable as plain text here; extracted in Phase 3 "
              "(PDFs carry a text layer; PNGs are application screenshots).", ""]
    manifest = read_csv(DATASET_DIR / "evidence_files.csv")
    lines += table(["file_id", "File", "Origin", "captured_at"], [
        [r["file_id"], f"`{r['file_path']}`", r["origin"], r["captured_at"]]
        for r in manifest if r["file_path"].endswith((".pdf", ".png"))])


def profile_resource_ids(lines: list[str], texts: dict[str, str]) -> None:
    lines += ["## 9. Resource ID prefixes", "",
              "Prefix of every `<prefix>_<number>` token in text evidence and activity rows.", ""]
    evidence_counts, activity_counts = Counter(), Counter()
    for text in texts.values():
        evidence_counts.update(m.group(1) for m in RESOURCE_ID.finditer(text))
    for r in read_csv(DATASET_DIR / "system_activity.csv"):
        activity_counts.update(m.group(1) for m in RESOURCE_ID.finditer(r["resource_id"]))
    prefixes = sorted((set(evidence_counts) | set(activity_counts)) - CITABLE_PREFIXES - {"user", "case"})
    lines += table(["Prefix", "Mentions in evidence", "Activity resource_id rows"],
                   [[f"`{p}_`", evidence_counts[p], activity_counts[p]] for p in prefixes
                    if evidence_counts[p] + activity_counts[p] >= 3])


def profile_samples(lines: list[str], texts: dict[str, str]) -> None:
    lines += ["## 10. Sample answers", ""]
    gold = read_csv(DATASET_DIR / "sample_output.csv")
    requests = {r["case_id"]: r for r in read_csv(DATASET_DIR / "sample_deletion_requests.csv")}
    manifest = {r["file_id"]: r for r in read_csv(DATASET_DIR / "evidence_files.csv")}
    steps = [(row, s) for row in gold for s in json.loads(row["steps_json"])]
    lines += [f"Statuses: {dict(Counter(r['deletion_status'] for r in gold))}. "
              f"Steps: {len(steps)}; per case min/max: "
              f"{min(len(json.loads(r['steps_json'])) for r in gold)}/"
              f"{max(len(json.loads(r['steps_json'])) for r in gold)}.", ""]
    ops = Counter((s["phase"], s["operation"], s["system_id"]) for _, s in steps)
    lines += table(["Phase", "Operation", "system_id", "Steps"],
                   [[p, f"`{o}`", f"`{sy}`", n] for (p, o, sy), n in sorted(ops.items())])

    cited = [(row, s) for row, s in steps if s["evidence_id"]]
    kinds = Counter(s["evidence_id"].split("_")[0] for _, s in cited)
    after = sum(manifest[s["evidence_id"]]["captured_at"] > requests[row["case_id"]]["audit_at"]
                for row, s in cited if s["evidence_id"] in manifest)
    latest_hits = latest_total = absent_hits = absent_total = 0
    for row, s in cited:
        if s["evidence_id"] not in texts:
            continue
        audit_at = requests[row["case_id"]]["audit_at"]
        containing = [f for f, t in texts.items()
                      if s["resource_id"] in t and manifest[f]["captured_at"] <= audit_at]
        latest = max(containing, key=lambda f: manifest[f]["captured_at"]) if containing else None
        if s["operation"] == "VERIFY_RECORD_ABSENT" and s["resource_id"] not in texts[s["evidence_id"]]:
            absent_total += 1
            absent_hits += 1
        else:
            latest_total += 1
            latest_hits += latest == s["evidence_id"]
    lines += [
        f"Cited evidence kinds: {dict(kinds)}. Cited evidence captured after the case's "
        f"audit_at: {after}.",
        f"Citation rule check: for {latest_hits}/{latest_total} citations of a record that is "
        f"mentioned in the cited file, the cited file is the latest text evidence captured at "
        f"or before audit_at that mentions the resource_id. {absent_hits}/{absent_total} "
        f"VERIFY_RECORD_ABSENT citations point to a file that does not mention the resource "
        f"(absence proven by a snapshot that lacks the row).",
        "",
    ]


def main() -> int:
    lines = ["# Data profile", "",
             "Generated by `python code/tools/profile_dataset.py` from `dataset/`. "
             "Do not edit by hand; see `data_assumptions.md` for interpretation.", ""]
    profile_csvs(lines)
    profile_requests(lines)
    profile_activity(lines)
    texts = profile_evidence(lines)
    profile_sql(lines)
    profile_json(lines)
    profile_text_logs(lines)
    profile_binary(lines)
    profile_resource_ids(lines, texts)
    profile_samples(lines, texts)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"wrote {OUT_PATH} ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
