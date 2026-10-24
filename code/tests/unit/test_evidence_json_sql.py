import pytest

from audit.evidence.json_records import extract_json
from audit.evidence.model import Condition, Exposure, FileStatus, TimeBasis
from audit.evidence.sql_snapshots import extract_sql
from audit.evidence_store import open_sql_snapshot
from evidence_helpers import by_resource, evidence, ts

BUNDLE = {
    "exported_at": "2026-09-17T12:00:00Z",
    "snapshots": [
        {"captured_at": "2026-09-15T12:00:00Z", "data_store": "security_service",
         "collection": "security_audit_events",
         "records": [{"resource_id": "audit_record_1", "subject_reference": "user_29"}]},
        {"captured_at": "2026-09-15T12:00:00Z", "data_store": "account_service",
         "collection": "customer_profiles", "records": []},
        {"captured_at": "2026-09-09T12:00:00Z", "data_store": "search_service",
         "collection": "customer_search_documents",
         "records": [{"resource_id": "search_1", "document": {
             "display_name": "Noah F", "email": "Noah@X.com", "customer_reference": "user_29"}}]},
        {"captured_at": "2026-09-10T00:00:00Z", "data_store": "backup_restore_service",
         "collection": "operational_record_history",
         "records": [{"recorded_at": "2026-09-08T09:00:00Z", "system": "account_service",
                      "resource_id": "account_9", "record_type": "USER_PROFILE",
                      "result": "FOUND", "lookup_value": "n@x.com"}]},
    ],
}


def run_json(data, captured=None):
    return extract_json(evidence("json", captured or ts(17, 12)), data)


def test_bundle_keeps_each_inner_snapshot_time():
    observations, coverages, issues, report = run_json(BUNDLE)
    assert issues == [] and report.parser == "json/bundle" and report.status is FileStatus.PARSED
    audit = by_resource(observations, "audit_record_1")[0]
    assert audit.observed_at == ts(15, 12) and audit.time_basis is TimeBasis.INNER_SNAPSHOT
    assert audit.file_captured_at == ts(17, 12)  # the citation date stays the manifest's
    search = by_resource(observations, "search_1")[0]
    assert search.observed_at == ts(9, 12)
    assert search.exposure is Exposure.IDENTIFIABLE
    assert {r.value for r in search.identity_refs} == {"noah@x.com", "user_29"}


def test_empty_snapshot_is_coverage_with_no_resources():
    _, coverages, _, _ = run_json(BUNDLE)
    accounts = next(c for c in coverages if c.collection == "customer_profiles")
    assert accounts.resource_ids == frozenset() and accounts.observed_at == ts(15, 12)
    # lookup history is not a snapshot of a system, so it gives no coverage
    assert all(c.collection != "operational_record_history" for c in coverages)


def test_lookup_history_uses_each_item_time():
    observations, _, _, _ = run_json(BUNDLE)
    lookup = by_resource(observations, "account_9")[0]
    assert lookup.observed_at == ts(8, 9) and lookup.time_basis is TimeBasis.ITEM
    assert lookup.attributes["reported_by"] == "backup_restore_service"


def test_inner_snapshot_after_export_is_flagged():
    data = {"exported_at": "2026-09-01T00:00:00Z", "snapshots": [
        {"captured_at": "2026-09-02T00:00:00Z", "data_store": "account_service",
         "collection": "customer_profiles", "records": []}]}
    _, _, issues, _ = run_json(data)
    assert "after the bundle export" in issues[0].message


def test_service_export_states():
    data = {"exported_at": "2026-09-15T11:44:55Z", "service_exports": {
        "account_service": {"profile_lookups": [
            {"observed_at": "2026-09-15T09:00:00Z", "profile": None, "resource_id": "account_1",
             "status_code": 404}]},
        "backup_service": {"restore_entries": [
            {"observed_at": "2026-09-15T09:01:00Z", "resource_id": "backup_1",
             "restore_allowed": False, "customer_reference": "user_7", "customer_email": None}]},
        "search_service": {"index_queries": [
            {"observed_at": "2026-09-15T09:02:00Z", "query": "a@x.com", "resource_id": "search_1",
             "hits": {"documents": [], "total": 0}},
            {"observed_at": "2026-09-15T09:03:00Z", "query": "b@x.com", "resource_id": "search_2",
             "hits": {"documents": [{"resource_id": "search_2", "email": "b@x.com"}], "total": 1}}]},
    }}
    observations, _, issues, report = run_json(data)
    assert issues == [] and report.parser == "json/service"
    state = {o.resource_id: o.condition for o in observations}
    assert state == {"account_1": Condition.ABSENT, "backup_1": Condition.RESTORE_BLOCKED,
                     "search_1": Condition.ABSENT, "search_2": Condition.PRESENT}
    missed = by_resource(observations, "search_1")[0]
    assert missed.identity_refs[0].basis == "query"
    assert by_resource(observations, "backup_1")[0].exposure is Exposure.REFERENCE_ONLY


def test_ledger_and_collection_families():
    ledger = {"records": [{"recorded_at": "2026-08-12T09:00:00Z", "system": "integration_service",
                           "resource_id": "data_flow_1", "record_type": "DATA_FLOW",
                           "result": "ACTIVE", "subject_reference": "a@x.com"}]}
    observations, _, _, report = run_json(ledger)
    assert report.parser == "json/ledger" and observations[0].condition is Condition.ACTIVE
    collection = {"captured_at": "2026-09-07T09:45:00Z", "collection": "customer_analytics_reports",
                  "records": [{"resource_id": "analytics_1", "user_id": None, "email": None}]}
    observations, coverages, _, report = run_json(collection)
    assert report.parser == "json/collection"
    assert observations[0].system_id == "analytics_platform"
    assert observations[0].exposure is Exposure.UNLINKED
    assert coverages[0].resource_ids == {"analytics_1"}


