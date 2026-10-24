"""Phase 4 rules on small hand-built evidence (see the rule list in audit/reconstruct.py)."""
import dataclasses
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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
from audit.loader import Dataset
from audit.models import (
    Activity,
    AuditEvent,
    DeletionRequest,
    EvidenceFile,
    IdentifierKind,
    UserRecord,
)
from audit.reconstruct import ABSENT, CONFLICT, CONFLICTED, PRESENT, UNKNOWN, Reconstructor

AUDIT = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


def t(day=15, hour=12, minute=0, second=0):
    return datetime(2026, 9, day, hour, minute, second, tzinfo=timezone.utc)


def ef(file_id, kind="json", captured=None):
    return EvidenceFile(file_id, f"evidence/x/{file_id}.{kind}", "test", captured or t(14))


ANN = IdentityRef(IdentifierKind.EMAIL, "ann@x.com", "ann@x.com", "email", "same_record")
ANN_OLD = IdentityRef(IdentifierKind.EMAIL, "ann.old@x.com", "ann.old@x.com", "email", "same_record")
BOB = IdentityRef(IdentifierKind.EMAIL, "bob@x.com", "bob@x.com", "email", "same_record")
STRANGER = IdentityRef(IdentifierKind.EMAIL, "who@x.com", "who@x.com", "email", "same_record")


def obs(evidence_id, resource, observed, condition=Condition.PRESENT, refs=(ANN,), *,
        captured=None, replica=None, exposure=Exposure.IDENTIFIABLE, locator="records[0]",
        kind="json", category=None, related=(), labels=(), source="json"):
    return Observation(evidence_id, source, locator, observed, TimeBasis.ITEM,
                       captured or observed, system_from(resource), resource,
                       category or category_from(resource), "test", condition, exposure,
                       tuple(refs), tuple(related), tuple(labels), replica=replica)


def system_from(resource):
    from audit.evidence.classify import system_for_resource
    return system_for_resource(resource)


def category_from(resource):
    from audit.evidence.classify import CATEGORY_BY_PREFIX
    return CATEGORY_BY_PREFIX.get(resource.split("_")[0]) if resource else None


def build(observations, coverages=(), files=None, activities=(), requests=None, audit=AUDIT):
    users = {u.user_id: u for u in (
        UserRecord("user_1", "Ann", "ann@x.com", "+1 202 555 0101", email_hash("ann@x.com"), t(1)),
        UserRecord("user_2", "Bob", "bob@x.com", "+1 202 555 0102", email_hash("bob@x.com"), t(1)))}
    audit_events = [
        AuditEvent("audit_1", "user_1", t(1), "email", "ann.old@x.com", "ann@x.com"),
        AuditEvent("audit_2", "user_1", t(1), "email_hash", email_hash("ann.old@x.com"),
                   email_hash("ann@x.com"))]
    requests = requests or [DeletionRequest("case_1", "ann@x.com", t(10), "COMPLETE", t(11),
                                            audit, "delete my account")]
    known = {o.evidence_id for o in observations} | {c.evidence_id for c in coverages}
    files = dict(files or {})
    for file_id in known:
        files.setdefault(file_id, ef(file_id, "json", min(
            [o.file_captured_at for o in observations if o.evidence_id == file_id]
            + [c.file_captured_at for c in coverages if c.evidence_id == file_id])))
    dataset = Dataset(Path("."), requests, users, audit_events, sorted(activities, key=lambda a: a.event_time),
                      {}, files)
    identities = IdentityIndex.from_dataset(dataset)
    result = ExtractionResult(observations=list(observations), coverages=list(coverages))
    return Reconstructor(dataset, identities, result)


def case(rc, case_id="case_1"):
    return rc.reconstruct(next(r for r in rc.dataset.requests if r.case_id == case_id))


def sql_cov(evidence_id, system, collection, observed, resources=(), replica="primary", captured=None):
    return Coverage(evidence_id, f"table {collection}", observed, TimeBasis.SQL_CAPTURE,
                    captured or observed, system, collection, replica, frozenset(resources))


