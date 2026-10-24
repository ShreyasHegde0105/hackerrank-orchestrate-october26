"""Evidence extraction over the real 94 evidence files."""
from collections import Counter
from datetime import datetime, timezone

import pytest

from audit.evidence.extract import extract_evidence
from audit.evidence.model import Condition, Exposure, FileStatus, TimeBasis
from audit.identity import IdentityIndex
from audit.loader import load_dataset


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def extracted():
    dataset = load_dataset(requests_file="sample_deletion_requests.csv")
    return dataset, extract_evidence(dataset), IdentityIndex.from_dataset(dataset)


def find(result, resource_id, evidence_id=None):
    return [o for o in result.observations if o.resource_id == resource_id
            and (evidence_id is None or o.evidence_id == evidence_id)]


def test_every_file_is_processed_and_only_screenshots_wait_for_ai(extracted):
    dataset, result, _ = extracted
    assert set(result.files) == set(dataset.evidence_files)
    by_status = Counter((r.kind, r.status) for r in result.files.values())
    assert by_status[("png", FileStatus.PENDING_AI)] == 11
    assert all(r.status in (FileStatus.PARSED, FileStatus.PENDING_AI)
               for r in result.files.values())
    assert sum(r.observations for r in result.files.values()) == len(result.observations)


def test_all_six_log_layouts_are_seen_and_no_line_is_unparsed(extracted):
    _, result, _ = extracted
    layouts = {l for r in result.files.values() if r.kind == "log" for l in r.details["layouts"]}
    assert layouts == {"key_value", "nested_message", "pipe", "bracket", "json", "syslog"}
    assert not [i for i in result.issues if "unparsed log line" in i.message]
    assert not [i for i in result.issues if "trace mixes" in i.message]


def test_every_observation_is_traceable(extracted):
    dataset, result, _ = extracted
    for obs in result.observations:
        assert obs.evidence_id in dataset.evidence_files and obs.locator
        assert obs.file_captured_at == dataset.evidence_files[obs.evidence_id].captured_at
        assert obs.system_id or obs.resource_id is None
    late = [o for o in result.observations if o.observed_at > o.file_captured_at]
    assert {(o.evidence_id, o.resource_id) for o in late} == {
        ("evidence_13", "account_1417"), ("evidence_36", "account_5043")}  # flagged anomalies
    assert sum("after the file's capture" in i.message for i in result.issues) == 2


def test_inner_bundle_time_is_preserved(extracted):
    _, result, _ = extracted
    (obs,) = find(result, "audit_record_3954")
    assert obs.evidence_id == "evidence_02" and obs.time_basis is TimeBasis.INNER_SNAPSHOT
    assert obs.observed_at == utc(2026, 9, 15, 12) < obs.file_captured_at == utc(2026, 9, 17, 12)


def test_replica_drift_case_05(extracted):
    _, result, _ = extracted
    (obs,) = find(result, "account_005", "evidence_54")
    assert (obs.replica, obs.condition, obs.exposure) == (
        "failover_replica", Condition.PRESENT, Exposure.IDENTIFIABLE)
    primary = next(c for c in result.coverages
                   if c.evidence_id == "evidence_54" and c.replica == "primary")
    assert "account_005" not in primary.resource_ids
    assert "account_001" not in primary.resource_ids  # case_01's account is absent here


def test_trace_links_support_002_to_old_email_case_10(extracted):
    _, result, index = extracted
    observations = find(result, "support_002", "evidence_61")
    assert {o.attributes["event"] for o in observations} == {"contact_query_completed",
                                                             "deletion_marker_check"}
    for obs in observations:
        (ref,) = obs.identity_refs
        assert ref.basis == "trace:tr-rp-002" and ref.raw == "nadia.ionescu@yahoo.com"
        assert {c.user_id for c in index.claims(ref.kind, ref.value)} == {"user_10"}
    decoy = find(result, "support_5914")[0]
    assert decoy.identity_refs[0].raw == "nadia.popescu@yahoo.com"
    assert not index.claims(decoy.identity_refs[0].kind, decoy.identity_refs[0].value)


def test_states_used_by_sample_answers(extracted):
    _, result, _ = extracted
    assert find(result, "backup_4978", "evidence_93")[0].condition is Condition.RESTORE_ALLOWED
    assert find(result, "data_flow_3827", "evidence_92")[0].condition is Condition.ACTIVE
    assert find(result, "tombstone_9178", "evidence_51")[0].exposure is Exposure.IDENTIFIABLE
    assert find(result, "tombstone_440", "evidence_51")[0].exposure is Exposure.REFERENCE_ONLY


def test_category_comes_from_content(extracted):
    _, result, _ = extracted
    (marketing,) = find(result, "analytics_9914")
    assert marketing.data_category == "MARKETING_PROFILE"
    assert "email_opt_in" in marketing.category_basis and marketing.system_id == "analytics_platform"
    assert find(result, "support_001", "evidence_58")[0].data_category == "SUPPORT_ATTACHMENT"
    assert find(result, "audit_record_003", "evidence_55")[0].attributes["retain_until"] \
        == "2027-08-25T04:03:00Z"


def test_legal_register_rows_stay_separate(extracted):
    _, result, _ = extracted
    rows = [o for o in result.observations if o.evidence_id == "evidence_27"
            and o.extractor == "pdf/table_row"]
    assert len(rows) == 6
    assert all(len(o.identity_refs) == 1 and o.resource_id is None for o in rows)


def test_no_observation_mixes_two_known_people(extracted):
    _, result, index = extracted
    for obs in result.observations:
        owners = {c.user_id for r in obs.identity_refs for c in index.claims(r.kind, r.value)}
        assert len(owners) <= 1, obs.obs_id


def test_sample_answer_records_linked_to_subject(extracted):
    import evidence_report
    dataset, result, index = extracted
    rows = evidence_report.sample_linkage(dataset, result, index)
    assert len(rows) == 111
    assert sum(1 for r in rows if r[3]) >= 84  # Phase 3 baseline