@pytest.mark.parametrize("data", [[1, 2], {"something": "else"}])
def test_unrecognised_json_fails_clearly(data):
    _, _, issues, report = run_json(data)
    assert report.status is FileStatus.FAILED and issues[0].severity == "error"


def test_bad_item_time_and_missing_resource_are_reported():
    data = {"exported_at": "2026-09-15T11:44:55Z", "service_exports": {"export_service": {
        "exports": [{"observed_at": "yesterday", "resource_id": "export_1"},
                    {"observed_at": "2026-09-15T09:00:00Z", "owner_email": "a@x.com"}]}}}
    observations, _, issues, report = run_json(data)
    assert observations == [] and report.status is FileStatus.PARTIAL
    assert len(issues) == 2


# --- SQL ------------------------------------------------------------------------------------

INVENTORY = """-- Captured at: 2026-09-16T08:37:39Z
CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);
INSERT INTO snapshot_scope VALUES('account_service','2026-09-16T08:30:00Z');
CREATE TABLE customer_profiles (resource_id TEXT, customer_reference TEXT, full_name TEXT,
  email TEXT, phone TEXT, email_hash TEXT, account_status TEXT, updated_at TEXT);
INSERT INTO customer_profiles VALUES('account_1',NULL,NULL,'A@x.com',NULL,NULL,'DELETION_PENDING','x');
CREATE TABLE support_records (resource_id TEXT, customer_reference TEXT, requester_name TEXT,
  requester_email TEXT, requester_phone TEXT, message_excerpt TEXT, updated_at TEXT);
INSERT INTO support_records VALUES('support_1',NULL,NULL,'b@x.com',NULL,'hi','x');
"""

REPLICATED = """-- Captured at: 2026-05-19T06:00:00Z
CREATE TABLE customer_data_primary (resource_id TEXT, origin_system TEXT, collection_name TEXT,
  record_json TEXT, updated_at TEXT);
CREATE TABLE customer_data_replica_eu_west (resource_id TEXT, origin_system TEXT,
  collection_name TEXT, record_json TEXT, updated_at TEXT);
INSERT INTO customer_data_primary VALUES('account_2','account_service','customer_profiles',
  '{"resource_id":"account_2","customer_reference":"user_2","email":"c@x.com"}','t');
INSERT INTO customer_data_replica_eu_west VALUES('account_2','account_service','customer_profiles',
  '{"resource_id":"account_2","customer_reference":"user_2","email":"c@x.com"}','t');
INSERT INTO customer_data_replica_eu_west VALUES('account_3','account_service','customer_profiles',
  '{"resource_id":"account_3","customer_reference":"user_3","email":"d@x.com"}','t');
INSERT INTO customer_data_replica_eu_west VALUES('account_4','account_service','customer_profiles',
  'not json','t');
"""


def run_sql(script, captured=None):
    connection = open_sql_snapshot(script)
    names = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    tables = {n: [dict(r) for r in connection.execute(f'SELECT * FROM "{n}"')] for n in names}
    return extract_sql(evidence("sql", captured or ts(16, 8, 37)), script, tables)


def test_inventory_covers_only_systems_in_scope():
    observations, coverages, issues, _ = run_sql(INVENTORY)
    assert [c.system_id for c in coverages] == ["account_service"]
    assert coverages[0].observed_at == ts(16, 8, 30)  # the scope row's time, not the header's
    assert [o.resource_id for o in observations] == ["account_1"]
    assert any("scope does not list" in i.message for i in issues)
    assert observations[0].exposure is Exposure.IDENTIFIABLE


def test_replica_drift_is_visible_per_copy():
    observations, coverages, issues, report = run_sql(REPLICATED, captured=ts(19, 6, month=5))
    copies = {(o.resource_id, o.replica) for o in observations}
    assert copies == {("account_2", "primary"), ("account_2", "replica_eu_west"),
                      ("account_3", "replica_eu_west")}
    primary = next(c for c in coverages if c.replica == "primary")
    assert primary.resource_ids == {"account_2"}  # account_3 survives only in the replica
    assert any("unreadable record_json" in i.message for i in issues)
    assert report.status is FileStatus.PARTIAL


def test_unknown_table_is_reported():
    _, _, issues, _ = run_sql("CREATE TABLE mystery (x TEXT); INSERT INTO mystery VALUES('y');")
    assert any("unrecognised table" in i.message for i in issues)


def test_search_primary_links_its_account():
    script = ("-- Captured at: 2026-09-10T08:45:00Z\nCREATE TABLE search_documents_primary "
              "(resource_id TEXT, display_name TEXT, email TEXT, source_resource_id TEXT, "
              "indexed_at TEXT);\nINSERT INTO search_documents_primary VALUES"
              "('search_001','Jonas Berg','jonas@x.com','account_006','t');")
    observations, coverages, _, _ = run_sql(script, captured=ts(10, 8, 45))
    assert observations[0].related_resources == ("account_006",)
    assert observations[0].replica == "primary" and coverages[0].system_id == "search_service"
