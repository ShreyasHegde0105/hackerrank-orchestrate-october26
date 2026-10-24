# Evaluation report: Ghost Data Still Exists deletion audit

Every figure here comes from commands run on 2026-10-25 against the final code (after the independent audit and the fixes in `code/docs/AUDIT_RESPONSE.md`). Commands are in section 6.

## 1. What was evaluated and how

- **Labelled data:** the only labelled data are the 32 official sample answers (`dataset/sample_output.csv`). They are used only for scoring and are never read by the pipeline.
- **How predictions were made:** with `python code/main.py --requests sample_deletion_requests.csv`, using the same code and approved policy as the 518-case submission.
- **How they were scored:** by the project evaluator, `code/evaluation/evaluate.py`.

**Metric definitions** (from the evaluator's docstring):

| Metric | Definition |
|---|---|
| Case-level status accuracy | Share of cases whose `deletion_status` matches |
| Step-level precision / recall / F1 | A step is its six fields other than `step_no`. Predicted and expected steps are matched as multisets, micro-averaged. |
| Discovery F1 | The same, on (phase, system, resource, operation) only, so a wrong `evidence_id` doesn't hide a correctly found record |
| Evidence accuracy | Among matched steps that cite evidence, the share with the same `evidence_id` |
| Field-level accuracy | Steps aligned by position; each of the 7 fields compared; missing or extra steps count as wrong |
| Sequence accuracy | Share of cases whose whole ordered step list is identical (here equal to exact-case accuracy) |
| Order accuracy | Among cases with the same set of steps, the share in the same order |

## 2. Results on the 32 official samples

| Metric | Final (approved policy) | Before the audit round | Without P1/P3 |
|---|---|---|---|
| **Case-level status accuracy** | **100.00%** (10/10 COMPLETE, 22/22 INCOMPLETE) | 100.00% | 96.88% |
| **Step-level precision / recall / F1** | **98.92% / 94.82% / 96.83%** | 99.42 / 89.12 / 93.99% | 99.39 / 84.46 / 91.32% |
| Evidence accuracy (matched steps) | **100.00%** | 100.00% | 100.00% |
| **Field-level accuracy** (all fields) | **89.52%** | 79.23% | 72.97% |
| **Sequence accuracy** (= exact cases) | **62.50%** (20/32) | 53.12% | 34.38% |
| Order accuracy when the steps match | 100.00% | 100.00% | 100.00% |
| Structural contract errors | **0** | 1 | 4 |

**Reading the numbers:**
- **Precision is 98.9%.** Of 185 predicted steps, 2 are not in the answers; 183 of the 193 expected steps are produced.
- **No predicted citation is wrong** (evidence accuracy 100%).
- The audit round recovered 11 previously missing steps: workflow-attributed accounts (gold case_01-style verifications with the official snapshot citations) and analytics records, plus the late-linkage rule proven by gold case_27.

**Every remaining difference** (12 cases; the evaluator's `--details` output):

| Cases | Difference | Root cause |
|---|---|---|
| case_03, 25, 26, 27, 28 | missing VERIFY_RECORD_ANONYMIZED on an analytics record | The anonymized record carries no identifier; the only link is the worker's ANONYMIZE event, and several same-batch events stay ambiguous, so nothing is attributed (never guessed) |
| case_09, 13 | **extra** future VERIFY_RECORD_ANONYMIZED | The converse: a forced analytics attribution landed on a case whose answer has none; the premise that every anonymization event belongs to some request does not always hold |
| case_04 | missing VERIFY account ABSENT | `account_004` appears in no evidence or activity row at all |
| case_08, 23 | missing future VERIFY account ABSENT | Several deletion events in the window match the case's status and requested times tie, so no event is attributed |
| case_18, 20 | missing VERIFY_RECORD_PRESENT `audit_record_6645` / `5569` | These record IDs appear in no evidence file or activity row |

## 3. Team-created evaluation cases

Six hand-made scenarios in `code/tests/custom_cases/test_custom_cases.py`. Each runs end to end through the real Phase 2, 4 and 5 code with the approved policy. Each expected row was written by hand from AGENTS.md §6 and `deletion_rules.csv` before running.

| # | Scenario | Expected output | First run | Final |
|---|---|---|---|---|
| 1 | French request: "Je ne demande pas la suppression de mon compte, seulement une copie…" (not a deletion request, in another language) | `NOT_APPLICABLE, []` | **FAIL**: INCOMPLETE, because "suppression" matched and French negations weren't recognised | pass, after the fix in §4 |
| 2 | Request uses an **old** email; a search entry still holds the old email; the account lookup by the new email returns 404 | INCOMPLETE: finding (cited) + delete for `search_1`; verify `account_1` and `search_1` absent | pass | pass |
| 3 | **Evidence after `audit_at`**: entry gone on day 10 (cited file), shown again in a file from day 16 (audit day 15) | COMPLETE: verify `search_1` absent citing the day-10 file | pass | pass |
| 4 | **Sync recreates** a deleted record: gone on day 10, back on day 12 with an active connector | INCOMPLETE: two findings; DISABLE_DATA_FLOW **before** DELETE_RECORD; two verifications | pass | pass |
| 5 | **Replica drift**: primary lookup 404, failover replica still holds the profile | INCOMPLETE: finding cites the replica file, delete, verify | pass | pass |
| 6 | Account gone, security log retained, tombstone keeps only `user_1` | COMPLETE: verify absent, present, redacted, each cited | pass | pass |

**Result: 6 of 6 pass.** Case 1 found a real defect, which was fixed.

## 4. Failures found and improvements made

| Phase | Failure found | Improvement |
|---|---|---|
| 1 | Activity log not in time order; a snapshot covering only some systems; five log layouts | Sort by time; record each snapshot's scope; one parser for all five layouts |
| 2 | Real request emails differing only in capital letters (20); people sharing a name | Case-insensitive matching; names never used for matching |
| 2 | An email changed without its hash changing went unnoticed | Email/hash consistency check |
| 3 | Bundle files hide inner snapshot times | Every fact keeps its own time and the file's capture time |
| 3 | Lookups split the person's identifier and the result across lines | Join by trace ID, refusing traces that mix identifiers |
| 3 | Support records uncategorised; a connector state read from a substring ("on" in "Continuous"); seconds dropped from screen times; future deadlines flagged as anomalies; instruction-like text able to flip a connector's state | Event-based categories; whole-word, status-field-only state rules; seconds parsed; deadline fields excluded |
| 4 | Treating every snapshot as complete wrongly "deleted" 9 records | Absence proven only by explicit lookups or a SQL snapshot of the same copy and kind of data |
| 4 | A primary-database deletion hid a surviving replica copy (case_05) | Per-copy state |
| 4 | Activity timing guessed the wrong account in 17 of 32 cases | Timing never used as identity evidence; at most a single, snapshot-proven candidate (P1) |
| 5 | 4 sample deletion requests read as non-deletion ("deleting", "removed", "… is gone") | Verb forms and "gone" patterns |
| 5 | A non-English "I'm not asking for deletion" read as a deletion (custom case 1) | Negations added for French, Spanish, Portuguese, German, Italian, Japanese and Korean |
| 5 | An UNKNOWN account with no category lost its future check (case_06) | Category falls back to the record-ID prefix |
| 5 | The activity-candidate rule could verify an account using only the event | Requires a citable SQL snapshot after the event showing it absent, and none still holding it |
| audit | Independent audit (see `code/docs/AUDIT_RESPONSE.md`): P1 linked an account by timing alone; absence proof unscoped; 7 invalid empty COMPLETE rows; string timestamp comparison; CLI exit 0 despite issues; traceback on missing input | Global forced request/event matching with status-result and ownership constraints (validated: 26/26 attributed sample accounts match gold); scoped copy/collection snapshot proof; syslog trace-join fix turned case_416/417 into cited INCOMPLETE rows; error/warning validator severities with the CLI failing on errors; instant-based timestamp comparison; clean CLI errors |
| audit | P4-1 temporal ambiguity | Resolved from gold case_27: late files may link records to the subject, facts at or before `audit_at` decide state, later evidence never changes the answer, citations never postdate `audit_at` |

**Effect on the official samples:**
- The first end-to-end run had 96.88% status accuracy and F1 91.01%.
- Before the audit round: 100% status accuracy, F1 93.99%.
- The final run has **100% status accuracy and F1 96.83%**, with evidence accuracy at 100% throughout and 0 structural contract errors.

## 5. Full evaluation set (518 requests, no answers available)

- **Rows:** 159 COMPLETE, 349 INCOMPLETE, 10 NOT_APPLICABLE (case_533 to case_542).
- **Steps:** 3,195 in total: 716 findings, 716 remediations, 1,763 verifications.
- **Validator:** 0 contract errors. 5 warnings — COMPLETE rows with no steps (case_35, 50, 55, 61, 63):
  - For these, no evidence-backed step exists under the rules: lookup-only links that may not verify a present record, UNKNOWN states, a catalog-only link, an uncitable search entry, and one record with a real ownership conflict (case_63).
  - The official text does not require a COMPLETE row to have steps, so `[]` is used and nothing is invented. Our validator, which is stricter, reports them as warnings (`code/docs/AUDIT_RESPONSE.md`, AUDIT-003).
  - The audit round turned the two other previously empty rows (case_416, 417) into INCOMPLETE with cited findings after the syslog trace-join fix.
- **Every cited file** was captured on or before its case's `audit_at`.
- **Re-runs** are byte-identical. `output.csv` SHA-256 `ac85935e3dd2c0bf2a34814c49c964bce819160edbfcc0da30e9ea32902efb86`.

## 6. How to reproduce

```text
python code/main.py --requests sample_deletion_requests.csv --out artifacts/sample_predictions.csv
python code/evaluation/main.py --pred artifacts/sample_predictions.csv --details
python -m pytest code/tests/custom_cases --basetemp artifacts/pytest-tmp/cc   # team-created cases
python -m pytest code/tests --basetemp artifacts/pytest-tmp/all               # all 393 tests
# --basetemp avoids a host ACL on pytest's default temp directory (AUDIT-ENV-001)
```
