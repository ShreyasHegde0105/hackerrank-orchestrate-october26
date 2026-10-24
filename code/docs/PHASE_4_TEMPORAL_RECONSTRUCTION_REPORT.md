# Phase 4: Temporal State Reconstruction — Engineering Report

Status: **COMPLETE.** All mandatory criteria were verified by tests and by the comparison against all 32 sample answers.

**Important qualification:** 27 of 111 sample-answer records cannot be discovered from the evidence under the approved rules. Each is explained in section J. These are limits of the evidence and of rules you chose deliberately; none is an open defect.

All figures below come from commands executed on 2026-10-24, on top of commit `4aa3b19`. The work is uncommitted.

## A. Executive summary

**What Phase 4 does:** for each deletion request, it reconstructs every record linked to the person *as it stood at that request's `audit_at`*. It then keeps two things separate:
- the **state** (PRESENT, ABSENT, UNKNOWN or CONFLICT), with exposure, connector or backup state, and a state per copy;
- the **evidence that may be cited** for that state.

It reuses the Phase 2 identity resolver and the Phase 3 observations, coverage records and lookup index. **No Phase 1–3 file was modified.**

**Verified results:**

| Measure | Result |
|---|---|
| Sample findings (records the answers report as still non-compliant) | 41 of 41 discovered, **41/41 states and 41/41 citations match** |
| COMPLETE-case verifications | 19 of 34 discovered, **19/19 states and 19/19 citations match** |
| Overall (111 expected case/record states) | 84 discovered, **83 states and 60/60 citations match**. The one state difference is a never-observed account, which Phase 4 correctly calls UNKNOWN rather than assuming it absent. |
| Tests | **285 passed** (baseline 241; 44 new), 0 failed, 0 skipped |
| Mutation checks | 4 deliberate rule breaks, each caught |

**Main limitations:**
- 27 sample records are not discoverable:
  - 13 appear only in the activity log
  - 10 carry no identifier
  - 3 appear nowhere
  - 1 is linked only by evidence dated after the audit
- 166 evaluation records have a state but no citable file: their only evidence sits in files captured after the audit (P4-1).

## B. Baseline and repository assessment

- **Branch and tree:** `main`, HEAD `4aa3b19` ("Complete Phase 3 evidence extraction and validation"), working tree clean.
- **Baseline tests:** `python -m pytest code/tests` gave **241 passed**, 0 failed, 0 skipped.
- **Files consumed and left unchanged:**

  | Phase | Files |
  |---|---|
  | 1 | `audit/contract.py`, `audit/validator.py`, `evaluation/evaluate.py` |
  | 2 | `audit/loader.py`, `audit/models.py`, `audit/identity.py`, `audit/evidence_store.py` |
  | 3 | `audit/evidence/*.py` (model, extract, index, duplicates, export, screenshots, …), `llm/*` |

- **Sample answers:** `dataset/sample_output.csv`, 32 cases (22 INCOMPLETE, 10 COMPLETE). They are the only labelled data. They are used **only** by `tools/reconstruction_report.py` and the integration tests to measure the reconstruction. The reconstruction never reads them.
- **Contract:** there is no official Phase 4 output format. The official output is `output.csv`, built in Phase 5. Phase 4's output is the internal `CaseReconstruction` described in section F. The evaluator and output contract are unchanged: evaluator calibration is identical to Phase 1.

## C. Requirements and assumptions

**Requirements:**
- Decide each case at its own `audit_at` (AGENTS.md §6.2).
- "Use only information available at or before `audit_at`; evidence created later must not change the answer".
- "Never cite … an evidence item captured after the case's `audit_at`" (§6.6).
- Treat statuses and activity results as claims (§6.5).
- Plus your brief: per-copy state, no assumed absence, explicit conflicts, state kept separate from citations.

**Assumptions:** recorded in `code/docs/phase4_assumptions.md` (P4-1 to P4-7), approved by you on 2026-10-24.

| Item | Phase 4 resolution |
|---|---|
| A8: inner snapshot times | P4-1: decide state by fact time; cite only files captured ≤ `audit_at` |
| A9: activity-only records | P4-3: unverified activity candidates only |
| A10 / A14: items dated after their capture | P4-4: never decide or cite |
| A11: `marketing_001` link conflict | P4-5: state CONFLICT, relationship CONFLICTED |
| A12: legacy-contact support rows | Deferred to Phase 5 (policy) |
| New | P4-2 absence proof; P4-6 lookup-only links; P4-7 inclusive boundary |

## D. Timestamp model

How each kind of time applies in this repository:

