"""Phase 5: apply the deletion rules to Phase 4 reconstructions and build each
case's output row (case_id, deletion_status, steps_json).

Consumes Phase 4 (CaseReconstruction / ResourceState) and the rules in
deletion_rules.csv; changes nothing upstream. Every record either produces
steps under a named rule or is listed in the case's diagnostics with the reason.

Record -> steps (category action from deletion_rules.csv, state from Phase 4):

  DELETE     PRESENT                     REPORT_REMAINING_RECORD, DELETE_RECORD, VERIFY_RECORD_ABSENT
             ABSENT                      VERIFY_RECORD_ABSENT
  REDACT     PRESENT, IDENTIFIABLE       REPORT_EXPOSED_PII, REDACT_RECORD, VERIFY_RECORD_REDACTED
             PRESENT, internal ID only   VERIFY_RECORD_REDACTED
             or unlinked
  ANONYMIZE  PRESENT, IDENTIFIABLE or    REPORT_EXPOSED_PII, ANONYMIZE_RECORD, VERIFY_RECORD_ANONYMIZED
             internal ID ("attributable")
             PRESENT, unlinked           VERIFY_RECORD_ANONYMIZED
  RETAIN     security log / legal hold   VERIFY_RECORD_PRESENT, while its retention or hold
             PRESENT                     date (if supplied) is after audit_at
  RETAIN     backup, restore allowed     REPORT_RESTORE_RISK, BLOCK_RESTORE, VERIFY_RESTORE_BLOCKED
             backup, restore blocked     VERIFY_RESTORE_BLOCKED
  data flow  ACTIVE                      REPORT_ACTIVE_DATA_FLOW, DISABLE_DATA_FLOW, VERIFY_DATA_FLOW_DISABLED
             DISABLED                    VERIFY_DATA_FLOW_DISABLED
  (A REDACT or ANONYMIZE record already ABSENT gets VERIFY_RECORD_ABSENT.)

Records that produce no steps (kept in diagnostics):
  * linked only by a catalog link: an activity-log claim (AGENTS.md §6.5), not evidence;
  * CONFLICT: ownership or state not established;
  * lookup-only links, except to verify an ABSENT record (never findings or remediation);
  * UNKNOWN, except a DELETE-category record in an INCOMPLETE case, which gets a
    future VERIFY_RECORD_ABSENT check: absence is never assumed;
  * an unknown exposure or restore state, a missing category, or an expired retention.

Case status:
  * the request does not authorise deletion -> NOT_APPLICABLE, [];
  * any finding -> INCOMPLETE: findings (cited), remediation and verification (evidence "");
  * otherwise COMPLETE: only verifications that have a citable file ("evidence-backed",
    §6.3); a record whose state is known but not citable is left out and listed.

Policy options (P1-P3) cover what the specification does not settle; see PolicyOptions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from audit.contract import OPERATION_STATE, step_sort_key
from audit.evidence.classify import classify_category
from audit.evidence.model import Exposure
from audit.intent import IntentVerdict, classify_request
from audit.loader import Dataset, format_timestamp
from audit.models import DeletionRequest
from audit.reconstruct import (
    ABSENT,
    CONFLICT,
    PRESENT,
    UNKNOWN,
    ActivityCandidate,
    CaseReconstruction,
    Reconstructor,
    ResourceState,
)

DATA_FLOW = "DATA_FLOW"  # a process, not a deletion_rules.csv category


@dataclass(frozen=True)
class PolicyOptions:
    """Decisions the specification leaves open (see the Phase 5 report)."""
    # P1: may the single account deletion near reported_at (an activity claim)
    #     become a VERIFY_RECORD_ABSENT, citing a SQL snapshot that lacks it?
    use_activity_candidates: bool = False
    # P2: status for a deletion request with no step at all (nothing found, nothing verifiable)
    empty_case_status: str = "COMPLETE"
    # P3: category for support rows of undetermined category that are seen only as
    #     contact-index lookups (not case-archive rows), or None for no steps
    uncategorised_support_category: str | None = None


# The configuration approved for the submission (P1 on, P3 SECONDARY_IDENTIFIER, P2 COMPLETE).
APPROVED = PolicyOptions(use_activity_candidates=True, empty_case_status="COMPLETE",
                         uncategorised_support_category="SECONDARY_IDENTIFIER")


@dataclass(frozen=True)
class RecordDecision:
    resource_id: str
    system_id: str
    rule: str                       # rule_id or the reason the rule was not applied
    finding: str | None = None
    remediation: str | None = None
    verification: str | None = None
    finding_evidence: str = ""
    verification_evidence: str = ""
    future_check_only: bool = False  # UNKNOWN record: verify after remediation, never cited


@dataclass
class CaseDecision:
    case_id: str
    status: str
    steps: list[dict]
    intent: IntentVerdict
    decisions: list[RecordDecision] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    def row(self) -> dict:
        return {"case_id": self.case_id, "deletion_status": self.status,
                "steps_json": json.dumps(self.steps, separators=(",", ":"), sort_keys=True)}


class DecisionEngine:
    def __init__(self, dataset: Dataset, reconstructor: Reconstructor,
                 options: PolicyOptions | None = None):
        self.dataset = dataset
        self.reconstructor = reconstructor
        self.options = options or PolicyOptions()
        self.actions = {category: rule for category, rule in dataset.rules.items()}

    # --- one record ----------------------------------------------------------------------

    def decide_record(self, case: CaseReconstruction, r: ResourceState) -> tuple[RecordDecision | None, str]:
        """(decision, "") or (None, reason it produced no steps)."""
        if r.state == CONFLICT:
            return None, f"CONFLICT: {r.reason}"
        if r.link_strength == "catalog_link":
            return None, "linked only by a catalog link (an activity-log claim, not evidence)"
        if r.link_strength == "lookup_only" and r.state != ABSENT:
            return None, "lookup-only link may only verify an absent record"
        category = r.data_category
        if category is None:
            # no usable observation stated it: fall back to the record ID (Phase 3's own
            # weakest signal); support_ and object_ IDs stay undetermined there
            category = classify_category(r.resource_id, None, {})[0]
        if category is None and r.system_id == "support_platform" and self._is_contact_index_entry(r):
            category = self.options.uncategorised_support_category
        if category is None:
            return None, "data category undetermined; no rule applies"
        if category == DATA_FLOW:
            return self._data_flow(r)
        rule = self.actions.get(category)
        if rule is None:
            return None, f"no deletion rule for category {category}"
        action = rule.required_action

        if r.state == UNKNOWN:
            if action == "DELETE":
                return RecordDecision(r.resource_id, r.system_id, rule.rule_id,
                                      verification="VERIFY_RECORD_ABSENT", future_check_only=True), ""
            return None, "UNKNOWN state; nothing can be verified"
        if r.state == ABSENT:
            if action == "RETAIN":
                return None, f"{category} must be retained but is ABSENT (no allowed remediation)"
            return self._verify(r, rule.rule_id, "VERIFY_RECORD_ABSENT"), ""

        # PRESENT
        if action == "DELETE":
            return self._violation(r, rule.rule_id, "REPORT_REMAINING_RECORD", "DELETE_RECORD",
                                   "VERIFY_RECORD_ABSENT"), ""
        if action == "REDACT":
            if r.exposure is Exposure.IDENTIFIABLE:
                return self._violation(r, rule.rule_id, "REPORT_EXPOSED_PII", "REDACT_RECORD",
                                       "VERIFY_RECORD_REDACTED"), ""
            if r.exposure in (Exposure.REFERENCE_ONLY, Exposure.UNLINKED):
                return self._verify(r, rule.rule_id, "VERIFY_RECORD_REDACTED"), ""
            return None, "exposure not shown by any usable evidence; cannot tell if redacted"
        if action == "ANONYMIZE":
            if r.exposure in (Exposure.IDENTIFIABLE, Exposure.REFERENCE_ONLY):
                return self._violation(r, rule.rule_id, "REPORT_EXPOSED_PII", "ANONYMIZE_RECORD",
                                       "VERIFY_RECORD_ANONYMIZED"), ""
            if r.exposure is Exposure.UNLINKED:
                return self._verify(r, rule.rule_id, "VERIFY_RECORD_ANONYMIZED"), ""
            return None, "exposure not shown by any usable evidence; cannot tell if anonymized"
        if action == "RETAIN":
            if category == "BACKUP_ARCHIVE":
                if r.process_state == "RESTORE_ALLOWED":
                    return self._violation(r, rule.rule_id, "REPORT_RESTORE_RISK", "BLOCK_RESTORE",
                                           "VERIFY_RESTORE_BLOCKED"), ""
                if r.process_state == "RESTORE_BLOCKED":
                    return self._verify(r, rule.rule_id, "VERIFY_RESTORE_BLOCKED"), ""
                return None, "backup restore state not shown by any usable evidence"
            expiry = self._retention_end(r)
            if expiry is not None and expiry <= format_timestamp(case.audit_at):
                return None, f"retention or hold ended {expiry}; rule {rule.rule_id} no longer applies"
            return self._verify(r, rule.rule_id, "VERIFY_RECORD_PRESENT"), ""
        return None, f"unsupported action {action} for {category}"

    CONTACT_INDEX_EVENTS = {"contact_query_completed", "deletion_marker_check"}

    def _is_contact_index_entry(self, r: ResourceState) -> bool:
        """P3 applies only to support rows seen solely as contact-index lookups (an
        identifier entry: no conversation, no file), never to case-archive rows."""
        index = getattr(self.reconstructor, "index", None)
        if index is None:
            return True  # hand-built test reconstructions carry no evidence index
        observations = index.by_resource(r.resource_id)
        return bool(observations) and all(
            o.attr("event") in self.CONTACT_INDEX_EVENTS and o.attr("source") != "case_archive"
            for o in observations)

    def _data_flow(self, r):
        if r.state != PRESENT:
            return None, f"data flow state {r.state}"
        if r.process_state == "ACTIVE":
            return self._violation(r, "data_flow", "REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW",
                                   "VERIFY_DATA_FLOW_DISABLED"), ""
        if r.process_state == "DISABLED":
            return self._verify(r, "data_flow", "VERIFY_DATA_FLOW_DISABLED"), ""
        return None, "data flow state not shown by any usable evidence"

    @staticmethod
    def _violation(r, rule_id, finding, remediation, verification) -> RecordDecision:
        return RecordDecision(r.resource_id, r.system_id, rule_id, finding, remediation,
                              verification, finding_evidence=r.citation or "")

    @staticmethod
    def _verify(r, rule_id, verification) -> RecordDecision:
        return RecordDecision(r.resource_id, r.system_id, rule_id, verification=verification,
                              verification_evidence=r.citation or "")

    def _retention_end(self, r: ResourceState) -> str | None:
        observations = {o.obs_id: o for o in self.reconstructor.result.observations}
        ends = set()
        for obs_id in r.supporting:
            obs = observations.get(obs_id)
            if obs is None:
                continue
            for name in ("retention_until", "retained_until", "retain_until"):
                value = obs.attr(name)
                if isinstance(value, str) and value:
                    ends.add(value)
        return max(ends) if ends else None

    def _candidate_decision(self, case: CaseReconstruction) -> tuple[RecordDecision | None, str]:
        """P1: the single account deletion near reported_at, verified absent."""
        unique = [a for a in case.activity_candidates if a.unique_in_window]
        if not unique:
            return None, ""
        candidate: ActivityCandidate = unique[0]
        if case.resource(candidate.resource_id) is not None:
            return None, ""  # already decided from evidence
        if any(r.data_category == "USER_PROFILE" and r.link_strength != "catalog_link"
               for r in case.resources):
            return None, (f"activity candidate {candidate.resource_id} not used: the case already "
                          "has an account from evidence")
        # The activity event is a claim: absence must be shown by a citable SQL snapshot of
        # the account service taken after the event, and no such snapshot may still hold it.
        after_event = [c for c in self.reconstructor.sql_coverage.get("account_service", ())
                       if candidate.event_time <= c.observed_at <= case.audit_at
                       and c.file_captured_at <= case.audit_at]
        holding = sorted({c.evidence_id for c in after_event if candidate.resource_id in c.resource_ids})
        if holding:
            return None, (f"activity candidate {candidate.resource_id} not used: SQL snapshots "
                          f"{holding} taken after the deletion event still contain it")
        proofs = [c for c in after_event if candidate.resource_id not in c.resource_ids]
        if not proofs:
            return None, (f"activity candidate {candidate.resource_id} not used: no citable SQL "
                          "snapshot taken after the deletion event shows it absent")
        proof = max(proofs, key=lambda c: (c.observed_at, c.evidence_id))
        rule = self.actions.get("USER_PROFILE")
        rationale = (f"{rule.rule_id if rule else 'USER_PROFILE'} via activity candidate "
                     f"{candidate.activity_id}: the only deletion_worker account deletion within "
                     f"-2h/+10min of reported_at ({candidate.resource_id} at "
                     f"{format_timestamp(candidate.event_time)}, reported {candidate.reported_result}); "
                     f"no account known from evidence; absent from SQL snapshot "
                     f"{proof.evidence_id} ({proof.locator}, {format_timestamp(proof.observed_at)})")
        return RecordDecision(candidate.resource_id, "account_service", rationale,
                              verification="VERIFY_RECORD_ABSENT",
                              verification_evidence=proof.evidence_id), ""

    # --- one case --------------------------------------------------------------------------

    def decide(self, request: DeletionRequest) -> CaseDecision:
        intent = classify_request(request.request_text)
        if not intent.authorises_deletion:
            return CaseDecision(request.case_id, "NOT_APPLICABLE", [], intent,
                                diagnostics=[f"request does not authorise deletion: {intent.rule} "
                                             f"{list(intent.matched)}"])
        case = self.reconstructor.reconstruct(request)
        decision = CaseDecision(request.case_id, "", [], intent)
        decision.diagnostics += case.diagnostics
        for r in case.resources:
            made, why = self.decide_record(case, r)
            if made is None:
                decision.diagnostics.append(f"{r.resource_id}: no steps ({why})")
            else:
                decision.decisions.append(made)
        if self.options.use_activity_candidates:
            made, why = self._candidate_decision(case)
            if made is not None:
                decision.decisions.append(made)
                decision.diagnostics.append(f"{made.resource_id}: {made.rule}")
            elif why:
                decision.diagnostics.append(why)
        elif case.activity_candidates:
            decision.diagnostics.append(
                "unverified activity candidates (not used): "
                + ", ".join(f"{a.resource_id}@{a.activity_id}" for a in case.activity_candidates))
        self._build_steps(decision)
        return decision

    def _build_steps(self, decision: CaseDecision) -> None:
        violations = [d for d in decision.decisions if d.finding]
        steps = []
        if violations:
            decision.status = "INCOMPLETE"
            for d in violations:
                if not d.finding_evidence:
                    decision.diagnostics.append(
                        f"{d.resource_id}: finding has no citable file captured by audit_at")
                steps.append(_step("FINDING", d, d.finding, d.finding_evidence))
                steps.append(_step("REMEDIATION", d, d.remediation, ""))
            for d in decision.decisions:
                steps.append(_step("VERIFICATION", d, d.verification, ""))
        else:
            decision.status = "COMPLETE"
            for d in decision.decisions:
                if d.future_check_only or not d.verification_evidence:
                    decision.diagnostics.append(
                        f"{d.resource_id}: {d.verification} left out of a COMPLETE case: no citable "
                        "file captured by audit_at proves it")
                    continue
                steps.append(_step("VERIFICATION", d, d.verification, d.verification_evidence))
            if not steps:
                decision.status = self.options.empty_case_status
                decision.diagnostics.append("no evidence-backed step could be produced")
        steps.sort(key=step_sort_key)
        for number, step in enumerate(steps, start=1):
            step["step_no"] = number
        decision.steps = steps

    def decide_all(self) -> list[CaseDecision]:
        return [self.decide(r) for r in self.dataset.requests]


def _step(phase: str, d: RecordDecision, operation: str, evidence: str) -> dict:
    return {"step_no": 0, "phase": phase, "system_id": d.system_id, "resource_id": d.resource_id,
            "operation": operation, "state": OPERATION_STATE[operation], "evidence_id": evidence}
