# Phase 5: Decision Engine and Official Output — Engineering Report

Status: **COMPLETE**, with one documented contract caveat (P2, section U).

## U. Update: approved configuration (2026-10-24)

You approved P1 and P3 with safeguards, and asked for P2 to be investigated. Sections A–J below describe the first round. Where they differ, this section supersedes them.

**P1, activity candidates. On by default (`APPROVED` in `decide.py`).**
- A candidate is used only when all of these hold:
  - it is the **single** account deletion within −2 h/+10 min of `reported_at`;
  - no account is known from evidence;
  - a **citable SQL snapshot** (captured on or before `audit_at`) taken after the event shows it absent;
  - **no** such snapshot still contains it.
- The activity event is never treated as proof. Without a qualifying snapshot, the candidate isn't used, not even as a future check.
- The full reasoning is written to the diagnostics, e.g. "rule_01 via activity candidate activity_3094: the only deletion_worker account deletion within −2h/+10min of reported_at (… reported PARTIAL); no account known from evidence; absent from SQL snapshot evidence_X".
- **Used in 10 evaluation cases; 0 rejected by the safeguards.**

**P3, legacy-contact rows. On by default, `SECONDARY_IDENTIFIER` (rule 2, DELETE).** Confirmed against policy and data:
- `support_002`'s query came from `source=legacy_contact_index`. It holds no conversation, which rule 5 (REDACT) requires, and no file, which rule 6 requires.
- The platform's own deletion worker issued **DELETE** for it (`activity_3596`). That's a claim, but it's consistent with deletion.
- The option now applies **only** to support rows seen solely as contact-index lookups, never to case-archive rows such as `support_5914`.
- **Applies to 0 evaluation cases** (only sample case_10).

**P2, seven COMPLETE rows with no steps. Investigated; unresolved by design.**

| Case | Record | Why there is no evidence-backed step |
|---|---|---|
| case_35 | `search_428` | Only evidence: the anomalous screenshot `evidence_09` (P4-4) plus a catalog link |
| case_50 | `analytics_7438` | Only evidence: the anomalous screenshot `evidence_19` plus a catalog link |
| case_55 | `backup_9287` | Screenshot shows no restore permission, so the restore state is unknown |
| case_61 | `object_19` | Linked only by a catalog link (the screenshot shows a file name only) |
| case_63 | `marketing_001` | CONFLICT |
| case_416, case_417 | — | No evidence; 4 competing activity candidates |

- **The samples follow the same rules:**
  - case_17 leaves out `tombstone_17`, whose only evidence is the anomalous screenshot `evidence_42`.
  - case_07 leaves out the catalog-only `object_19`.
- **Contract check:**
  - AGENTS.md §6.3 / problem_statement: "For COMPLETE, include only verification steps proving the current state". No minimum number of steps is stated; `[]` is spelled out only for NOT_APPLICABLE.
  - The "COMPLETE case has no steps" check comes from this project's stricter Phase 1 validator (`validator.py:81`). It is kept, not weakened.
  - INCOMPLETE without a finding would break §6.3 outright.
- **Conclusion:** `COMPLETE []` is the only representation that invents nothing. It is **not prohibited by the official text**, but **flagged by our validator**.

**Also fixed in this round:**
- Non-English negations ("Je ne demande pas la suppression…") were read as deletion requests. This was found by a team-created evaluation case.
- Negations were added for French, Spanish, Portuguese, German, Italian, Japanese and Korean.
- The real-data classification is unchanged: exactly case_533–542.

**Final figures:**

| Measure | Result |
|---|---|
| Tests | **370 passed**, 0 failed, 0 skipped (285 before Phase 5) |
| `output.csv` | 518 rows: 161 COMPLETE, 347 INCOMPLETE, 10 NOT_APPLICABLE; 2,804 steps (705 findings, 705 remediations, 1,394 verifications) |
| Determinism | Byte-identical re-runs (SHA-256 `5771d729…`) |
| Validator | Only the 7 P2 rows flagged |
| Samples, official evaluator | **100% status; step P/R/F1 99.42% / 89.12% / 93.99%; evidence 100%; field 79.23%; sequence 53.12%** |

**Deliverables:** `code/evaluation/evaluation_report.md`, `code/evaluation/usage_report.md`, and the README setup and run section.

---

*The rest of this report records the first Phase 5 round. Section U above supersedes it where they differ.*

All figures below come from commands executed on 2026-10-24, on top of commit `4aa3b19` plus the uncommitted Phase 4 work. Nothing is committed.

## A. Summary

Phase 5 turns each request into the official row `case_id, deletion_status, steps_json`:

