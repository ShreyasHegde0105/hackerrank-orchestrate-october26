# Evidence JSONL schema (version 1)

Written by `python code/tools/export_evidence.py` (default
`artifacts/evidence_observations.jsonl`) and by `audit.evidence.export.write_jsonl`.
Each line is one observation: one thing one evidence file says about one
record at one moment.

**Serialisation:**
- UTF-8 with `\n` line endings, one JSON object per line.
- Keys sorted, compact separators, non-ASCII written as-is (not escaped).
- Lines ordered by evidence file number, then `locator`.
- The same inputs always produce a byte-identical file.
- `read_jsonl` rebuilds `Observation` objects equal to the originals.

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int | Always `1` for this format. |
| `obs_id` | string | `<evidence_id>#<locator>`; unique across the export. |
| `evidence_id` | string | `file_id` from `evidence_files.csv`. |
| `source_kind` | string | `sql`, `json`, `log`, `txt`, `pdf` or `png`. |
| `locator` | string | Where in the file: e.g. `line 42`, `snapshots[3].records[0]`, `customer_profiles[1]`, `page 2 line 5`, `records[0]`. |
| `observed_at` | timestamp | When the fact was true, from the most specific source time (see `time_basis`). |
| `time_basis` | string | `item`, `inner_snapshot`, `log_line`, `sql_capture`, `document` or `manifest`. |
| `file_captured_at` | timestamp | The manifest `captured_at` of the file: the date a citation of this evidence carries. |
| `system_id` | string or null | System the record belongs to, e.g. `account_service`. |
| `resource_id` | string or null | Record ID, e.g. `account_005`; null when the source shows the record only by a label. |
| `data_category` | string or null | `deletion_rules.csv` category (or `DATA_FLOW`), null if the source does not say. |
| `category_basis` | string | Why that category was chosen: label, content, event, collection, prefix, screen description, or "undetermined: ...". |
| `condition` | string | `PRESENT`, `ABSENT`, `ACTIVE`, `DISABLED`, `RESTORE_ALLOWED`, `RESTORE_BLOCKED` or `MENTIONED`. |
| `exposure` | string | `IDENTIFIABLE`, `REFERENCE_ONLY`, `UNLINKED` or `UNKNOWN`. |
| `identity_refs` | array | Identifiers seen with the record: `{kind, value (normalised), raw, field, basis}`. `basis` is `same_record`, `trace:<id>`, `query` or `same_document`. Names are never refs. |
| `related_resources` | array of string | Other resource IDs the record points to (e.g. a search document's account). |
| `labels` | array of string | Non-ID references: file names, ticket, order or statement numbers. |
| `replica` | string or null | Copy the row came from: `primary`, `read_replica`, `replica_eu_west`, `failover_replica`, ... |
| `collection` | string or null | Table, collection or export section. |
| `extractor` | string | Parser that produced it, e.g. `logs/profile_lookup_completed`, `png/manual`. |
| `confidence` | string | `parsed`, `manual_transcription` (screenshot transcribed by hand) or `ai_extracted` (vision model). |
| `attributes` | object | Every other scalar field of the source record, unchanged (e.g. `retention_until`, `enabled`, `trace_id`, `event`). |

Timestamps use the form `YYYY-MM-DDTHH:MM:SSZ` (UTC).

**Not in the export:**
- Coverage records (which collections a file fully captured).
- Extraction issues.

Both are available in memory on `ExtractionResult`, and the issues are summarised in `code/docs/evidence_report.md`.
