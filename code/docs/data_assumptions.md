# Data assumptions, ambiguities and risks (Phase 1)

Companion to the generated `data_profile.md`. Each item states what was observed,
how we will treat it, and how confident we are. Items marked **OPEN** must be
resolved by evidence in a later phase, not by guessing.

## Confirmed facts

| # | Observation | Consequence for the design |
|---|---|---|
| F1 | All timestamps in all CSVs are `YYYY-MM-DDTHH:MM:SSZ` (UTC). | Times compare correctly as plain strings; we still parse them once into one type. |
| F2 | 518 evaluation cases (`case_33`..`case_550`) and 32 samples (`case_01`..`case_32`) do not overlap. | Samples are a development set only. |
| F3 | Every request email resolves to a known user: 418 by current email, 100 by a historical email only (samples: 29 / 3). No email is owned by two users. | Identity resolution is exact; an unresolved email is a bug, not a data case. |
| F4 | `user_audit_log.csv` changes email (107), email_hash (107) and phone (92) for 196 users. | Collect all old values; old hashes matter: sample case_12 is found only through an old email hash. |
| F5 | Emails appear in mixed case in evidence (e.g. `JULES-LAURENT@PROTON.ME`). | Compare emails case-insensitively. |
| F6 | `system_activity.csv` is not in time order (10 adjacent rows go back in time). | Always sort activity by `event_time`, never by file position. |
| F7 | Activity `reported_result` is SUCCESS 2914 / FAILED 721 / PARTIAL 376. | Results are claims; state comes from evidence. |
| F8 | Manifest and disk agree: 94 files (24 SQL, 28 JSON, 26 log, 1 txt, 4 PDF, 11 PNG). | The manifest is a complete index. |
| F9 | SQL files run cleanly in SQLite. Inventory snapshots carry a `snapshot_scope` table listing which systems were captured; `customer_data_inventory_20260905` covers only `account_service` and has no rows. | A snapshot proves absence only for systems in its scope. |
| F10 | Replica snapshots store records as `record_json` with `origin_system` and `collection_name`; primary and replica row counts differ (drift). | Compare primary vs replica per resource; a row only in a replica is a remaining copy. |
| F11 | Logs come in five syntaxes (plain key=value; key=value nested in `message="..."`; pipe header; bracket header; timestamp + JSON object). The subject identifier field varies: `lookup_key`, `match_key`, `correlation_key`, `subject_ref`, `subject_reference`, `customer_reference`. | One log parser with five front-ends, normalised to the same fields. |
| F12 | Two log files (`notification_delivery`, `search_index_diagnostic`) and several JSON files share a "record lookup" shape: `system, resource_id, record_type, result, subject_reference`. `record_type` names the data category directly. | Prefer the evidence's own category label when present. |
| F13 | Observed condition vocabulary: FOUND / NOT_FOUND, IDENTIFIABLE / REDACTED / ANONYMIZED, ACTIVE / DISABLED / BLOCKED, `enabled` 0/1, `restore_allowed` 0/1. | Normalised condition enum in Phase 3. |
| F14 | PDFs have a text layer (verified by reading `legal_retention_register_20260529.pdf`). PNGs are application screenshots with readable tables. | PDFs: deterministic text extraction. PNGs: vision model, cached once. |
| F15 | Sample answers cite only manifest files (`evidence_*`), never activity or audit IDs, and never a file captured after `audit_at`. | Our citations follow the same pattern unless evidence demands otherwise. |
| F16 | Citation rule, checked on all 75 sample citations: 70/70 citations of a mentioned record point to the latest text file captured at or before `audit_at` that mentions the resource; 4/4 `VERIFY_RECORD_ABSENT` citations point to a covering snapshot that lacks the row; 1 citation is a PDF/PNG. | This is the citation algorithm for Phase 4. |
| F17 | Verification in samples covers every relevant resource: already-absent accounts, retained security records (`VERIFY_RECORD_PRESENT`, 24 of 32 cases), plus each remediated record and process. | The planner verifies the whole final state, not just the fixes. |
| F18 | `email_hash` = SHA-256 of the lower-cased email for all 850 current and 107 historical emails; every email change is paired with a hash change at the same instant. | A request email's hash is computed and matched; an unpaired change is reported as a conflict. |
| F19 | 20 evaluation request emails differ from the stored value only by letter case (e.g. `CHOI.JIHOON94@GMAIL.COM`). | Case-insensitive matching is required by real data, not just defensive. |
| F20 | Three names belong to two users each (Gabriel Costa, Mateo Ríos, Thandiwe Ndlovu). | Names are display-only and never matched. |
| F21 | 12 `LINK_RESOURCE_IDENTITY` rows link a resource to an email and user_id (all consistent). Two phrasings exist; one ("belongs only to ... no other subject is linked") states attachment exclusivity, rule_06's condition. | Parsed as trusted ResourceLinks with an `exclusive` flag. |

## Ambiguities

