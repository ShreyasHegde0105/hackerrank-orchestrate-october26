"""load_dataset and EvidenceStore on a tiny synthetic dataset directory."""
import csv
import sqlite3
from datetime import datetime, timezone

import pytest

from audit.evidence_store import EvidenceStore, open_sql_snapshot
from audit.loader import DatasetError, load_dataset, parse_timestamp

HEADERS = {
    "deletion_requests.csv": ["case_id", "email", "requested_at", "reported_status",
                              "reported_at", "audit_at", "request_text"],
    "user_identities.csv": ["user_id", "name", "email", "phone", "email_hash", "updated_at"],
    "user_audit_log.csv": ["audit_id", "user_id", "event_time", "field_name", "old_value",
                           "new_value"],
    "system_activity.csv": ["activity_id", "event_time", "event_type", "source_system",
                            "target_system", "resource_id", "reported_result", "details"],
    "deletion_rules.csv": ["rule_id", "data_category", "condition", "required_action"],
    "evidence_files.csv": ["file_id", "file_path", "origin", "captured_at"],
}

T1, T2, T3 = "2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z", "2026-06-03T00:00:00Z"


def base_rows():
    return {
        "deletion_requests.csv": [["case_1", "a@x.com", T1, "COMPLETE", T2, T3,
                                   "Please delete my account, thanks."]],
        "user_identities.csv": [["user_1", "A Person", "a@x.com", "+1 202 555 0100", "h", T1]],
        "user_audit_log.csv": [["audit_1", "user_1", T1, "phone", "+1 202 555 0199",
                                "+1 202 555 0100"]],
        "system_activity.csv": [
            ["activity_2", T3, "DELETE_RECORD", "w", "account_service", "account_1", "SUCCESS", "d"],
            ["activity_1", T1, "SYNC_RECORD", "w", "account_service", "account_1", "FAILED", "d"],
        ],
        "deletion_rules.csv": [["rule_01", "USER_PROFILE", "Always", "DELETE"]],
        "evidence_files.csv": [
            ["evidence_1", "evidence/db.sql", "capture", T2],
            ["evidence_2", "evidence/data.json", "capture", T3],
            ["evidence_3", "evidence/shot.png", "capture", T1],
        ],
    }


def write_dataset(root, rows):
    for name, header in HEADERS.items():
        with (root / name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows[name])
    evidence = root / "evidence"
    evidence.mkdir(exist_ok=True)
    (evidence / "db.sql").write_text(
        "PRAGMA foreign_keys=ON;\nBEGIN TRANSACTION;\n"
        "CREATE TABLE t (resource_id TEXT PRIMARY KEY, email TEXT);\n"
        "INSERT INTO t VALUES('account_1','a@x.com');\nCOMMIT;\n", encoding="utf-8")
    (evidence / "data.json").write_text('{"records": [{"resource_id": "search_1"}]}',
                                        encoding="utf-8")
    (evidence / "shot.png").write_bytes(b"\x89PNG")
    return root


@pytest.fixture
def dataset_dir(tmp_path):
    return write_dataset(tmp_path, base_rows())


def test_loads_typed_records(dataset_dir):
    ds = load_dataset(dataset_dir)
    assert ds.requests[0].audit_at == datetime(2026, 6, 3, tzinfo=timezone.utc)
    assert ds.users["user_1"].name == "A Person"
    assert ds.rules["USER_PROFILE"].required_action == "DELETE"
    assert ds.evidence_files["evidence_3"].kind == "png"
    assert ds.warnings == []


def test_activity_is_sorted_by_time_not_file_order(dataset_dir):
    ds = load_dataset(dataset_dir)
    assert [a.activity_id for a in ds.activities] == ["activity_1", "activity_2"]
    assert [a.activity_id for a in ds.activity_by_resource["account_1"]] == ["activity_1",
                                                                              "activity_2"]


def test_citable_time_for_files_activities_and_audit_rows(dataset_dir):
    ds = load_dataset(dataset_dir)
    assert ds.citable_time("evidence_1") == parse_timestamp(T2)
    assert ds.citable_time("activity_2") == parse_timestamp(T3)
    assert ds.citable_time("audit_1") == parse_timestamp(T1)
    assert ds.citable_time("evidence_99") is None


