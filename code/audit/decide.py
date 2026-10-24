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
from dataclasses import dataclass, field, replace

from audit.contract import OPERATION_STATE, step_sort_key
from audit.evidence.classify import COLLECTION_CATEGORY, classify_category
from audit.evidence.model import Exposure, TimeBasis
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
        if r.link_strength == "workflow_link" and not self.options.use_activity_candidates:
            return None, "linked only by this case's workflow event (activity candidates disabled)"
        if r.link_strength == "lookup_only" and r.state != ABSENT:
            return None, "lookup-only link may only verify an absent record"
        if (r.link_strength == "lookup_only" and r.state == ABSENT
                and r.system_id == "account_service" and self.options.use_activity_candidates
                and any(a.attributed and a.resource_id != r.resource_id
                        for a in case.activity_candidates)):
            # Gold case_17: a stale 404 on a different account than the one this
            # case's own workflow deleted is not part of this deletion.
            return None, ("lookup of a different account than this case's own workflow "
                          "deletion; not part of this case")
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

    def _upgrade_attributed_account(self, case: CaseReconstruction, decision: "CaseDecision") -> None:
        """P1: upgrade the attributed account's future absence check to a cited one.

        The official samples endorse the workflow link (no activity details carry a
        subject identifier, yet gold answers verify these accounts): the deletion
        event forced onto this case by Reconstructor._attribute_events is its own
        workflow's action, so the account is a workflow_link resource. Evidence
        decides its state as usual. When the state stays UNKNOWN, the record keeps
        a future VERIFY_RECORD_ABSENT (gold case_07/22, emitted only for an
        INCOMPLETE case) — unless a citable SQL snapshot of the primary
        USER_PROFILE copy taken after the event proves the absence, which gives the
        verification its citation (gold case_01/02 cite exactly one such snapshot).
        """
        attributed = next((a for a in case.activity_candidates if a.attributed), None)
        if attributed is None:
            if case.activity_candidates:
                decision.diagnostics.append(
                    "activity candidates not used: no deletion event in the window can be "
                    "attributed to this case's workflow (ambiguous or status-mismatched)")
            return
        state = case.resource(attributed.resource_id)
        if state is None or state.state != UNKNOWN:
            return  # evidence decided the account's state; the normal path handled it
        made = next((d for d in decision.decisions
                     if d.resource_id == attributed.resource_id and d.future_check_only), None)
        if made is None:
            return
        # The event is a claim (AUDIT-001/002): absence must be shown by a citable
        # snapshot scoped to the account service's USER_PROFILE collection — a SQL
        # capture or an assurance bundle's inner snapshot (gold case_31 cites one) —
        # taken after the event, and no later qualifying copy snapshot may still
        # hold the account.
        after_event = [c for c in self.reconstructor.result.coverages
                       if c.system_id == "account_service"
                       and COLLECTION_CATEGORY.get(c.collection) == "USER_PROFILE"
                       and c.time_basis in (TimeBasis.SQL_CAPTURE, TimeBasis.INNER_SNAPSHOT)
                       and attributed.event_time <= c.observed_at <= case.audit_at
                       and c.observed_at <= c.file_captured_at
                       and c.file_captured_at <= case.audit_at
                       and self.dataset.evidence_files.get(c.evidence_id) is not None
                       and self.dataset.evidence_files[c.evidence_id].captured_at <= case.audit_at]
        by_copy = {}
        for coverage in after_event:
            copy = coverage.replica or "primary"
            current = by_copy.get(copy)
            if current is None or (coverage.observed_at, coverage.evidence_id) > (
                    current.observed_at, current.evidence_id):
                by_copy[copy] = coverage
        holding = sorted(c.evidence_id for c in by_copy.values()
                         if attributed.resource_id in c.resource_ids)
        if holding:
            decision.diagnostics.append(
                f"{attributed.resource_id}: absence not verified; latest qualifying copy "
                f"snapshots {holding} still contain it")
            return
        if "primary" not in by_copy:
            decision.diagnostics.append(
                f"{attributed.resource_id}: future check only; no citable SQL USER_PROFILE "
                "snapshot of the primary copy taken after the attributed deletion "
                f"{attributed.activity_id}")
            return
        proof = by_copy["primary"]
        decision.decisions[decision.decisions.index(made)] = replace(
            made, verification_evidence=proof.evidence_id, future_check_only=False)
        decision.diagnostics.append(
            f"{attributed.resource_id}: absent from SQL snapshot {proof.evidence_id} "
            f"({proof.locator}, {format_timestamp(proof.observed_at)}) taken after the "
            f"attributed deletion {attributed.activity_id}")

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
            self._upgrade_attributed_account(case, decision)
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
