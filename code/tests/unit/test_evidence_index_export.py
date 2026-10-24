"""Duplicate accounting, the lookup index and the JSONL export, on hand-built evidence."""
import dataclasses
import json

import pytest

from audit.evidence.duplicates import analyse_duplicates
from audit.evidence.export import observation_from_dict, read_jsonl, write_jsonl
from audit.evidence.extract import extract_evidence
from audit.evidence.index import EvidenceIndex
from audit.evidence.model import (
    Condition,
    Exposure,
    ExtractionResult,
    IdentityRef,
    Observation,
    TimeBasis,
)
from audit.evidence.sql_snapshots import extract_sql
from audit.evidence_store import open_sql_snapshot
from audit.identity import IdentityIndex, email_hash
from audit.models import Activity, IdentifierKind, UserRecord
from evidence_helpers import evidence, ts


def ref(value, kind=IdentifierKind.EMAIL, basis="same_record"):
    return IdentityRef(kind, value, value, "email", basis)


def obs(evidence_id="evidence_1", locator="line 1", resource="account_1", refs=(), day=10,
        condition=Condition.PRESENT, **attributes):
    return Observation(evidence_id, "log", locator, ts(day), TimeBasis.LOG_LINE, ts(20),
                       "account_service", resource, "USER_PROFILE", "test", condition,
                       Exposure.UNKNOWN, tuple(refs), attributes=dict(attributes))


# --- duplicates ------------------------------------------------------------------------------

def test_identical_rows_in_one_file_are_flagged_and_kept():
    script = ("CREATE TABLE customer_profiles (resource_id TEXT, customer_reference TEXT, "
              "full_name TEXT, email TEXT, phone TEXT, email_hash TEXT, account_status TEXT, "
              "updated_at TEXT);"
              "INSERT INTO customer_profiles VALUES('account_1','user_1',NULL,'a@x.com',NULL,NULL,'ACTIVE','t');"
              "INSERT INTO customer_profiles VALUES('account_1','user_1',NULL,'a@x.com',NULL,NULL,'ACTIVE','t');")
    connection = open_sql_snapshot(script)
    tables = {"customer_profiles": [dict(r) for r in connection.execute("SELECT * FROM customer_profiles")]}
    observations, _, _, _ = extract_sql(evidence("sql"), script, tables)
    assert len(observations) == 2  # both kept
    report = analyse_duplicates(observations)
    assert report.within_file == [["evidence_99#customer_profiles[0]", "evidence_99#customer_profiles[1]"]]
    assert report.summary()["within_file_extra_copies"] == 1


def test_repeated_lookups_in_different_traces_are_not_duplicates():
    first = obs(locator="line 1", trace_id="t1", event="profile_lookup_completed")
    second = obs(locator="line 9", trace_id="t2", event="profile_lookup_completed")
    assert analyse_duplicates([first, second]).within_file == []


def test_same_fact_from_two_files_is_counted_not_merged():
    a = obs("evidence_1", "line 1", day=10)
    b = obs("evidence_2", "records[0]", day=10)
    c = obs("evidence_3", "records[4]", day=12)               # same state, later
    d = obs("evidence_4", "line 2", day=12, condition=Condition.ABSENT)  # different state
    report = analyse_duplicates([a, b, c, d])
    assert report.duplicate_facts == [["evidence_1#line 1", "evidence_2#records[0]"]]
    assert report.repeated_states == [["evidence_1#line 1", "evidence_2#records[0]",
                                       "evidence_3#records[4]"]]
    assert report.within_file == []


def test_within_file_duplicates_become_warnings_in_extraction(tmp_path):
    from test_dataset_loader import base_rows, write_dataset
    from audit.loader import load_dataset
    root = write_dataset(tmp_path, base_rows())
    (root / "evidence" / "data.json").write_text(json.dumps({"records": [
        {"recorded_at": "2026-06-02T00:00:00Z", "system": "account_service",
         "resource_id": "account_1", "record_type": "USER_PROFILE", "result": "FOUND"}] * 2}),
        encoding="utf-8")
    result = extract_evidence(load_dataset(root))
    assert sum(o.evidence_id == "evidence_2" for o in result.observations) == 2
    assert any("2 identical observations in one file (kept)" in i.message for i in result.issues)


# --- lookup index ------------------------------------------------------------------------------

@pytest.fixture
def people():
    users = [UserRecord("user_1", "Ann", "ann@x.com", "+1 202 555 0101", email_hash("ann@x.com"), ts(1)),
             UserRecord("user_2", "Bob", "bob@x.com", "+1 202 555 0102", email_hash("bob@x.com"), ts(1))]
    link = Activity("activity_1", ts(5), "LINK_RESOURCE_IDENTITY", "data_catalog", "account_service",
                    "account_9", "SUCCESS", "Data catalog linked account_9 (account_9) to ann@x.com and user_1.")
    return IdentityIndex.build(users, [], [link])


