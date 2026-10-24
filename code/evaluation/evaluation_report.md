# Evaluation report: Ghost Data Still Exists deletion audit

Every figure here comes from commands run on 2026-10-24 against the final code. Commands are in section 6.

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

| Metric | Final (approved policy) | Without P1/P3 (for comparison) |
|---|---|---|
| **Case-level status accuracy** | **100.00%** (10/10 COMPLETE, 22/22 INCOMPLETE) | 96.88% |
| **Step-level precision / recall / F1** | **99.42% / 89.12% / 93.99%** | 99.39% / 84.46% / 91.32% |
| Discovery precision / recall / F1 | 99.42% / 89.12% / 93.99% | 99.39% / 84.46% / 91.32% |
| Evidence accuracy (matched steps) | **100.00%** | 100.00% |
| **Field-level accuracy** (all fields) | **79.23%** | 72.97% |
| Fields: step_no / phase / evidence_id | 88.7% / 88.7% / 88.7% | 84.0% each |
| Fields: system_id / resource_id | 70.6% / 70.6% | 62.4% |
| Fields: operation / state | 73.7% / 73.7% | 67.0% |
| **Sequence accuracy** (= exact cases) | **53.12%** (17/32) | 34.38% |
| Order accuracy when the steps match | 100.00% | 100.00% |

**Reading the numbers:**
- **Precision is 99.4%.** Of 173 predicted steps, one is not in the answers; 172 of the 193 expected steps are produced.
- **Recall is 89.1%.** Missing steps are verifications of records the evidence doesn't connect to the person.
- **Every finding and remediation step in the answers is produced, with the official evidence.**
- **No predicted citation is wrong.**
- Field-level accuracy is lower than step-level F1 because a missing step shifts every later position in that case.

**Every remaining difference** (15 cases; the evaluator's `--details` output):

| Cases | Difference | Root cause |
|---|---|---|
| case_03, 21, 25, 26, 27, 28 | missing VERIFY_RECORD_ANONYMIZED on an analytics record | The anonymized record carries no identifier, so no evidence ties it to the person |
| case_04 | missing VERIFY account ABSENT and analytics ANONYMIZED | The account appears in no evidence or activity; analytics has no identifier |
| case_21, 25, 28 | missing VERIFY account ABSENT | The 404 lookups carry no identifier; several activity candidates, so the single-candidate rule doesn't apply |
| case_27 | missing VERIFY `account_3866` ABSENT | Only evidence dated after `audit_at` links it, and later evidence may not change the answer |
| case_18, 20, 22, 23, 29, 31, 32 | missing VERIFY account ABSENT | Account known only from the activity log, with several competing candidates |
| case_18, 20 | missing VERIFY_RECORD_PRESENT `audit_record_6645` / `5569` | These records appear in no evidence file or activity row |
| case_17 | **extra** VERIFY `account_6523` ABSENT | Linked by a "not found" lookup on the subject's email in March. A lookup-only link, which may only verify absence. |

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

**Effect on the official samples:**
- The first end-to-end run had 96.88% status accuracy and F1 91.01%.
- The final run has **100% status accuracy and F1 93.99%**, with evidence accuracy at 100% throughout.

## 5. Full evaluation set (518 requests, no answers available)

- **Rows:** 161 COMPLETE, 347 INCOMPLETE, 10 NOT_APPLICABLE (case_533 to case_542).
- **Steps:** 2,804 in total: 705 findings, 705 remediations, 1,394 verifications.
- **Validator:** 0 issues except 7 COMPLETE rows with no steps (case_35, 50, 55, 61, 63, 416, 417).
  - For these, no evidence-backed step exists under the rules: excluded anomalous screenshots, catalog-only links, an unknown backup restore state, a conflicted owner, or no evidence at all.
  - The official text does not require a COMPLETE row to have steps, so `[]` is used and nothing is invented.
  - Our validator, which is stricter, still flags them. See `code/docs/PHASE_5_DECISION_ENGINE_REPORT.md`.
- **Every cited file** was captured on or before its case's `audit_at`.
- **Re-runs** are byte-identical.

## 6. How to reproduce

```text
python code/main.py --requests sample_deletion_requests.csv --out artifacts/sample_predictions.csv
python code/evaluation/main.py --pred artifacts/sample_predictions.csv --details
python -m pytest code/tests/custom_cases          # team-created cases
python -m pytest code/tests                       # all 370 tests
```
