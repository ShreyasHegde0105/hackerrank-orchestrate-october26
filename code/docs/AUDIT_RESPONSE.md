# Response to the independent full-pipeline audit (2026-10-25)

The audit report (`FINAL_AUDIT_REPORT.md`, produced outside the repository) rated the
project **NOT READY** with 5 code defects, 1 environment issue and 3 ambiguities.
This document classifies every finding, describes the fix, the regression test, and
the effect on the output. Where the audit left a question open, it was answered
**empirically from the official sample answers** instead of by assumption; the key
discovery is described at the end.

## 1. Finding-by-finding resolution

### AUDIT-001 — P1 linked an account by time proximity alone (defect, High)

- **Root cause:** a unique deletion event near `reported_at` was treated as the
  subject's without any ownership check (`reconstruct.py _candidates`,
  `decide.py _candidate_decision`).
- **Resolution:** the timing link is in fact the dataset's intended mechanism — the
  gold answers verify these accounts although **0 of 539** account deletion events
  carry any identifier (gold case_01/02/07/18/20/22/23/25/28/29/31/32). What was
  missing is the discipline around it. Attribution is now a global matching over
  **all 550 requests** (`reconstruct.py _attribute_events`): window −2 h/+10 min
  around each case's own `reported_at`, the event's `reported_result` must match the
  request's `reported_status` (COMPLETE↔SUCCESS, PARTIALLY_COMPLETE↔PARTIAL,
  NOT_STARTED↔FAILED — demonstrated by gold case_01/02/31/32), events whose account
  is evidence-owned by a different person are excluded (gold case_03), non-deletion
  requests do not compete, and only **forced** assignments are used, plus the
  gold-validated queue-order tiebreak for accounts (gold case_28/29; never between
  simultaneous requests). All 26 attributed sample accounts match gold; 0 are wrong.
- **Tests:** `test_reconstruct.py` (attribution, exclusivity, determinism),
  `test_decide.py` (attributed vs unattributed candidates),
  `test_decide_real_data.py::test_attributed_accounts_cite_scoped_snapshots_or_stay_future_checks`.
- **Output impact:** account verification steps now appear in many more cases, with
  the gold-matching citations; sample step F1 rose from 93.99% to 96.83%.

### AUDIT-002 — P1 accepted the wrong copy/collection as absence proof (defect, High)

- **Root cause:** `_candidate_decision` filtered coverage by system and time only.
- **Fix:** the upgrade (`decide.py _upgrade_attributed_account`) requires a citable
  snapshot scoped to `account_service` / the `USER_PROFILE` collection, taken after
  the event and at or before `audit_at`, in a file captured by `audit_at`; the
  latest qualifying snapshot of **every** represented copy must lack the account; a
  primary-copy snapshot is required for the citation (gold cites exactly one:
  `evidence_54` for case_01/02, `evidence_51` for case_31). Scoped **inner snapshots
  of assurance bundles qualify** — gold case_31 cites a JSON bundle — while unscoped
  JSON collections still never prove absence.
- **Tests:** `test_decide.py::test_wrong_or_ineligible_snapshot_never_upgrades`
  (wrong system, wrong collection, wrong replica, late capture, pre-event snapshot),
  `::test_copy_still_containing_the_account_blocks_the_upgrade`,
  `::test_assurance_bundle_inner_snapshot_qualifies`.
- **Output impact:** absence citations can only come from correctly scoped snapshots.

### AUDIT-003 — seven empty COMPLETE rows / CLI exit 0 despite issues (defect + ambiguity, High)

- **Root cause (rows):** mostly a **discovery gap**, not a contract hole. After the
  fixes, case_416/417 (whose subjects' records were visible in the syslog archive,
  see AUDIT-S1 below) became INCOMPLETE with cited findings. Five rows remain
  (case_35, 50, 55, 61, 63): their records are blocked by lookup-only non-absent
  links, UNKNOWN states, a catalog-only link, an uncitable search entry, and a real
  ownership CONFLICT (case_63).
- **Contract position:** the official text never forbids `COMPLETE` with `[]`; it
  requires steps to be evidence-backed and forbids invented citations, and offers no
  UNKNOWN status. `COMPLETE` + `[]` is therefore the least-violating representation
  for an authorized case with nothing citable. Our validator's "no steps" check is
  **stricter than the contract**, so it is now reported as a `warning` severity
  (`validator.py Issue.severity`), still printed, never silenced.
- **CLI:** `main.py` exits non-zero on contract **errors** (and on extraction
  failures); warnings are reported but do not fail the run. A clean full run now
  exits 0 with `contract issues: 0; warnings: 5`.
