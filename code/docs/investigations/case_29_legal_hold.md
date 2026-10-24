# Investigation: does the legal-retention PDF create a legal hold for case_29?

Resolves open question A3 in `../data_assumptions.md`. No expected answer was
changed and no case-specific behaviour was added; the outcome is a general rule.

## The question

`evidence_27` (`legal_retention_register_20260529.pdf`) lists an entry for
`user_29`, the subject of sample case_29. The official answer for case_29 has
no legal-hold step. Is the official answer missing a retained record, or does
the PDF not establish a hold?

## Evidence examined

| Item | Value |
|---|---|
| Case | case_29, email `noah.f438085@outlook.com` (historical), resolves to `user_29` via `audit_284` |
| `requested_at` / `audit_at` | 2026-08-27T12:00:00Z / 2026-09-15T12:00:00Z |
| `evidence_27` captured_at | 2026-05-29T08:00:00Z, so it is admissible (before `audit_at`) |
| PDF title and owner | "Disputed orders and tax records register", Finance Operations, classification Internal |
| PDF entry | `ORD-20264600 \| user_29 \| Tax record \| 28 May 2026 \| USD 48.75 \| Retain until 28 May 2033 \| SK` |
| Other PDF entries | `user_08`, `user_12`, `user_17`, `user_21`, `user_24`: all subjects of sample cases |
| Applicable rule | `rule_13`: `LEGAL_HOLD_RECORD`, condition "A supplied legal hold is active", action `RETAIN` |
| Official answer for case_29 | 14 steps over account, search, support, analytics, integration and security services; no `legal_service` step |
| Official answers for case_08/12/17/21/24 | none contains a `legal_service` step |

How legal holds actually appear in this dataset:

- They are records `legal_hold_<n>` in system `legal_service`, collection
  `retained_order_records`, with fields `subject_reference` (a user_id),
  `order_reference` (`ORD-2026<n>`, the same number as the hold), `retention_reason`
  and `retained_until`. Example: `legal_hold_8017`, `user_259`, `ORD-20268017`,
  retained until 2033-12-31, in SQL snapshot `evidence_24`.
- 8 such holds exist in evidence (`legal_hold_438`, `2227`, `4743`, `4887`,
  `4961`, `5622`, `8017`, `8049`), plus 10 `RETAIN_RECORD` activity rows targeting
  `legal_service`. All belong to other users.
- No `legal_hold_4600` and no `ORD-20264600` record exists anywhere: in SQL, JSON,
  logs, text or activity. The same is true for every order in the PDF
  (`4118`, `4207`, `4319`, `4402`, `4511`, `4600`).

## Conclusion

The PDF does **not** establish an active legal hold on any audited resource for case_29:

1. A step needs a `system_id` and a `resource_id`. The register names a finance
   order (`ORD-20264600`), not a record in any audited system, and no
   `legal_service` record links that order or `user_29` to a hold.
2. The register is a finance retention list ("Retain until"), not a statement
   that a legal hold is active on the customer's data.
3. The same holds for all six users in the PDF: none of their six official
   answers contains a legal-hold step. So the official answer for case_29 is
   consistent, not an omission.
4. The finance order record is outside the deletion scope of the audited
   systems. Nothing in it calls for deleting it, and it gives no reason to keep
   other data.

**The PDF is context, probably a deliberate distractor, and produces no steps.**

## General rule adopted (not case-specific)

A `LEGAL_HOLD_RECORD` produces a `VERIFY_RECORD_PRESENT` step only when **all** of these hold:

- a `legal_service` resource (`legal_hold_*`) is observed at or before `audit_at`;
- it is linked to the subject by an exact identifier (`subject_reference` = the
  subject's user_id, or one of their emails, hashes or phones);
- its `retained_until` is after `audit_at`.

A document that only mentions a user and an order, with no such resource,
yields no step. A test for this rule is planned for the policy engine (Phase 5).

## Side finding for Phase 4: snapshot time inside bundle files

`customer_data_snapshot_bundle_20260917.json` (`evidence_02`, manifest
captured_at 2026-09-17, after the audit) contains inner snapshots with their own
`captured_at` of 2026-09-15T12:00:00Z, which equals the audit time. That inner
snapshot is the only evidence of `audit_record_3954`, which the official answer
verifies as PRESENT (with an empty evidence_id, as for any INCOMPLETE case).
The official findings, by contrast, cite `evidence_92` (manifest 2026-09-15T11:44:55Z).

Working hypothesis for Phase 4:

- **Discovery:** a fact's time is its own `captured_at` or `observed_at`, which
  may be earlier than the file's manifest time.
- **Citation:** cite only files whose manifest `captured_at` is at or before
  `audit_at`, as all 75 sample citations do.

To be confirmed against the other samples in Phase 4.
