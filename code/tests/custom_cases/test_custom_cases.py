"""Team-created evaluation cases (reported in code/evaluation/evaluation_report.md).

Each case is a small hand-made scenario run end to end through the real
pipeline: IdentityIndex (Phase 2) -> Reconstructor (Phase 4) ->
DecisionEngine with the approved policy (Phase 5) -> the official output
row. Expected rows were written by hand from AGENTS.md section 6 and
deletion_rules.csv, before running the pipeline.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from audit.decide import APPROVED, DecisionEngine
from audit.evidence.model import (
    Condition,
    Coverage,
    Exposure,
    ExtractionResult,
    IdentityRef,
    Observation,
    TimeBasis,
)
from audit.identity import IdentityIndex, email_hash
from audit.loader import DATASET_DIR, Dataset, load_dataset
from audit.models import AuditEvent, DeletionRequest, EvidenceFile, IdentifierKind, UserRecord
from audit.reconstruct import Reconstructor

RULES = load_dataset().rules  # the official deletion_rules.csv


def t(day, hour=12):
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


def ref(value, kind=IdentifierKind.EMAIL):
    return IdentityRef(kind, value, value, kind.value, "same_record")


def obs(evidence_id, resource, system, day, condition=Condition.PRESENT, refs=(), replica=None,
        category=None, exposure=Exposure.IDENTIFIABLE, captured=None, hour=12):
    when = t(day, hour)
    return Observation(evidence_id, "json", "records[0]", when, TimeBasis.ITEM, captured or when,
                       system, resource, category, "custom case", condition, exposure, tuple(refs),
                       replica=replica)


def run_case(text, email, observations, coverages=(), audit_day=15):
    users = {"user_1": UserRecord("user_1", "Ana Lima", "ana.new@x.com", "+55 11 90000 0001",
                                  email_hash("ana.new@x.com"), t(1))}
    history = [AuditEvent("audit_1", "user_1", t(2), "email", "ana.old@x.com", "ana.new@x.com"),
               AuditEvent("audit_2", "user_1", t(2), "email_hash", email_hash("ana.old@x.com"),
                          email_hash("ana.new@x.com"))]
    request = DeletionRequest("case_900", email, t(8), "COMPLETE", t(9), t(audit_day), text)
    files = {}
    for item in list(observations) + list(coverages):
        kind = "sql" if isinstance(item, Coverage) or item.replica else "json"
        files[item.evidence_id] = EvidenceFile(item.evidence_id, f"evidence/{item.evidence_id}.{kind}",
                                               "custom", item.file_captured_at)
    dataset = Dataset(Path(DATASET_DIR), [request], users, history, [], RULES, files)
    result = ExtractionResult(observations=list(observations), coverages=list(coverages))
    reconstructor = Reconstructor(dataset, IdentityIndex.from_dataset(dataset), result)
    decision = DecisionEngine(dataset, reconstructor, APPROVED).decide(request)
    return decision.status, [(s["phase"], s["system_id"], s["resource_id"], s["operation"],
                              s["evidence_id"]) for s in decision.steps]


DELETE_ME = "Please permanently delete my account and personal data."


def test_case_1_non_english_request_that_is_not_a_deletion():
    """French: 'I am not asking for my account to be deleted, only for a copy of my data.'"""
    status, steps = run_case("Je ne demande pas la suppression de mon compte, seulement une copie "
                             "de mes données.", "ana.new@x.com",
                             [obs("evidence_1", "search_1", "search_service", 10, refs=[ref("ana.new@x.com")])])
    assert (status, steps) == ("NOT_APPLICABLE", [])


def test_case_2_request_by_an_old_email_finds_records_under_both():
    """Request uses the email replaced on day 2; a search entry still holds the OLD email,
    and the account lookup by the NEW email returned nothing."""
    status, steps = run_case(DELETE_ME, "ana.old@x.com", [
        obs("evidence_1", "search_1", "search_service", 10, refs=[ref("ana.old@x.com")],
            category="SEARCH_INDEX_ENTRY"),
        obs("evidence_2", "account_1", "account_service", 11, Condition.ABSENT,
            refs=[ref("ana.new@x.com")], category="USER_PROFILE", exposure=Exposure.UNKNOWN),
    ])
    assert (status, steps) == ("INCOMPLETE", [
        ("FINDING", "search_service", "search_1", "REPORT_REMAINING_RECORD", "evidence_1"),
        ("REMEDIATION", "search_service", "search_1", "DELETE_RECORD", ""),
        ("VERIFICATION", "account_service", "account_1", "VERIFY_RECORD_ABSENT", ""),
        ("VERIFICATION", "search_service", "search_1", "VERIFY_RECORD_ABSENT", ""),
    ])


def test_case_3_evidence_after_audit_at_is_ignored():
    """The entry was gone on day 10 (audit day 15). A file captured on day 16 shows it
    again: that later evidence must not change the answer or be cited."""
    status, steps = run_case(DELETE_ME, "ana.new@x.com", [
        obs("evidence_1", "search_1", "search_service", 10, Condition.ABSENT,
            refs=[ref("ana.new@x.com")], category="SEARCH_INDEX_ENTRY", exposure=Exposure.UNKNOWN),
        obs("evidence_2", "search_1", "search_service", 16, refs=[ref("ana.new@x.com")],
            category="SEARCH_INDEX_ENTRY"),
    ])
    assert (status, steps) == ("COMPLETE", [
        ("VERIFICATION", "search_service", "search_1", "VERIFY_RECORD_ABSENT", "evidence_1")])


def test_case_4_active_sync_recreated_a_deleted_record():
    """Deleted on day 10, but an enabled connector re-created it by day 12 (before audit).
    The flow must be disabled before the record is deleted again."""
    status, steps = run_case(DELETE_ME, "ana.new@x.com", [
        obs("evidence_1", "search_1", "search_service", 10, Condition.ABSENT,
            refs=[ref("ana.new@x.com")], category="SEARCH_INDEX_ENTRY", exposure=Exposure.UNKNOWN),
        obs("evidence_2", "search_1", "search_service", 12, refs=[ref("ana.new@x.com")],
            category="SEARCH_INDEX_ENTRY"),
        obs("evidence_3", "data_flow_1", "integration_service", 12, Condition.ACTIVE,
            refs=[ref("ana.new@x.com")], category="DATA_FLOW", exposure=Exposure.UNKNOWN),
    ])
    assert (status, steps) == ("INCOMPLETE", [
        ("FINDING", "search_service", "search_1", "REPORT_REMAINING_RECORD", "evidence_2"),
        ("FINDING", "integration_service", "data_flow_1", "REPORT_ACTIVE_DATA_FLOW", "evidence_3"),
        ("REMEDIATION", "integration_service", "data_flow_1", "DISABLE_DATA_FLOW", ""),
        ("REMEDIATION", "search_service", "search_1", "DELETE_RECORD", ""),
        ("VERIFICATION", "search_service", "search_1", "VERIFY_RECORD_ABSENT", ""),
        ("VERIFICATION", "integration_service", "data_flow_1", "VERIFY_DATA_FLOW_DISABLED", ""),
    ])


def test_case_5_primary_deleted_but_a_replica_still_holds_the_profile():
    """The primary lookup returns 404, but the failover replica snapshot still has the row."""
    status, steps = run_case(DELETE_ME, "ana.new@x.com", [
        obs("evidence_1", "account_1", "account_service", 10, refs=[ref("ana.new@x.com")],
            replica="failover_replica", category="USER_PROFILE"),
        obs("evidence_2", "account_1", "account_service", 11, Condition.ABSENT,
            refs=[ref("ana.new@x.com")], category="USER_PROFILE", exposure=Exposure.UNKNOWN),
    ])
    assert (status, steps) == ("INCOMPLETE", [
        ("FINDING", "account_service", "account_1", "REPORT_REMAINING_RECORD", "evidence_1"),
        ("REMEDIATION", "account_service", "account_1", "DELETE_RECORD", ""),
        ("VERIFICATION", "account_service", "account_1", "VERIFY_RECORD_ABSENT", ""),
    ])


def test_case_6_retained_security_log_and_redacted_tombstone_complete():
    """Account gone, security log retained, tombstone keeps only user_1: COMPLETE."""
    user = ref("user_1", IdentifierKind.USER_ID)
    status, steps = run_case(DELETE_ME, "ana.new@x.com", [
        obs("evidence_1", "account_1", "account_service", 10, Condition.ABSENT,
            refs=[ref("ana.new@x.com")], category="USER_PROFILE", exposure=Exposure.UNKNOWN),
        obs("evidence_2", "audit_record_1", "security_service", 11, refs=[user],
            category="SECURITY_AUDIT_LOG", exposure=Exposure.REFERENCE_ONLY),
        obs("evidence_3", "tombstone_1", "identity_service", 11, refs=[user],
            category="ACCOUNT_TOMBSTONE", exposure=Exposure.REFERENCE_ONLY),
    ])
    assert (status, steps) == ("COMPLETE", [
        ("VERIFICATION", "account_service", "account_1", "VERIFY_RECORD_ABSENT", "evidence_1"),
        ("VERIFICATION", "security_service", "audit_record_1", "VERIFY_RECORD_PRESENT", "evidence_2"),
        ("VERIFICATION", "identity_service", "tombstone_1", "VERIFY_RECORD_REDACTED", "evidence_3"),
    ])