1. **Intent.** Does the request authorise deletion? If not, the row is NOT_APPLICABLE with `[]`.
2. **Rules.** Each record reconstructed by Phase 4 is mapped through its `deletion_rules.csv` action and its state at `audit_at` to findings, remediation and verifications. A record that can't be decided is kept in diagnostics with its reason.
3. **Status and steps.** Any finding makes the case INCOMPLETE; otherwise COMPLETE. Steps are ordered by the contract's sort key and numbered from 1.
4. **Output.** `output.csv` is written at the repository root and validated against the contract right after writing.

Phase 5 calls the Phase 2–4 code (`Reconstructor`, `IdentityIndex`, `extract_evidence`) and doesn't duplicate any of it.

## B. Repository and baseline

- **Branch and tree:** `main`, HEAD `4aa3b19`. Phase 4 files were untracked (uncommitted) at the start.
- **Baseline:** `python -m pytest code/tests` gave **285 passed**.
- **Contract sources:**
  - `AGENTS.md` §6.3–6.6
  - `problem_statement.md`
  - `code/audit/contract.py` (columns, operations, states, order)
  - `code/audit/validator.py`
  - `code/evaluation/evaluate.py` (scoring)
  - `dataset/deletion_rules.csv`
- **Samples:** `dataset/sample_output.csv`, used only for evaluation.

## C. Decision flow and rules applied

**Intent** (`code/audit/intent.py`), rules in order:
1. Explicit negation ("not asking for deletion", "do not close", "without removing", "keep the account", "continue using", …) → not deletion.
2. Marketing-only opt-out → not deletion.
3. A deletion phrase → deletion. English and its verb forms, "… is gone", French, Spanish, Portuguese, German, Italian, Japanese, Korean, Chinese, Russian.
4. Otherwise → not deletion.

Every verdict records the matched phrases.

**Result:** all 32 samples are deletion requests. **Exactly 10 evaluation requests are not:** case_533 to case_542 (phone change, opt-out, data download, login help, surname correction, marketing-only, information request, invoice copy, email change, data copy).

**Record → steps** (`code/audit/decide.py`). The action is read from `deletion_rules.csv`:

| Action (rules) | Phase 4 state | Steps |
|---|---|---|
| DELETE (rules 1, 2, 3, 4, 6, 9, 10) | PRESENT | REPORT_REMAINING_RECORD → DELETE_RECORD → VERIFY_RECORD_ABSENT |
| DELETE | ABSENT | VERIFY_RECORD_ABSENT |
| REDACT (rules 5, 11, 15) | PRESENT + identifiable | REPORT_EXPOSED_PII → REDACT_RECORD → VERIFY_RECORD_REDACTED |
| REDACT | PRESENT + internal ID only / unlinked | VERIFY_RECORD_REDACTED |
| ANONYMIZE (rules 7, 8) | PRESENT + identifiable or internal ID ("still attributable") | REPORT_EXPOSED_PII → ANONYMIZE_RECORD → VERIFY_RECORD_ANONYMIZED |
| ANONYMIZE | PRESENT + unlinked | VERIFY_RECORD_ANONYMIZED |
| RETAIN (rules 12, 13) | PRESENT, retention or hold date (if supplied) after `audit_at` | VERIFY_RECORD_PRESENT |
| RETAIN backup (rule 14) | restore allowed | REPORT_RESTORE_RISK → BLOCK_RESTORE → VERIFY_RESTORE_BLOCKED |
| RETAIN backup | restore blocked | VERIFY_RESTORE_BLOCKED |
| Data flow (process; §6.4) | ACTIVE | REPORT_ACTIVE_DATA_FLOW → DISABLE_DATA_FLOW → VERIFY_DATA_FLOW_DISABLED |
| Data flow | DISABLED | VERIFY_DATA_FLOW_DISABLED |
| REDACT / ANONYMIZE | ABSENT | VERIFY_RECORD_ABSENT |

This mapping reproduces every combination found in the 32 sample answers.

**Status and evidence** (§6.3):
- **INCOMPLETE:** findings cite the Phase 4 citation; remediation and verification use `""`; every decided record is verified.
- **COMPLETE:** verifications only, each citing a file captured on or before `audit_at`.
- **Ordering:** remediation follows the contract's operation order, so data flows are disabled and restores blocked before records are changed.

## D. Phase 4 handoff issues

