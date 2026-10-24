"""Phase 5 decision rules on hand-built Phase 4 results, and the intent rules."""
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from audit.decide import DecisionEngine, PolicyOptions
from audit.evidence.model import Coverage, Exposure, TimeBasis
from audit.intent import classify_request
from audit.loader import Dataset
from audit.models import DeletionRequest, DeletionRule
from audit.reconstruct import ActivityCandidate, CaseReconstruction, ResourceState
from audit.validator import validate_steps

AUDIT = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
RULES = {
    "USER_PROFILE": "DELETE", "SECONDARY_IDENTIFIER": "DELETE", "SEARCH_INDEX_ENTRY": "DELETE",
    "SUPPORT_MESSAGE": "REDACT", "SUPPORT_ATTACHMENT": "DELETE", "ANALYTICS_PROFILE": "ANONYMIZE",
    "USER_EXPORT": "DELETE", "MARKETING_PROFILE": "DELETE", "EMAIL_DELIVERY_LOG": "REDACT",
    "SECURITY_AUDIT_LOG": "RETAIN", "LEGAL_HOLD_RECORD": "RETAIN", "BACKUP_ARCHIVE": "RETAIN",
    "ACCOUNT_TOMBSTONE": "REDACT",
}


def rs(resource_id, category, state="PRESENT", exposure=Exposure.IDENTIFIABLE, process=None,
       link="record_content", citation="evidence_1", system=None, supporting=()):
    from audit.evidence.classify import system_for_resource
    return ResourceState(resource_id, system or system_for_resource(resource_id), category, state,
                         state, exposure, process, "CONFLICTED" if state == "CONFLICT" else "CONFIRMED",
                         ("evidence:x",), link, (), tuple(supporting), citation, None, (), "test")


class StubReconstructor:
    def __init__(self, cases, coverages=(), observations=()):
        self.cases = cases
        self.sql_coverage = {"account_service": list(coverages)}
        self.result = SimpleNamespace(observations=list(observations))

    def reconstruct(self, request):
        return self.cases[request.case_id]


def engine(resources, options=None, candidates=(), coverages=(), text="Please delete my account.",
           observations=()):
    request = DeletionRequest("case_1", "a@x.com", AUDIT, "COMPLETE", AUDIT, AUDIT, text)
    rules = {c: DeletionRule(f"rule_{i:02d}", c, "-", a) for i, (c, a) in enumerate(RULES.items(), 1)}
    dataset = Dataset(Path("."), [request], {}, [], [], rules, {})
    case = CaseReconstruction("case_1", AUDIT, "RESOLVED", "user_1", tuple(resources),
                              tuple(candidates))
    return DecisionEngine(dataset, StubReconstructor({"case_1": case}, coverages, observations),
                          options), request


def decide(resources, **kw):
    eng, request = engine(resources, **kw)
    return eng.decide(request)


def ops(decision):
    return [(s["phase"], s["operation"], s["resource_id"], s["evidence_id"]) for s in decision.steps]


# --- intent ---------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Please permanently delete the account associated with this email address.",
    "I'm deleting my account. You can keep anonymous statistics.",
    "Please check whether the account data have already been removed.",
    "Please confirm that the profile is gone and that remaining records cannot identify me.",
    "Je demande la suppression définitive du compte associé à cette adresse e-mail.",
    "Solicito la eliminación permanente de la cuenta asociada a este correo.",
    "Solicito a exclusão permanente da conta associada a este e-mail.",
    "このメールアドレスに関連するアカウントの完全な削除を依頼します。",
    "이 이메일 주소에 연결된 계정의 영구 삭제를 요청합니다.",
    "Please close my account and process the associated personal-data deletion.",
])
def test_deletion_requests_are_recognised(text):
    assert classify_request(text).authorises_deletion