| Concept | Field | Meaning here |
|---|---|---|
| Event / observation time | `Observation.observed_at` (with `time_basis`) | When the fact was true. Phase 3 takes the most specific source: the item's own time, an inner snapshot, a log line, a SQL capture or scope, a document date, else the manifest. Phase 3 doesn't distinguish "event" from "observation" separately because the sources don't. |
| Capture time | `Observation.file_captured_at` (manifest `captured_at`) | When the file was captured: the date a citation carries. |
| Ingestion time | — | Not represented. Extraction is a pure function of the files, so it doesn't affect any decision. |
| Audit time | `DeletionRequest.audit_at` | The per-case cutoff, never the machine clock. |

**Eligibility rules:**

| Rule | Fact may decide state | File may be cited |
|---|---|---|
| T1 / T2 | `observed_at <= audit_at` | also `file_captured_at <= audit_at` |
| T3 | not if `observed_at > file_captured_at`, or the file is an anomalous screenshot | never in those cases |
| T4 | facts after `audit_at` are only counted (`excluded_future`) | — |

- **Boundary:** exactly `audit_at` is eligible.
- **Time zones:** all times are timezone-aware and compared by instant, so `14:00+02:00` equals `12:00Z` (tested).
- **Missing or invalid times:** Phase 3 rejects them at extraction and reports them, so every observation has a valid time. Nothing passes the cutoff by default.
- **Later-captured files:** facts inside them can decide state (P4-1) but can never be cited (T2).
- **What stays out:** a later file's later facts, a deletion after the cutoff and coverage after the cutoff all have no effect (tested).

## E. Identity and resource resolution

For each request:
1. `IdentityIndex.resolve_request` resolves the person (Phase 2). Anything other than RESOLVED stops the case with a diagnostic.
2. `EvidenceIndex.for_person(user_id)` (Phase 3) returns observations whose identifiers belong to that person alone.
3. Each usable observation adds its `resource_id` (rule R1) and its `related_resources` (R2).
4. Trusted catalog links known by `audit_at` add records (R3). Their labels (a file name or statement number) join observations that show a record only by label.

**Each record keeps its strongest link:** record content, then catalog link, then related record, then lookup only.

**Handled explicitly:**
- Observations naming several people are listed in diagnostics and not attributed.
- A case record that also shows another or unknown person gets a CONFLICTED relationship and CONFLICT state (R5).
- Activity-log account deletions near `reported_at` are recorded as unverified candidates (R4).

Names are never used. Unknown identifiers are never linked.

## F. State reconstruction

Each record is reconstructed **per copy** (`primary`, `read_replica`, `failover_replica`, `replica_eu_west`, …):

1. **S1:** the latest usable sighting decides the copy. Sightings at the same instant that disagree make the copy CONFLICT.
2. **S2:** a copy last seen present becomes ABSENT only if a later qualifying SQL snapshot taken by `audit_at` lacks the record: same system, same copy, same kind of data, file captured by `audit_at`. **JSON snapshots and missing evidence never prove absence (P4-2).**
3. **S3:** across copies:
   - any CONFLICT copy → CONFLICT
   - any copy holding the record → PRESENT, with exposure taken from the most exposed such copy
   - every evaluated copy gone → ABSENT
   - nothing usable → **UNKNOWN**
   - a CONFLICTED relationship overrides all of these to CONFLICT

**Output:** `CaseReconstruction` for each case, holding:
- `resources` (one `ResourceState` each)
- `activity_candidates`
- `excluded_future`, `excluded_anomalous`
- `unattributed_mentions`
- `diagnostics`

Each `ResourceState` has:
- `state`, `existence`, `exposure`, `process_state`
- `relationship`, `relationship_basis`, `link_strength`
- `copies` (one `CopyState` each)
- `supporting`, `citation`, `citation_obs`, `competing`
- a plain-language `reason`

## G. Evidence citations

The citation is chosen **after** the state, only from citable evidence (T2 + T3) that supports that state (rule C1):

| State | Citation |
|---|---|
| PRESENT | The latest citable observation that is not an absence |
| ABSENT | The latest citable explicit absence; otherwise the citable SQL snapshot that proved it |
| UNKNOWN, CONFLICT | None. CONFLICT lists the competing observations. |

If the state rests only on later-captured files, the citation is empty and the reason says so (C2).

Provenance is preserved:
- `supporting` and `citation_obs` hold Phase 3 `obs_id`s (`<evidence_id>#<locator>`) or `coverage:<evidence_id>#<locator>`.
- No ID is invented.
- Observations are never modified.

## H. Implementation details