# --- time ------------------------------------------------------------------------------------

@pytest.mark.parametrize("observed,expected", [
    (AUDIT - timedelta(seconds=1), PRESENT),   # before
    (AUDIT, PRESENT),                          # exactly at audit_at: eligible
])
def test_sightings_at_or_before_audit_are_used(observed, expected):
    rc = build([obs("evidence_1", "search_1", observed)])
    assert case(rc).resource("search_1").state == expected


def test_sightings_after_audit_never_count():
    rc = build([obs("evidence_1", "search_1", AUDIT + timedelta(seconds=1))])
    result = case(rc)
    assert result.resources == () and result.excluded_future == ("evidence_1#records[0]",)


def test_a_deletion_after_audit_does_not_make_the_record_absent():
    rc = build([obs("evidence_1", "search_1", t(14)),
                obs("evidence_2", "search_1", t(16), Condition.ABSENT, refs=())])
    assert case(rc).resource("search_1").state == PRESENT


def test_equivalent_instants_in_other_offsets_compare_by_instant():
    plus_two = timezone(timedelta(hours=2))
    at_audit = datetime(2026, 9, 15, 14, 0, tzinfo=plus_two)       # == 12:00 UTC
    after = datetime(2026, 9, 15, 14, 0, 1, tzinfo=plus_two)       # 12:00:01 UTC
    assert case(build([obs("evidence_1", "search_1", at_audit)])).resource("search_1").state == PRESENT
    assert case(build([obs("evidence_1", "search_1", after)])).resources == ()


def test_fact_inside_a_later_file_decides_state_but_is_not_cited():
    later_file = obs("evidence_9", "audit_record_1", t(15, 12), refs=(
        IdentityRef(IdentifierKind.USER_ID, "user_1", "user_1", "subject_reference", "same_record"),),
        captured=t(17), exposure=Exposure.REFERENCE_ONLY)
    state = case(build([later_file])).resource("audit_record_1")
    assert state.state == PRESENT and state.citation is None
    assert "no citable file captured by audit_at" in state.reason


def test_citation_never_comes_from_a_file_captured_after_audit():
    early = obs("evidence_1", "search_1", t(14), captured=t(14))
    late_file = obs("evidence_2", "search_1", t(15, 11), captured=t(16))
    state = case(build([early, late_file])).resource("search_1")
    assert state.state == PRESENT and state.citation == "evidence_1"
    assert "evidence_2#records[0]" in state.supporting  # decided by the newer fact


def test_items_dated_after_their_own_file_are_ignored():
    impossible = obs("evidence_1", "search_1", t(14, 10), captured=t(14, 9))
    result = case(build([impossible]))
    assert result.resources == () and result.excluded_anomalous == ("evidence_1#records[0]",)


def test_screenshots_showing_later_events_are_ignored():
    screen = dataclasses.replace(
        obs("evidence_5", "account_1", t(14, 10), captured=t(14, 10), source="png"),
        attributes={"last_activity": "14 Sep 2026 14:22 UTC"})
    rc = build([screen], files={"evidence_5": ef("evidence_5", "png", t(14, 10))})
    assert rc.anomalous_files == {"evidence_5"}
    assert case(rc).resources == ()


# --- identity and relationships ----------------------------------------------------------------

def test_people_are_kept_apart():
    rc = build([obs("evidence_1", "search_1", t(14), refs=(ANN,)),
                obs("evidence_2", "search_2", t(14), refs=(BOB,))],
               requests=[DeletionRequest("case_1", "ann@x.com", t(10), "COMPLETE", t(11), AUDIT, "x"),
                         DeletionRequest("case_2", "bob@x.com", t(10), "COMPLETE", t(11), AUDIT, "x")])
    assert [r.resource_id for r in case(rc, "case_1").resources] == ["search_1"]
    assert [r.resource_id for r in case(rc, "case_2").resources] == ["search_2"]


def test_historical_identifiers_attribute_records():
    rc = build([obs("evidence_1", "search_1", t(14), refs=(ANN_OLD,))])
    assert case(rc).resource("search_1").state == PRESENT