@pytest.mark.parametrize("text,rule", [
    ("Please stop the promotional emails. I am not asking for deletion.", "explicit negation of deletion"),
    ("I'd like to download my data. Please keep the account as it is.", "explicit negation of deletion"),
    ("I need help resetting the login. Do not close the account.", "explicit negation of deletion"),
    ("Can you correct my surname without removing any of my records?", "explicit negation of deletion"),
    ("Please remove me from marketing lists only.", "marketing opt-out only"),
    ("This is an information request, not a request to erase it.", "explicit negation of deletion"),
    ("Please change my email address. I want to continue using the service.", "explicit negation of deletion"),
    ("Could you send me a copy? Please don’t delete or change anything.", "explicit negation of deletion"),
    ("What personal information do you hold about me?", "no deletion phrase"),
    ("Je ne demande pas la suppression de mon compte.", "explicit negation of deletion"),
    ("No quiero la eliminación de mi cuenta, solo una copia.", "explicit negation of deletion"),
    ("Não solicito a exclusão da conta.", "explicit negation of deletion"),
    ("Bitte keine Löschung, nur eine Kopie.", "explicit negation of deletion"),
    ("アカウントは削除しないでください。", "explicit negation of deletion"),
    ("계정을 삭제하지 마세요.", "explicit negation of deletion"),
    ("", "empty request text"),
])
def test_other_requests_are_not_deletion(text, rule):
    verdict = classify_request(text)
    assert not verdict.authorises_deletion and verdict.rule == rule


def test_not_applicable_case_has_no_steps():
    decision = decide([rs("account_1", "USER_PROFILE")], text="Please keep my account active; "
                      "do not close it.")
    assert (decision.status, decision.steps) == ("NOT_APPLICABLE", [])
    assert decision.row()["steps_json"] == "[]"


# --- rule mapping --------------------------------------------------------------------------------

V, F, R = "VERIFICATION", "FINDING", "REMEDIATION"


@pytest.mark.parametrize("record,expected", [
    (rs("search_1", "SEARCH_INDEX_ENTRY"),
     [(F, "REPORT_REMAINING_RECORD"), (R, "DELETE_RECORD"), (V, "VERIFY_RECORD_ABSENT")]),
    (rs("account_1", "USER_PROFILE", "ABSENT", Exposure.UNKNOWN), [(V, "VERIFY_RECORD_ABSENT")]),
    (rs("support_1", "SUPPORT_MESSAGE"),
     [(F, "REPORT_EXPOSED_PII"), (R, "REDACT_RECORD"), (V, "VERIFY_RECORD_REDACTED")]),
    (rs("support_1", "SUPPORT_MESSAGE", exposure=Exposure.REFERENCE_ONLY), [(V, "VERIFY_RECORD_REDACTED")]),
    (rs("support_1", "SUPPORT_ATTACHMENT"),
     [(F, "REPORT_REMAINING_RECORD"), (R, "DELETE_RECORD"), (V, "VERIFY_RECORD_ABSENT")]),
    (rs("analytics_1", "ANALYTICS_PROFILE", exposure=Exposure.REFERENCE_ONLY),
     [(F, "REPORT_EXPOSED_PII"), (R, "ANONYMIZE_RECORD"), (V, "VERIFY_RECORD_ANONYMIZED")]),
    (rs("analytics_1", "ANALYTICS_PROFILE", exposure=Exposure.UNLINKED), [(V, "VERIFY_RECORD_ANONYMIZED")]),
    (rs("marketing_1", "MARKETING_PROFILE", exposure=Exposure.UNKNOWN),
     [(F, "REPORT_REMAINING_RECORD"), (R, "DELETE_RECORD"), (V, "VERIFY_RECORD_ABSENT")]),
    (rs("export_1", "USER_EXPORT"),
     [(F, "REPORT_REMAINING_RECORD"), (R, "DELETE_RECORD"), (V, "VERIFY_RECORD_ABSENT")]),
    (rs("tombstone_1", "ACCOUNT_TOMBSTONE"),
     [(F, "REPORT_EXPOSED_PII"), (R, "REDACT_RECORD"), (V, "VERIFY_RECORD_REDACTED")]),
    (rs("tombstone_1", "ACCOUNT_TOMBSTONE", exposure=Exposure.REFERENCE_ONLY), [(V, "VERIFY_RECORD_REDACTED")]),
    (rs("audit_record_1", "SECURITY_AUDIT_LOG", exposure=Exposure.REFERENCE_ONLY), [(V, "VERIFY_RECORD_PRESENT")]),
    (rs("legal_hold_1", "LEGAL_HOLD_RECORD", exposure=Exposure.REFERENCE_ONLY), [(V, "VERIFY_RECORD_PRESENT")]),
    (rs("backup_1", "BACKUP_ARCHIVE", process="RESTORE_ALLOWED"),
     [(F, "REPORT_RESTORE_RISK"), (R, "BLOCK_RESTORE"), (V, "VERIFY_RESTORE_BLOCKED")]),
    (rs("backup_1", "BACKUP_ARCHIVE", process="RESTORE_BLOCKED"), [(V, "VERIFY_RESTORE_BLOCKED")]),
    (rs("data_flow_1", "DATA_FLOW", process="ACTIVE"),
     [(F, "REPORT_ACTIVE_DATA_FLOW"), (R, "DISABLE_DATA_FLOW"), (V, "VERIFY_DATA_FLOW_DISABLED")]),
    (rs("data_flow_1", "DATA_FLOW", process="DISABLED"), [(V, "VERIFY_DATA_FLOW_DISABLED")]),
    (rs("support_1", "SUPPORT_MESSAGE", "ABSENT", Exposure.UNKNOWN), [(V, "VERIFY_RECORD_ABSENT")]),
])
def test_each_category_and_state(record, expected):
    decision = decide([record])
    assert [(s["phase"], s["operation"]) for s in decision.steps] == expected
    assert decision.status == ("INCOMPLETE" if expected[0][0] == F else "COMPLETE")
    assert validate_steps("case_1", decision.status, decision.row()["steps_json"]) == []