| File | Purpose | Tests |
|---|---|---|
| `code/audit/reconstruct.py` (new) | `Reconstructor`, `CaseReconstruction`, `ResourceState`, `CopyState`, `ActivityCandidate`; rules T1–T4, R1–R5, S1–S3, C1–C2 | `tests/unit/test_reconstruct.py` (34), `tests/integration/test_reconstruct_real_data.py` (10) |
| `code/tools/reconstruction_report.py` (new) | Runs reconstruction for samples and evaluation, compares with the sample answers, explains every undiscovered record, writes `code/docs/reconstruction_report.md` | integration tests import `build`, `compare`, `load_expectations` |
| `code/docs/reconstruction_report.md` (generated) | Full 111-row comparison, extras, evaluation summary | — |
| `code/docs/phase4_assumptions.md` (new) | P4-1 to P4-7 and open items for Phase 5 | — |
| `code/docs/PHASE_4_TEMPORAL_RECONSTRUCTION_REPORT.md` (new) | This report | — |

**Phase 1–3 files modified: none.** `git diff --stat` shows only `log.txt`, the session transcript.

Phase 4 imports Phase 3's public constants `PAST_EVENT_WORDS` and `screen_time` to recognise anomalous screenshots, without changing them.

## I. Test results

| Category | Command | Baseline | Final |
|---|---|---|---|
| Phase 1 tests | `pytest test_loader, test_validator, test_evaluator, test_profile` | 50 passed | 50 passed |
| Phase 2 tests | `pytest test_identity, test_dataset_loader, test_identity_real_data, test_identity_report` | 74 passed | 74 passed |
| Phase 3 tests (7 files) | `pytest test_evidence_* , test_screenshots_index_export_real` | 117 passed | 117 passed |
| Phase 4 unit | `pytest code/tests/unit/test_reconstruct.py` | — | 34 passed |
| Phase 4 integration | `pytest code/tests/integration/test_reconstruct_real_data.py` | — | 10 passed |
| **Full suite** | `python -m pytest code/tests` | **241 passed** | **285 passed, 0 failed, 0 skipped** |
| Mutation checks | 4 breaks (cite later files; JSON proves absence; no copies; future facts usable) | — | caught by 6, 4, 4 and 2 tests respectively; restored to 285 passed |
| Evaluator calibration | `python code/evaluation/main.py --pred dataset/sample_output.csv` | 100% | identical metrics |
| Phase 3 export determinism | `export_evidence.py` run twice, compared | identical | identical |

**What the Phase 4 tests cover:**
- **Time:** before, exactly at and after the cutoff; time-zone offsets; a deletion after the audit; a fact inside a later file; a citation only from an earlier file; items dated after their file; anomalous screenshots.
- **Identity:** two people kept apart; historical identifiers; an unknown person makes a CONFLICT; a two-person observation is not attributed; an unresolved request; catalog links before and after the audit; link strengths; activity candidates.
- **State:** UNKNOWN without a sighting; SQL-proven absence and its citation; 7 kinds of non-qualifying snapshot; per-copy state; absent only when every copy is gone; a recreated record; a same-instant conflict; mentions don't decide; exposure and process state; independent duplicates both support the state.
- **Determinism:** shuffled input order gives the same result.
- **Real data:** findings and COMPLETE checks all match; the overall agreement figures; undiscovered records explained; no future evidence decides or is cited (samples and all 518 evaluation cases); every absence has qualifying evidence; known cases; the `marketing_001` conflict; deterministic re-runs.

## J. Sample-answer comparison

**How expectations are derived** (`reconstruction_report.py` docstring):
- A finding means the state at the audit.
- A COMPLETE-case verification means the proven state, and its evidence is the expected citation.
- An INCOMPLETE-case verification of a record with no finding is a post-remediation check. It's compared, but labelled "verification only", because the answer doesn't say what was observed at the audit.

| Kind | Expected | Discovered | State matches | Citation matches |
|---|---|---|---|---|
| Finding | 41 | 41 | 41 | 41/41 |
| Verified (COMPLETE) | 34 | 19 | 19 | 19/19 |
| Verification only (INCOMPLETE) | 36 | 24 | 23 | — |
| **Total** | **111** | **84** | **83** | **60/60** |

**All rows that don't match** (the other 83 match; the full 111-row table is in `code/docs/reconstruction_report.md`):