def test_a_record_also_showing_an_unknown_person_is_conflicted():
    rc = build([obs("evidence_1", "marketing_1", t(14), refs=(ANN,)),
                obs("evidence_2", "marketing_1", t(14, 13), refs=(STRANGER,))])
    state = case(rc).resource("marketing_1")
    assert (state.state, state.existence, state.relationship) == (CONFLICT, PRESENT, CONFLICTED)
    assert "evidence_2#records[0]" in state.competing and state.citation is None


def test_observation_naming_two_people_is_not_attributed():
    both = obs("evidence_1", "support_1", t(14), refs=(ANN, BOB))
    result = case(build([both]))
    assert result.resources == ()
    assert any("several people" in d for d in result.diagnostics)


def test_unresolvable_request_reconstructs_nothing():
    rc = build([obs("evidence_1", "search_1", t(14))],
               requests=[DeletionRequest("case_1", "nobody@x.com", t(10), "COMPLETE", t(11), AUDIT, "x")])
    result = case(rc)
    assert result.resolution == "UNRESOLVED" and result.resources == ()


def test_catalog_links_count_only_when_known_by_audit():
    def link(when):
        return Activity("activity_1", when, "LINK_RESOURCE_IDENTITY", "data_catalog", "object_storage",
                        "object_1", "SUCCESS",
                        "Data catalog linked object_1 (file_1.pdf) to ann@x.com and user_1.")
    labelled = obs("evidence_1", None, t(14), refs=(), labels=("file_1.pdf",), exposure=Exposure.UNKNOWN)
    before = case(build([labelled], activities=[link(t(14))])).resource("object_1")
    assert before.state == PRESENT and before.link_strength == "catalog_link"
    assert before.citation == "evidence_1"  # the label-only observation joined via the link
    assert case(build([labelled], activities=[link(t(16))])).resources == ()


def test_link_strengths():
    rc = build([obs("evidence_1", "account_1", t(14), Condition.ABSENT),
                obs("evidence_2", "search_1", t(14), related=("account_2",))])
    result = case(rc)
    strengths = {r.resource_id: r.link_strength for r in result.resources}
    assert strengths == {"account_1": "lookup_only", "search_1": "record_content",
                         "account_2": "related"}


def test_activity_deletions_are_unverified_candidates_only():
    deletion = Activity("activity_7", t(11) - timedelta(minutes=30), "DELETE_RECORD",
                        "deletion_worker", "account_service", "account_9", "SUCCESS", "done")
    result = case(build([obs("evidence_1", "search_1", t(14))], activities=[deletion]))
    assert [(a.resource_id, a.unique_in_window) for a in result.activity_candidates] == [("account_9", True)]
    assert result.resource("account_9") is None  # no state, no citation


# --- state and absence ----------------------------------------------------------------------------

def test_no_sighting_is_unknown_not_absent():
    rc = build([obs("evidence_1", "search_1", t(14), related=("account_1",))])
    state = case(rc).resource("account_1")
    assert state.state == UNKNOWN and state.citation is None
    assert "absence is not assumed" in state.reason


def test_qualifying_sql_snapshot_proves_absence_and_is_cited():
    seen = obs("evidence_1", "account_1", t(10), source="sql", replica="primary")
    cover = sql_cov("evidence_2", "account_service", "customer_profiles", t(12), ["account_9"])
    rc = build([seen], [cover], files={"evidence_2": ef("evidence_2", "sql", t(12))})
    state = case(rc).resource("account_1")
    assert state.state == ABSENT and state.citation == "evidence_2"
    assert state.citation_obs == "coverage:evidence_2#table customer_profiles"


@pytest.mark.parametrize("variation", ["json_file", "other_copy", "other_category", "too_early",
                                       "after_audit", "file_captured_after_audit", "contains_it"])