@pytest.mark.parametrize("record,reason", [
    (rs("object_1", "SUPPORT_ATTACHMENT", link="catalog_link"), "catalog link"),
    (rs("marketing_1", "MARKETING_PROFILE", state="CONFLICT"), "CONFLICT"),
    (rs("account_1", "USER_PROFILE", link="lookup_only"), "lookup-only"),
    (rs("support_1", None), "category undetermined"),
    (rs("support_1", "SUPPORT_MESSAGE", exposure=Exposure.UNKNOWN), "cannot tell if redacted"),
    (rs("analytics_1", "ANALYTICS_PROFILE", exposure=Exposure.UNKNOWN), "cannot tell if anonymized"),
    (rs("backup_1", "BACKUP_ARCHIVE"), "restore state not shown"),
    (rs("audit_record_1", "SECURITY_AUDIT_LOG", "ABSENT"), "must be retained but is ABSENT"),
    (rs("audit_record_1", "SECURITY_AUDIT_LOG", "UNKNOWN"), "UNKNOWN state"),
    (rs("data_flow_1", "DATA_FLOW"), "data flow state not shown"),
])
def test_records_without_a_supported_decision_produce_no_steps(record, reason):
    decision = decide([record])
    assert decision.steps == [] and any(reason in d for d in decision.diagnostics)


def test_lookup_only_absent_record_may_be_verified():
    decision = decide([rs("account_1", "USER_PROFILE", "ABSENT", Exposure.UNKNOWN, link="lookup_only")])
    assert ops(decision) == [(V, "VERIFY_RECORD_ABSENT", "account_1", "evidence_1")]


def test_expired_retention_is_not_verified_as_retained():
    obs = SimpleNamespace(obs_id="o1", attr=lambda name: "2026-01-01T00:00:00Z"
                          if name == "retention_until" else None)
    decision = decide([rs("audit_record_1", "SECURITY_AUDIT_LOG", supporting=("o1",))],
                      observations=[obs])
    assert decision.steps == [] and any("retention or hold ended" in d for d in decision.diagnostics)