| Case | Record | Kind | Expected | Actual | Explanation | Cause |
|---|---|---|---|---|---|---|
| case_06 | account_006 | verification only | ABSENT | UNKNOWN | No sighting at or before `audit_at`; linked only as the search document's account | Rule by design (no assumed absence) |
| case_01, 02, 07, 08, 10, 18, 20, 22, 23, 29, 30, 31, 32 | the case's account (13) | mixed | ABSENT | not discovered | Appears only in the activity log; all 13 are listed as unverified activity candidates (17 of the 27 undiscovered records overall) | Incomplete evidence; P4-3 |
| case_03, 04, 21, 25, 26, 27, 28 | analytics records (7) | verified (COMPLETE) | PRESENT + UNLINKED | not discovered | Anonymized: the evidence carries no identifier | Incomplete evidence |
| case_21, 25, 28 | `account_6440`, `account_3136`, `account_5482` | verified (COMPLETE) | ABSENT | not discovered | The 404 lookups carry no identifier | Incomplete evidence |
| case_04 | account_004 | verified (COMPLETE) | ABSENT | not discovered | In no evidence file and no activity row | Not in data |
| case_18, case_20 | `audit_record_6645`, `audit_record_5569` | verification only | PRESENT | not discovered | In no evidence file and no activity row | Not in data |
| case_27 | account_3866 | verified (COMPLETE) | ABSENT | not discovered | Only a snapshot dated 09-17 (after the 09-15 audit) links it to user_27 | T4: later evidence can't link |

**Records reconstructed but not in the sample answers (3):**
- `case_07 object_19`: PRESENT, via catalog link `activity_4007` and the storage screenshot label.
- `case_17 account_6523`: ABSENT, a lookup-only link from a March 404 lookup by the subject's email (P4-6).
- `case_17 tombstone_17`: UNKNOWN, because its only screenshot is time-anomalous.

None of the differences comes from an implementation defect. None of the official answers contradicts the specification. The sample answers were not modified and are not read at runtime.

## K. Failures and corrections

| Problem | Root cause | Correction | Verification | Status |
|---|---|---|---|---|
| Throwaway probe missed `support_001` (case_07) | Bug in the throwaway script, not upstream: `EvidenceIndex.for_person('user_07')` returns it | Real implementation written fresh; covered by a test | `test_known_cases` (`support_001` PRESENT, cites `evidence_58`) | fixed (no upstream change) |
| Bundle snapshots made 9 retained or present records "absent" in the naive rule | Bundles are partial extracts | P4-2: only explicit absences and same-copy, same-kind SQL snapshots prove absence | 7-variant non-qualifying snapshot test; real-data absence test | fixed by design |
| `account_005` wrongly absent in the naive rule | Primary deletion hid a surviving failover-replica row | Per-copy state (S1–S3) | `test_state_is_tracked_per_copy`, `test_known_cases` | fixed by design |
| `account_3866` labelled "time anomaly" in the report | The report classifier checked all observations instead of the ones linking the subject | Classifier fixed to check only linking observations | `test_undiscovered_records_are_explained` | fixed |
| `account_6523` (case_17) linked by a 404 lookup but absent from the answers | A lookup that finds nothing is weak ownership evidence | Recorded `link_strength = lookup_only` (P4-6) instead of over-fitting a rule | `test_link_strengths` | documented residual risk |
| Pre-existing failures | — | — | Baseline was 241/241 | none |

## L. Limitations, reproduction and next steps

**Limitations:**
1. **27 of 111 sample records can't be discovered from the evidence** (section J). Expect the same pattern in the evaluation set: the case's account is often known only from the activity log.
2. **166 evaluation records have no citable file.** Their state comes only from later-captured files: 109 security logs, 55 account absences, 2 legal holds. A COMPLETE verdict needs citations, so Phase 5 must handle these.
3. **P4-1 reads AGENTS.md "available at or before `audit_at`" as "existing then".** That matches every sample answer but is an interpretation.
4. **Lookup-only links** (162 records in the evaluation set) are the main way accounts are found. In the samples 9 of 10 match the answers.
5. **Unverified activity candidates:** 22 evaluation cases have a unique candidate. They are never treated as verified.

**Reproduce:**
```text
python -m pytest code/tests                      # 285 tests
python code/tools/reconstruction_report.py       # writes code/docs/reconstruction_report.md
```

`Reconstructor(dataset, IdentityIndex.from_dataset(dataset), extract_evidence(dataset, transcriber=ScreenshotTranscriber.manual())).reconstruct_all()` gives one `CaseReconstruction` per request.

**What Phase 5 needs from Phase 4:**
- For each case: `resources` with state, exposure, process state, category, copies, `citation` and `reason`.
- `activity_candidates`.
- The open items in `phase4_assumptions.md`:
  - how records without a citation affect status
  - whether to use activity candidates and lookup-only links
  - how to treat UNKNOWN records
  - A12

Phase 5 maps each state, through `deletion_rules.csv`, to findings, remediation and verification steps.

**Status: COMPLETE.**
- Every mandatory criterion was verified: temporal correctness, identity reuse, scoped absence, per-copy state, explicit conflicts, traceable citations, unchanged Phases 1–3, full suite 285/285, and the 32-case comparison.
- The remaining differences are explained limits of the evidence or deliberate rules, not defects.