| # | Question | Current decision | Status |
|---|---|---|---|
| A1 | "Sort rows by case_id": evaluation IDs are not zero-padded, so numeric order (`case_33`, ..., `case_100`) differs from string order (`case_100` before `case_33`). | Numeric order, matching `deletion_requests.csv`. The grader joins on `case_id`, so either order should score the same. | Decided, low risk |
| A2 | Data category is not always the system: sample case_12 deletes an `analytics_platform` record that is a contact lookup with `email_opt_in=true` (marketing-type data), while other analytics records are anonymized. | Implemented in Phase 3 (`evidence/classify.py`): record_type label, then content (email_opt_in, attachment file, message text, event), then collection, then prefix. `analytics_9914` is MARKETING_PROFILE. Policy mapping is confirmed in Phase 5. | Implemented; confirm in Phase 5 |
| A3 | The legal retention register PDF lists orders for `user_08`, `user_12`, `user_17`, `user_21`, `user_24`, `user_29`, but sample case_29 (user_29) has no legal-hold step. | A legal hold creates a step only when a `legal_service` `legal_hold_*` resource is linked to the subject and `retained_until` > `audit_at`. The register maps to no such resource. See `investigations/case_29_legal_hold.md`. | **Resolved** |
| A4 | Activity-only prefixes (`cache_`, `email_delivery_`) never appear in evidence files; evidence-only prefixes include `delivery_confirmation_` and `customer_export_`. | Activity alone may not prove a cache or delivery-log record exists; decide from samples and coverage. | **OPEN** (Phase 4) |
| A5 | Several hundred log lines concern other people (decoys), including 5-digit account IDs (e.g. `account_66777`) and near-identical names. | Match only by exact identifiers and explicit links. | Decided |
| A6 | `CLASSIFY_REQUEST` activity rows label some requests as non-deletion. | Treat as a hint; the decision comes from `request_text`. | Decided, verify in Phase 5 |
| A7 | No sample shows a `NOT_APPLICABLE` answer, a legal hold, an email-delivery log, a cache entry or a marketing-platform record. | These paths are driven by policy plus evidence, and covered by our own test cases. | Risk accepted |
| A8 | Bundle files hold inner snapshots whose own `captured_at` can be earlier than the manifest time (`evidence_02`: manifest 09-17, inner 09-15 12:00). Sample case_29 verifies `audit_record_3954`, which appears only there. | Phase 3 keeps both times on every observation (`observed_at` from the inner snapshot or item, `file_captured_at` from the manifest). Hypothesis for Phase 4: discover by `observed_at`, cite only files whose manifest time is at or before `audit_at`. | Data preserved; rule decided in Phase 4 |
| A9 | Sample-answer records with no identifier. | Phase 3 trace joins, query refs and related resources link 84 of 111 distinct (case, record) pairs. Of the other 27, 17 appear in no evidence file at or before `audit_at` (activity log only) and 10 exist only in anonymized form. A timing rule ("account deletion nearest `reported_at`") was measured: right 8, ambiguous 6, wrong 17, none 1 of 32, because requests share report times. So timing is **not** used as identity evidence. A narrow variant (exactly one candidate) was right 8/8 and is left for a Phase 4 decision. See `evidence_report.md` §5 and §5b. | Investigated; Phase 4 decision |
| A10 | Two ledger items are dated after their file's capture (`evidence_13` record dated 08-14 in a file captured 08-12; `evidence_36` record dated 04-11 in a file captured 04-09). | Kept as observations and flagged as warnings. Proposed for Phase 4: such items are not trusted as evidence. | **OPEN** (Phase 4) |
| A11 | Screenshot `marketing_profile_console.png` (seen during design) shows `marketing_001` holding Soo-jin Park / `sj.park@naver.com` (an unknown person), while catalog link `activity_4008` says `marketing_001` belongs to Kwame Mensah (`user_09`, evaluation case_63). | Both are kept as separate observations; the conflict is resolved by policy in Phase 4/5, not in extraction. Needs the vision transcription to be produced first. | **OPEN** (Phase 4/5) |
| A12 | `support_002` (case_10) is a `legacy_contact_index` row in a support replica: neither a message nor an attachment by content. The sample answer deletes it. | Category left undetermined in extraction (with basis); decided by policy in Phase 5. | **OPEN** (Phase 5) |
| A13 | Screenshots need a vision model; no Anthropic credentials are configured in this environment. | Transcriber built with cache, schema-constrained output and fallback handling; the 11 PNGs are reported PENDING_AI until credentials are provided. | **BLOCKER** for screenshot evidence |

## Risks

1. **Over-fitting to 32 samples**: they contain no NOT_APPLICABLE case. Mitigation: rule-based logic and custom test cases (Phase 6).
2. **Missed records under old identifiers or hashes**: mitigation is the full identifier set (F4) plus resource-link following.
3. **Leakage of post-`audit_at` evidence**: mitigation is a hard filter, plus a validator check that already fails any citation after `audit_at`.
4. **Vision extraction errors on 11 PNGs**: mitigation is a schema-constrained output, cross-checking IDs against other evidence, and a cache reviewed by hand.