@pytest.mark.parametrize("file,row,col,value,message", [
    ("user_identities.csv", 0, 0, None, "repeats user_id"),
    ("user_identities.csv", 0, 5, "2026-06-01 00:00", "invalid timestamp"),
    ("user_audit_log.csv", 0, 3, "name", "unknown field_name 'name'"),
    ("deletion_requests.csv", 0, 3, "DONE", "unknown reported_status"),
    ("deletion_requests.csv", 0, 0, "", "empty case_id"),
    ("evidence_files.csv", 0, 1, "evidence/missing.log", "evidence file missing"),
    ("system_activity.csv", 0, 1, "yesterday", "invalid timestamp"),
])
def test_malformed_inputs_fail_with_location(tmp_path, file, row, col, value, message):
    rows = base_rows()
    if value is None:  # duplicate the row
        rows[file].append(list(rows[file][row]))
    else:
        rows[file][row][col] = value
    write_dataset(tmp_path, rows)
    with pytest.raises(DatasetError, match=message) as error:
        load_dataset(tmp_path)
    assert file in str(error.value)


def test_audit_event_for_unknown_user_is_a_warning(tmp_path):
    rows = base_rows()
    rows["user_audit_log.csv"][0][1] = "user_404"
    ds = load_dataset(write_dataset(tmp_path, rows))
    assert "unknown user user_404" in ds.warnings[0]


def test_missing_dataset_file_is_reported(dataset_dir):
    (dataset_dir / "deletion_rules.csv").unlink()
    with pytest.raises(DatasetError, match="not found"):
        load_dataset(dataset_dir)


# --- evidence store -------------------------------------------------------------------

def test_store_reads_sql_json_and_text(dataset_dir):
    store = EvidenceStore(load_dataset(dataset_dir))
    assert store.sql_tables("evidence_1") == {"t": [{"resource_id": "account_1", "email": "a@x.com"}]}
    assert store.json("evidence_2")["records"][0]["resource_id"] == "search_1"
    assert store.json("evidence_2") is store.json("evidence_2")  # cached
    assert "CREATE TABLE" in store.text("evidence_1")
    assert store.path("evidence_3").name == "shot.png"


def test_store_refuses_wrong_kinds_and_unknown_ids(dataset_dir):
    store = EvidenceStore(load_dataset(dataset_dir))
    with pytest.raises(DatasetError, match="not text"):
        store.text("evidence_3")
    with pytest.raises(DatasetError, match="not a SQL snapshot"):
        store.sql("evidence_2")
    with pytest.raises(DatasetError, match="unknown evidence"):
        store.text("evidence_99")


def test_captured_by_filters_on_manifest_time(dataset_dir):
    store = EvidenceStore(load_dataset(dataset_dir))
    assert [f.file_id for f in store.captured_by(parse_timestamp(T2))] == ["evidence_3", "evidence_1"]


def test_invalid_json_is_a_clear_error(dataset_dir):
    (dataset_dir / "evidence" / "data.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(DatasetError, match="evidence_2: invalid JSON"):
        EvidenceStore(load_dataset(dataset_dir)).json("evidence_2")


def test_loaded_snapshot_is_read_only(dataset_dir):
    connection = EvidenceStore(load_dataset(dataset_dir)).sql("evidence_1")
    for statement in ("DELETE FROM t", "INSERT INTO t VALUES('x','y')", "DROP TABLE t",
                      "CREATE TABLE u (x)", "ATTACH DATABASE 'x.db' AS x",
                      "PRAGMA writable_schema=ON"):
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(statement)
    assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 1


def test_untrusted_script_cannot_attach_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(DatasetError, match="failed to load"):
        open_sql_snapshot("ATTACH DATABASE 'evil.db' AS e; CREATE TABLE e.t (x);")
    with pytest.raises(DatasetError, match="failed to load"):
        open_sql_snapshot("PRAGMA writable_schema=ON;")
    assert not (tmp_path / "evil.db").exists()


def test_broken_sql_is_a_clear_error():
    with pytest.raises(DatasetError, match="snap: SQL snapshot failed to load"):
        open_sql_snapshot("CREATE TABLE (", "snap")