| Issue | Treatment | Basis | Effect on evaluation output |
|---|---|---|---|
| A. State known, no citable file | In COMPLETE cases, that verification is **left out** ("include only evidence-backed verification steps", §6.3) and listed. In INCOMPLETE cases it stays as a future check (evidence `""` anyway). A finding with no citable file is reported as a diagnostic. | Spec text | 11 verifications left out; 0 uncited findings |
| B. UNKNOWN | Never treated as ABSENT. A DELETE-category UNKNOWN in an INCOMPLETE case becomes a future VERIFY_RECORD_ABSENT, as in sample case_06. Otherwise it's a diagnostic. When Phase 4 has no category for it, the record ID's prefix decides (Phase 3's `classify_category`). | Spec + sample case_06 | 0 UNKNOWN records in the evaluation set |
| C. CONFLICT | No steps; the conflict is kept in diagnostics. | Ownership not established (§6.5) | case_63 `marketing_001` |
| D. Lookup-only links | May only support VERIFY_RECORD_ABSENT, never a finding or remediation. | A lookup that found nothing proves absence for that identifier, not ownership | case_17 `account_6523` adds one step not in the answers |
| E. Activity candidates | Off by default: diagnostic only. **P1** below. | §6.5: activity is a claim | — |
| F. A12 legacy-contact rows | Off by default: no steps. **P3** below. | No rule names the category | case_10 `support_002` (samples only; 0 in the evaluation set) |
| Catalog-only links | No steps (an activity-log claim, not evidence). | §6.5 | 5 records (`object_19` and `tombstone_17` match the sample answers) |
| Expired retention | Not verified as retained; diagnostic. | Rule 12/13 conditions | 0 occurrences (no retention date has passed) |

**Decisions needed from you:**

**P1. Activity candidates.** Should the single account deletion near `reported_at` become a VERIFY_RECORD_ABSENT, citing the latest SQL snapshot taken after the event that lacks the account?
- 8 of 8 consistent with the samples.
- It adds the official account steps for case_01, case_02 (both citing `evidence_54`, as the answers do), 07, 08, 10 and 30.
- It adds 10 steps across the 518 evaluation cases.
- It relies on timing for ownership, which is why it's off by default.

**P2. COMPLETE rows with no steps.**
- 7 evaluation cases have no evidence-backed step: case_35, 50, 55, 61, 63, 416, 417. Today they're `COMPLETE []`, the only form the contract allows without a finding, and the validator flags it.
- case_63 is the one whose only record is the conflicted `marketing_001`.
- The option `--empty-case-status INCOMPLETE` exists, but INCOMPLETE without a finding also breaks the contract, so I recommend keeping COMPLETE.

**P3. Legacy-contact support rows.** Treat them as `SECONDARY_IDENTIFIER` (rule 2, DELETE)?
- This matches sample case_10.
- No evaluation case is affected.

## E. Output

- **Command:** `python code/main.py` writes `output.csv` at the repository root.
  - Options: `--requests`, `--out`, `--diagnostics FILE`, `--use-activity-candidates`, `--empty-case-status`, `--uncategorised-support-category`.
- **Result:** 518 rows (161 COMPLETE, 347 INCOMPLETE, 10 NOT_APPLICABLE), 2,794 steps, in about 3.6 s.
- **Determinism:** byte-identical on re-run (SHA-256 `a20c7138…`).
- **Contract validation:** 0 issues except the 7 `COMPLETE case has no steps` rows (P2).
  - Every cited file is captured on or before its case's `audit_at`.
  - The validator checks file existence and dates on every row.
- **Recommended setting** (P1 on, P3 `SECONDARY_IDENTIFIER`): 2,804 steps, same statuses.

## F. Files

| File | Purpose |
|---|---|
| `code/audit/intent.py` (new) | Deletion-intent rules |
| `code/audit/decide.py` (new) | `DecisionEngine`, `PolicyOptions`, `CaseDecision`, `RecordDecision` |
| `code/main.py` (starter file, previously empty) | Entry point: runs Phases 2–5, writes and validates `output.csv` |
| `output.csv` (generated) | The submission file |
| `code/tests/unit/test_decide.py` (new, 63 tests) | Intent, every rule/state combination, skips, statuses, citations, UNKNOWN, P1–P3, order independence |
| `code/tests/integration/test_decide_real_data.py` (new, 7 tests) | Contract validity, traceable evidence, conflict handling, both sample scores, byte-identical re-runs, the command line |
| `code/docs/PHASE_5_DECISION_ENGINE_REPORT.md` (new) | This report |

**Phase 1–4 implementation files modified: none.** `git diff --stat` shows only `code/main.py` (the starter's empty entry point) and `log.txt`. The evaluator and sample answers are untouched.

## G. Tests

| What | Command | Result |
|---|---|---|
| Baseline | `python -m pytest code/tests` | 285 passed |
| Phase 5 unit | `pytest code/tests/unit/test_decide.py` | 63 passed |
| Phase 5 integration | `pytest code/tests/integration/test_decide_real_data.py` | 7 passed |
| **Full suite** | `python -m pytest code/tests` | **355 passed, 0 failed, 0 skipped** |
| Mutation checks | uncited COMPLETE steps allowed; catalog links accepted; lookup-only findings allowed; intent negation ignored | each caught (2, 3, 1 and 5 tests); restored to 355 |

## H. Official evaluator on the 32 samples

Command: `python code/main.py --requests sample_deletion_requests.csv --out artifacts/s.csv [options]`, then `python code/evaluation/main.py --pred artifacts/s.csv`.

| Metric | Conservative (current) | Recommended (P1 + P3) |
|---|---|---|
| Status accuracy | 96.88% (case_10 → COMPLETE) | **100%** |
| Step precision / recall / F1 | 99.39% / 84.46% / 91.32% | 99.42% / 89.12% / 93.99% |
| Evidence accuracy | 100% | 100% |
| Field accuracy | 72.97% | 79.23% |
| Sequence = exact-case accuracy | 34.38% | 53.12% |
| Order accuracy (same steps) | 100% | 100% |
| Structural issues | 4 empty COMPLETE (case_01, 02, 04, 10) | 1 (case_04) |

- **Findings, remediation and citations:** every sample finding and remediation step is produced, with the official evidence (41 findings).
- **No wrong citations.** Every missing step is a verification.
- **One unexpected step:** case_17 `account_6523` (lookup-only, issue D).

**Every remaining mismatch under the recommended setting.** All are missing verifications of records Phase 4 can't discover (see `PHASE_4_TEMPORAL_RECONSTRUCTION_REPORT.md` §J), except the one extra:

| Case | Expected (missing) | Root cause | Kind |
|---|---|---|---|
| case_03, 21, 25, 26, 27, 28 | VERIFY_RECORD_ANONYMIZED analytics (cite `evidence_94` / `evidence_92`) | Anonymized record carries no identifier | Evidence-limited |
| case_04 | VERIFY_RECORD_ABSENT `account_004` and VERIFY_RECORD_ANONYMIZED `analytics_901` | Account in no evidence or activity; analytics has no identifier | Evidence-limited |
| case_21, 25, 28 | VERIFY_RECORD_ABSENT account (`evidence_92`) | 404 lookups without an identifier; several candidates, so P1 doesn't apply | Evidence-limited |
| case_27 | VERIFY_RECORD_ABSENT `account_3866` | Only evidence dated after `audit_at` links it | Evidence-limited (time rule) |
| case_31 | VERIFY_RECORD_ABSENT `account_4857` (`evidence_51`) | Activity only, several candidates | Evidence-limited |
| case_18, 20, 22, 23, 29, 32 | VERIFY_RECORD_ABSENT account | Activity only, several candidates | Evidence-limited |
| case_18, 20 | VERIFY_RECORD_PRESENT `audit_record_6645` / `5569` | In no evidence or activity | Not in data |
| case_17 | extra VERIFY_RECORD_ABSENT `account_6523` | Lookup-only link from a March 404 by the subject's email | Policy-dependent (issue D) |

**Under the conservative setting, there are additionally:**
- case_01, 02, 07, 08, 30: missing account verification (P1).
- case_10: missing `support_002` finding, remediation and verification, plus the status difference (P3).

**Implementation defects remaining: none known.** Defects found and fixed during this phase are in section I.

## I. Problems found and fixed

| Problem | Root cause | Fix | Verified by |
|---|---|---|---|
| 4 sample deletion requests classified as not deletion | Patterns lacked "deleting" / "removed" and "… is gone" | Verb forms and "gone" pattern added | Intent tests; samples 32/32 |
| case_06 `account_006` future check missing | UNKNOWN record with no observation had no category | Fall back to the record ID prefix (Phase 3's `classify_category`) | `test_unknown_record_without_category_uses_its_id` |
| Integration test imported the evaluator's `main` | `code/main.py` and `code/evaluation/main.py` share a module name | Test loads `code/main.py` by path (no change to shared test setup) | 7 integration tests pass |
| case_63 diagnostic named "catalog link" instead of the CONFLICT | Check order | CONFLICT checked first | `test_unresolved_relationships_never_produce_steps` |
| Earlier estimate "P1 reduces empty COMPLETE rows to 3" | I read truncated output | Corrected: P1 doesn't change the 7 empty rows | Section E |

## J. Assumptions and remaining issues

**New interpretations, each with its specification basis:**
- Catalog links and activity candidates are claims (§6.5).
- COMPLETE contains only cited verifications (§6.3).
- Lookup-only links are limited to verifying absence.
- Retention counts as current when no date is supplied (rules 12/13 conditions; no supplied date has passed).
- Intent rules (§6.5 "access, correction, export, marketing opt-out … is NOT_APPLICABLE").

**Remaining:**
- **Your decisions:** P1, P2 and P3 (section D).
- **Not done yet:** the README run instructions and the `evaluation_report.md` / `usage_report.md` deliverables (AGENTS.md §6.8) belong to the submission phase.
- **Repository state:**
  - Branch `main`.
  - Uncommitted: `code/main.py`, `log.txt`, `output.csv`, plus the untracked Phase 4 and Phase 5 files.
  - `artifacts/` is gitignored.
  - **No commits or pushes were made.**