# --- statuses, citations, unknowns ---------------------------------------------------------------

def test_incomplete_case_cites_findings_only_and_verifies_everything():
    decision = decide([rs("search_1", "SEARCH_INDEX_ENTRY", citation="evidence_7"),
                       rs("audit_record_1", "SECURITY_AUDIT_LOG", citation="evidence_8"),
                       rs("account_1", "USER_PROFILE", "ABSENT", citation="evidence_9")])
    assert decision.status == "INCOMPLETE"
    assert ops(decision) == [
        (F, "REPORT_REMAINING_RECORD", "search_1", "evidence_7"),
        (R, "DELETE_RECORD", "search_1", ""),
        (V, "VERIFY_RECORD_ABSENT", "account_1", ""),
        (V, "VERIFY_RECORD_ABSENT", "search_1", ""),
        (V, "VERIFY_RECORD_PRESENT", "audit_record_1", ""),
    ]
    assert [s["step_no"] for s in decision.steps] == [1, 2, 3, 4, 5]


def test_complete_case_keeps_only_cited_verifications():
    decision = decide([rs("account_1", "USER_PROFILE", "ABSENT", citation="evidence_9"),
                       rs("audit_record_1", "SECURITY_AUDIT_LOG", citation=None)])
    assert decision.status == "COMPLETE"
    assert ops(decision) == [(V, "VERIFY_RECORD_ABSENT", "account_1", "evidence_9")]
    assert any("audit_record_1" in d and "left out of a COMPLETE case" in d for d in decision.diagnostics)


def test_finding_without_a_citable_file_is_reported():
    decision = decide([rs("search_1", "SEARCH_INDEX_ENTRY", citation=None)])
    assert decision.status == "INCOMPLETE" and decision.steps[0]["evidence_id"] == ""
    assert any("finding has no citable file" in d for d in decision.diagnostics)


def test_unknown_record_is_a_future_check_only_in_incomplete_cases():
    unknown = rs("account_1", "USER_PROFILE", "UNKNOWN", Exposure.UNKNOWN, citation=None)
    incomplete = decide([unknown, rs("search_1", "SEARCH_INDEX_ENTRY")])
    assert (V, "VERIFY_RECORD_ABSENT", "account_1", "") in ops(incomplete)
    complete = decide([unknown])
    assert complete.steps == [] and any("left out of a COMPLETE case" in d for d in complete.diagnostics)


def test_unknown_record_without_category_uses_its_id():
    unknown = rs("account_1", None, "UNKNOWN", Exposure.UNKNOWN, citation=None, link="related")
    decision = decide([unknown, rs("search_1", "SEARCH_INDEX_ENTRY")])
    assert (V, "VERIFY_RECORD_ABSENT", "account_1", "") in ops(decision)


@pytest.mark.parametrize("status", ["COMPLETE", "INCOMPLETE"])
def test_empty_case_status_option(status):
    decision = decide([], options=PolicyOptions(empty_case_status=status))
    assert decision.status == status and decision.steps == []
    assert any("no evidence-backed step" in d for d in decision.diagnostics)


def test_uncategorised_support_category_option():
    record = rs("support_2", None)
    assert decide([record]).steps == []
    decision = decide([record], options=PolicyOptions(uncategorised_support_category="SECONDARY_IDENTIFIER"))
    assert [(s["phase"], s["operation"]) for s in decision.steps] == [
        (F, "REPORT_REMAINING_RECORD"), (R, "DELETE_RECORD"), (V, "VERIFY_RECORD_ABSENT")]


# --- activity candidates (P1) -------------------------------------------------------------------

def candidate(resource="account_9", unique=True, when=datetime(2026, 9, 10, tzinfo=timezone.utc)):
    return ActivityCandidate("activity_5", resource, when, "SUCCESS", unique)