def test_snapshots_that_do_not_qualify_never_prove_absence(variation):
    seen = obs("evidence_1", "account_1", t(10), replica="primary")
    kw = dict(evidence_id="evidence_2", system="account_service", collection="customer_profiles",
              observed=t(12), resources=["account_9"])
    kind = "sql"
    if variation == "json_file":
        kind = "json"
    elif variation == "other_copy":
        kw["replica"] = "read_replica"
    elif variation == "other_category":
        kw["collection"] = "customer_search_documents"
    elif variation == "too_early":
        kw["observed"] = t(9)
    elif variation == "after_audit":
        kw["observed"] = t(16)
    elif variation == "file_captured_after_audit":
        kw["captured"] = t(16)
    elif variation == "contains_it":
        kw["resources"] = ["account_1"]
    cover = sql_cov(**kw)
    rc = build([seen], [cover], files={"evidence_2": ef("evidence_2", kind, cover.file_captured_at)})
    assert case(rc).resource("account_1").state == PRESENT


def test_state_is_tracked_per_copy():
    replica = obs("evidence_1", "account_1", t(10), replica="failover_replica", source="sql")
    primary = obs("evidence_2", "account_1", t(11), Condition.ABSENT, refs=(ANN,), replica="primary")
    state = case(build([replica, primary])).resource("account_1")
    assert state.state == PRESENT
    assert {(c.copy, c.state) for c in state.copies} == {("failover_replica", PRESENT),
                                                         ("primary", ABSENT)}
    assert state.citation == "evidence_1"


def test_absent_only_when_every_copy_is_gone():
    gone = [obs("evidence_1", "account_1", t(10), Condition.ABSENT, replica="primary"),
            obs("evidence_2", "account_1", t(11), Condition.ABSENT, replica="read_replica")]
    state = case(build(gone)).resource("account_1")
    assert state.state == ABSENT and state.citation == "evidence_2"


def test_recreated_record_is_present_again():
    rc = build([obs("evidence_1", "search_1", t(10), Condition.ABSENT),
                obs("evidence_2", "search_1", t(11))])
    assert case(rc).resource("search_1").state == PRESENT


def test_disagreement_at_the_same_instant_is_a_conflict():
    rc = build([obs("evidence_1", "search_1", t(11)),
                obs("evidence_2", "search_1", t(11), Condition.ABSENT)])
    state = case(rc).resource("search_1")
    assert state.state == CONFLICT and state.citation is None
    assert set(state.competing) == {"evidence_1#records[0]", "evidence_2#records[0]"}


def test_mentions_never_decide_state():
    mention = obs("evidence_1", "support_1", t(14), Condition.MENTIONED)
    assert case(build([mention])).resource("support_1").state == UNKNOWN


def test_exposure_is_the_most_exposed_present_copy_and_process_state_is_kept():
    rc = build([obs("evidence_1", "analytics_1", t(10), replica="primary", exposure=Exposure.UNLINKED,
                    refs=(IdentityRef(IdentifierKind.USER_ID, "user_1", "user_1", "user_id", "same_record"),)),
                obs("evidence_2", "analytics_1", t(10), replica="read_replica"),
                obs("evidence_3", "data_flow_1", t(10), Condition.ACTIVE)])
    result = case(rc)
    assert result.resource("analytics_1").exposure is Exposure.IDENTIFIABLE
    assert result.resource("data_flow_1").process_state == "ACTIVE"


def test_independent_files_stating_the_same_fact_both_support_it():
    rc = build([obs("evidence_1", "search_1", t(11)), obs("evidence_2", "search_1", t(11))])
    state = case(rc).resource("search_1")
    assert state.supporting == ("evidence_1#records[0]", "evidence_2#records[0]")


# --- determinism ----------------------------------------------------------------------------------

def test_input_order_does_not_change_the_result():
    observations = [obs("evidence_1", "search_1", t(10)), obs("evidence_2", "account_1", t(11), Condition.ABSENT),
                    obs("evidence_3", "search_1", t(12)), obs("evidence_4", "support_1", t(9))]
    expected = case(build(observations))
    for seed in range(5):
        shuffled = observations[:]
        random.Random(seed).shuffle(shuffled)
        assert case(build(shuffled)).resources == expected.resources