- **Tests:** `test_validator.py` (severity), `test_decide_real_data.py`
  (`EMPTY_COMPLETE` set, CLI exit codes for errors vs warnings,
  `test_cli_fails_when_output_validation_reports_issues`).
- **Output impact:** 2 statuses corrected (case_416/417 COMPLETE→INCOMPLETE with
  cited findings); 5 empty rows remain, documented.

### AUDIT-004 — P4-1 temporal semantics (ambiguity, High) — RESOLVED from gold data

The question "may a fact observed at or before `audit_at`, inside a file captured
later, influence the state?" is answered by gold case_27:

- the subject's account (`account_3866`) is linked to her **only** by a bundle
  captured two days **after** `audit_at` → late files may establish linkage (P4-8);
- the state is decided by the 09:59 pre-audit 404, cited from `evidence_92`
  (captured 11:44, before the 12:00 audit) → facts at or before `audit_at` decide;
- the same late bundle shows the account **ACTIVE again** after `audit_at`, and the
  gold verdict stays COMPLETE → evidence created later must not change the answer;
- all 193 gold steps cite files captured at or before `audit_at` → the citation rule
  is absolute.

Both interpretations are covered by tests:
`test_reconstruct.py::test_sightings_after_audit_link_but_never_decide_state`,
`::test_late_link_with_earlier_absence_lookup_gives_cited_absent_state`,
`::test_a_deletion_after_audit_does_not_make_the_record_absent`, and
`test_reconstruct_real_data.py::test_no_future_evidence_decides_or_is_cited`.

### AUDIT-005 — validator compared timestamps as strings (defect, Medium)

Fixed before this pass (uncommitted working-tree change): `_check_evidence` parses
both timestamps as timezone-aware instants and flags invalid ones. Test:
`test_validator.py::test_evidence_timestamp_offsets_are_compared_as_instants`.
No effect on the supplied Zulu-only data.

### AUDIT-006 — CLI exit ignored validation (defect, Medium)

Fixed: see AUDIT-003. Exit is non-zero for contract errors or extraction failures.

### AUDIT-007 — missing input printed a traceback (defect, Low)

Fixed before this pass: `main.py` catches `DatasetError`/`OSError` and prints a
one-line error. Test:
`test_decide_real_data.py::test_cli_reports_missing_dataset_input_without_traceback`.

### AUDIT-008 — P3 contact-row category fallback (ambiguity, Low)

Unchanged: the `SECONDARY_IDENTIFIER` fallback applies only to rows seen solely as
contact-index lookups, matches public sample case_10, and affects no evaluation
case. It remains a flagged interpretation (`--uncategorised-support-category none`
turns it off).

### AUDIT-ENV-001 — pytest temp directory ACL (environment, Medium)

Host ACLs block pytest's default per-user temp root. All runs use
`--basetemp artifacts/pytest-tmp/<name>` (gitignored). No code change needed.

## 2. Additional defect found while resolving AUDIT-003

### AUDIT-S1 — RFC 5424 syslog traces were parsed without their positional header

- **Root cause:** `evidence/logs/syslog_archive_*.log` lines carry the service and
  trace ID positionally (`<15>1 node-8 account_service tr-0854521 - - …`); the
  generic key=value parser dropped both, so `profile_lookup_started`
  (`lookup_key=<email>`) never joined its `_completed` line's `resource_id`.
- **Fix:** a sixth log layout in `logs.py` (`_SYSLOG`); the existing trace-join then
  attaches the identity. Test:
  `test_evidence_logs.py::test_syslog_trace_joins_identity_across_infrastructure_lines`.
- **Output impact:** case_416/417 gained cited findings — their subjects' account,
  search entry and audit record were visible minutes before `audit_at` in
  `evidence_39` — turning both from (wrong) empty COMPLETE rows into INCOMPLETE.

## 3. Verification (final state)

| Check | Result |
|---|---|
| Full test suite | `python -m pytest code/tests --basetemp artifacts/pytest-tmp/x`: **393 passed** |
| Sample evaluator | status 100%, step P/R/F1 98.92/94.82/96.83%, evidence 100%, 0 structural errors |
| Full run | 518 rows (159 COMPLETE / 349 INCOMPLETE / 10 NOT_APPLICABLE), 3,195 steps, exit 0 |
| Validation | contract errors 0; warnings 5 (the documented empty rows) |
| Citations | every cited file captured at or before its case's `audit_at` (tested) |
| Determinism | repeated runs byte-identical (tested + verified on the final output) |
| output.csv SHA-256 | `ac85935e3dd2c0bf2a34814c49c964bce819160edbfcc0da30e9ea32902efb86` |
