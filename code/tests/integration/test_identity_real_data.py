"""Identity resolution on the real participant dataset."""
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

import pytest

from audit.evidence_store import EvidenceStore
from audit.identity import IdentityIndex, ResolutionStatus, email_hash
from audit.loader import DATASET_DIR, load_dataset
from audit.models import IdentifierKind, Provenance


@pytest.fixture(scope="module")
def evaluation():
    dataset = load_dataset()
    return dataset, IdentityIndex.from_dataset(dataset)


@pytest.fixture(scope="module")
def samples():
    dataset = load_dataset(requests_file="sample_deletion_requests.csv")
    return dataset, IdentityIndex.from_dataset(dataset)


def test_real_dataset_loads_cleanly(evaluation):
    dataset, index = evaluation
    assert len(dataset.requests) == 518 and len(dataset.users) == 850
    assert len(dataset.activities) == 4011 and len(dataset.evidence_files) == 94
    assert dataset.warnings == []
    assert index.issues == []  # no shared identifiers, chain breaks or hash mismatches


@pytest.mark.parametrize("fixture,total,current,historical",
                         [("evaluation", 518, 418, 100), ("samples", 32, 29, 3)])
def test_every_request_resolves_to_exactly_one_user(request, fixture, total, current, historical):
    dataset, index = request.getfixturevalue(fixture)
    results = [index.resolve_request(r) for r in dataset.requests]
    assert Counter(r.status for r in results) == {ResolutionStatus.RESOLVED: total}
    assert Counter(r.method for r in results) == {"current_email": current,
                                                  "historical_email": historical}


def test_case_29_resolves_through_its_historical_email(samples):
    dataset, index = samples
    request = next(r for r in dataset.requests if r.case_id == "case_29")
    result = index.resolve_request(request)
    assert (result.status, result.user_id, result.method) == (
        ResolutionStatus.RESOLVED, "user_29", "historical_email")
    subject = result.subject
    changed = datetime(2026, 5, 18, 5, 36, 49, tzinfo=timezone.utc)
    old_email = subject.match("noah.f438085@outlook.com")
    new_email = subject.match("noah.f4380@outlook.com")
    assert (old_email.provenance, old_email.source, old_email.valid_to) == (
        Provenance.HISTORICAL, "audit_284", changed)
    assert (new_email.provenance, new_email.valid_from) == (Provenance.CURRENT, changed)
    assert subject.values(IdentifierKind.EMAIL_HASH) == {
        "f377ccb44ed37d3b3485c1343aab7d223f60ec70c0ca511fe04c7f09dd5ea727",  # audit_285 (old)
        "b1d07882c40af50936b997d454d5c6518ec22754eedbbf86ca9f6a3c30cc4cf0",  # current
    }
    assert subject.values(IdentifierKind.PHONE) == {"13125550129"}


@pytest.mark.parametrize("case_id,user_id", [("case_10", "user_10"), ("case_30", "user_30")])
def test_other_historical_sample_emails(samples, case_id, user_id):
    dataset, index = samples
    request = next(r for r in dataset.requests if r.case_id == case_id)
    assert index.resolve_request(request).user_id == user_id


def test_email_hash_is_sha256_of_every_known_email(evaluation):
    _, index = evaluation
    for subject in index.subjects.values():
        assert {email_hash(e) for e in subject.values(IdentifierKind.EMAIL)} == \
            subject.values(IdentifierKind.EMAIL_HASH), subject.user_id


def test_people_sharing_a_name_stay_separate(evaluation):
    dataset, index = evaluation
    namesakes = defaultdict(list)
    for user in dataset.users.values():
        namesakes[user.name].append(user.user_id)
    shared = {name: ids for name, ids in namesakes.items() if len(ids) > 1}
    assert {"Thandiwe Ndlovu", "Gabriel Costa"} <= set(shared)
    for name, ids in shared.items():
        assert index.owners(name) == set()
        identifier_sets = [index.subjects[u].identifiers() for u in ids]
        assert identifier_sets[0].isdisjoint(identifier_sets[1])
    assert index.resolve_email("thandiwe.n@gmail.com").user_id == "user_04"
    assert index.resolve_email("thandiwe.n5276@outlook.com").user_id == "user_413"


def test_catalog_links_are_parsed_and_trusted(evaluation):
    _, index = evaluation
    assert len(index.links) == 12 and all(l.trusted for l in index.links)
    by_resource = {l.resource_id: l for l in index.links}
    assert (by_resource["backup_9287"].user_id, by_resource["backup_9287"].system_id) == (
        "user_16", "backup_service")
    assert by_resource["object_19"].exclusive
    assert by_resource["export_1842"].label == "NST-2026-0718-1842"
    assert [l.resource_id for l in index.links_for("user_18")] == ["export_1260", "export_1842"]


def _records_by_resource(dataset):
    """resource_id -> [(file_id, values)]: the values that sit in the same SQL row,
    JSON object or log line as the resource_id. A coarse scan for this test only;
    Phase 3 replaces it with real extractors."""
    store = EvidenceStore(dataset)
    records = defaultdict(list)

    def walk(node, file_id):
        if isinstance(node, dict):
            if isinstance(node.get("resource_id"), str):
                records[node["resource_id"]].append((file_id, list(_strings(node))))
            for value in node.values():
                walk(value, file_id)
        elif isinstance(node, list):
            for value in node:
                walk(value, file_id)

    resource = re.compile(r"\b(?:account|search|support|analytics|export|backup|tombstone|"
                          r"audit_record|legal_hold|data_flow|marketing|object)_\d+\b")
    token = re.compile(r"[\w.+\-]+@[\w.\-]+|[0-9a-f]{64}|user_\d+|\+?\d[\d ()\-]{6,}\d")
    for evidence in dataset.evidence_files.values():
        if evidence.kind == "json":
            walk(store.json(evidence.file_id), evidence.file_id)
        elif evidence.kind == "sql":
            for rows in store.sql_tables(evidence.file_id).values():
                for row in rows:
                    if row.get("record_json"):
                        walk(json.loads(row["record_json"]), evidence.file_id)
                    if row.get("resource_id"):
                        records[row["resource_id"]].append(
                            (evidence.file_id, [str(v) for v in row.values() if v is not None]))
        elif evidence.kind in ("log", "txt"):
            for line in store.lines(evidence.file_id):
                values = token.findall(line)
                for resource_id in set(resource.findall(line)):
                    records[resource_id].append((evidence.file_id, values))
    store.close()
    return records


def _strings(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)
    elif isinstance(node, str):
        yield node


def test_resolved_identities_reach_the_sample_answer_records(samples):
    """Gate check: the identifiers of each resolved sample subject must connect
    to the records in the official answers, and never to another user."""
    dataset, index = samples
    records = _records_by_resource(dataset)
    with (DATASET_DIR / "sample_output.csv").open(encoding="utf-8") as handle:
        gold = {r["case_id"]: json.loads(r["steps_json"]) for r in csv.DictReader(handle)}
    outcome = Counter()
    for request in dataset.requests:
        subject = index.resolve_request(request).subject
        for step in gold[request.case_id]:
            owners = {user for _, values in records.get(step["resource_id"], [])
                      for value in values for user in index.owners(value)}
            owners |= {l.user_id for l in index.links if l.resource_id == step["resource_id"]}
            if subject.user_id in owners:
                outcome["subject"] += 1
            elif owners:
                outcome["other user"] += 1
            else:
                outcome["no identifier in record"] += 1
    assert outcome["other user"] == 0
    assert outcome["subject"] >= 147  # Phase 2 baseline; the rest carry no identifier