def build_index(people, observations):
    return EvidenceIndex(ExtractionResult(observations=list(observations)), people)


def test_person_lookup_never_leaks_between_people(people):
    ann = obs("evidence_1", resource="account_1", refs=[ref("ann@x.com")])
    bob = obs("evidence_2", resource="account_2", refs=[ref("bob@x.com")])
    both = obs("evidence_3", resource="support_1", refs=[ref("ann@x.com"), ref("bob@x.com")])
    index = build_index(people, [ann, bob, both])
    assert index.for_person("user_1").observations == (ann,)
    assert index.for_person("user_2").observations == (bob,)
    assert index.for_person("user_1").conflicted == (both,) == index.for_person("user_2").conflicted
    assert index.for_person("user_3").observations == ()


def test_unresolved_and_identifier_less_evidence_is_kept_separately(people):
    stranger = obs("evidence_1", resource="search_1", refs=[ref("nobody@x.com")])
    anonymous = obs("evidence_2", resource="analytics_1")
    index = build_index(people, [stranger, anonymous])
    assert index.unattributed() == (stranger,) and index.no_identity() == (anonymous,)
    assert index.counts()["attributed_observations"] == 0


def test_lookups_normalise_identifiers_and_keep_provenance(people):
    record = obs("evidence_7", "line 42", resource="account_1",
                 refs=[ref("ann@x.com"), ref("12025550101", IdentifierKind.PHONE)])
    index = build_index(people, [record])
    assert index.by_identifier("  ANN@X.com ") == (record,)
    assert index.by_identifier("+1 (202) 555-0101") == (record,)
    assert index.by_identifier("Ann") == ()  # names are never identifiers
    found = index.by_resource("account_1")[0]
    assert found is record and (found.evidence_id, found.locator) == ("evidence_7", "line 42")


def test_lookup_order_is_deterministic(people):
    later = obs("evidence_10", "line 1", refs=[ref("ann@x.com")], day=12)
    earlier = obs("evidence_2", "line 5", refs=[ref("ann@x.com")], day=10)
    same_time = obs("evidence_9", "line 3", refs=[ref("ann@x.com")], day=10)
    for order in ([later, earlier, same_time], [same_time, later, earlier]):
        assert build_index(people, order).for_person("user_1").observations == (
            earlier, same_time, later)


def test_catalog_link_conflict_is_reported(people):
    consistent = obs("evidence_1", resource="account_9", refs=[ref("ann@x.com")])
    stranger = obs("evidence_2", resource="account_9", refs=[ref("sj.park@naver.com")])
    no_refs = obs("evidence_3", resource="account_9")
    conflicts = build_index(people, [consistent, stranger, no_refs]).link_conflicts()
    assert [(l.resource_id, o.evidence_id, owners) for l, o, owners in conflicts] == [
        ("account_9", "evidence_2", ())]


# --- export --------------------------------------------------------------------------------------

def test_jsonl_round_trip_and_canonical_order(tmp_path):
    observations = [
        obs("evidence_10", "line 2", refs=[ref("josé@x.com")], note="naïve café ✓", rows="1"),
        obs("evidence_2", "records[0]", resource=None, refs=[], flag=True, size=1.5, empty=None),
    ]
    path = tmp_path / "out.jsonl"
    assert write_jsonl(observations, path) == 2
    back = read_jsonl(path)
    assert back == [observations[1], observations[0]]  # evidence_2 sorts before evidence_10
    text = path.read_text(encoding="utf-8")
    assert "naïve café ✓" in text and "josé@x.com" in text  # UTF-8, not \u escapes
    write_jsonl(list(reversed(observations)), tmp_path / "again.jsonl")
    assert (tmp_path / "again.jsonl").read_bytes() == path.read_bytes()


def test_export_rejects_bad_input(tmp_path):
    with pytest.raises(TypeError, match="not JSON-serialisable"):
        write_jsonl([obs(weird=object())], tmp_path / "x.jsonl")
    assert not (tmp_path / "x.jsonl").exists()  # no partial file left behind
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"schema_version": 99}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="bad.jsonl:1: invalid observation"):
        read_jsonl(bad)
    with pytest.raises(ValueError, match="schema_version"):
        observation_from_dict({"schema_version": 2})


def test_round_trip_preserves_every_field():
    original = dataclasses.replace(obs(refs=[ref("user_1", IdentifierKind.USER_ID, "trace:t9")]),
                                   related_resources=("account_6",), labels=("CS-1",),
                                   replica="failover_replica", collection="customer_profiles")
    from audit.evidence.export import dumps
    assert observation_from_dict(json.loads(dumps(original))) == original