def cov(evidence_id, observed, resources=()):
    return Coverage(evidence_id, "table t", observed, TimeBasis.SQL_CAPTURE, observed,
                    "account_service", "customer_profiles", "primary", frozenset(resources))


def test_candidates_are_diagnostic_unless_enabled():
    decision = decide([], candidates=[candidate()])
    assert decision.steps == [] and any("unverified activity candidates" in d for d in decision.diagnostics)


def test_enabled_candidate_is_verified_with_a_later_sql_snapshot():
    later = cov("evidence_54", datetime(2026, 9, 12, tzinfo=timezone.utc))
    too_early = cov("evidence_40", datetime(2026, 9, 1, tzinfo=timezone.utc), ["account_9"])
    decision = decide([], options=PolicyOptions(use_activity_candidates=True),
                      candidates=[candidate()], coverages=[too_early, later])
    assert ops(decision) == [(V, "VERIFY_RECORD_ABSENT", "account_9", "evidence_54")]
    rationale = next(d for d in decision.diagnostics if "via activity candidate" in d)
    assert "the only deletion_worker account deletion" in rationale and "evidence_54" in rationale


def test_candidate_needs_a_snapshot_proving_absence():
    on = PolicyOptions(use_activity_candidates=True)
    # the activity event alone is never proof: no snapshot -> not used, not even as a future check
    decision = decide([rs("search_1", "SEARCH_INDEX_ENTRY")], options=on, candidates=[candidate()])
    assert "account_9" not in [s["resource_id"] for s in decision.steps]
    assert any("no citable SQL snapshot" in d for d in decision.diagnostics)
    # a later snapshot that still holds the account vetoes it
    holds = cov("evidence_41", datetime(2026, 9, 13, tzinfo=timezone.utc), ["account_9"])
    lacks = cov("evidence_54", datetime(2026, 9, 12, tzinfo=timezone.utc))
    decision = decide([], options=on, candidates=[candidate()], coverages=[lacks, holds])
    assert decision.steps == [] and any("still contain it" in d for d in decision.diagnostics)
    # a snapshot captured after audit_at cannot be cited, so it does not qualify
    late = Coverage("evidence_70", "table t", datetime(2026, 9, 12, tzinfo=timezone.utc),
                    TimeBasis.SQL_CAPTURE, datetime(2026, 9, 16, tzinfo=timezone.utc),
                    "account_service", "customer_profiles", "primary", frozenset())
    assert decide([], options=on, candidates=[candidate()], coverages=[late]).steps == []


def test_candidate_rules_ambiguity_and_existing_accounts():
    on = PolicyOptions(use_activity_candidates=True)
    proof = cov("evidence_54", datetime(2026, 9, 12, tzinfo=timezone.utc))
    assert decide([], options=on, candidates=[candidate(unique=False), candidate("account_8", False)],
                  coverages=[proof]).steps == []
    evidence_account = rs("account_1", "USER_PROFILE", "ABSENT")
    decision = decide([evidence_account], options=on, candidates=[candidate()], coverages=[proof])
    assert [s["resource_id"] for s in decision.steps] == ["account_1"]


# --- determinism ------------------------------------------------------------------------------------

def test_decisions_do_not_depend_on_record_order():
    records = [rs("search_1", "SEARCH_INDEX_ENTRY"), rs("support_1", "SUPPORT_MESSAGE"),
               rs("data_flow_1", "DATA_FLOW", process="ACTIVE"),
               rs("backup_1", "BACKUP_ARCHIVE", process="RESTORE_ALLOWED"),
               rs("audit_record_1", "SECURITY_AUDIT_LOG")]
    expected = decide(records).row()
    for seed in range(5):
        shuffled = records[:]
        random.Random(seed).shuffle(shuffled)
        assert decide(shuffled).row() == expected
    steps = json.loads(expected["steps_json"])
    assert [s["operation"] for s in steps if s["phase"] == R] == [
        "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD"]
