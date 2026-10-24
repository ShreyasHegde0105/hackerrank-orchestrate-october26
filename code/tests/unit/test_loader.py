import json

import pytest

from audit.loader import DATASET_DIR, DatasetError, read_csv
from conftest import SAMPLE_OUTPUT, SAMPLE_REQUESTS

DATASET_FILES = {
    "deletion_requests.csv": (518, ("case_id", "email", "requested_at", "reported_status",
                                    "reported_at", "audit_at", "request_text")),
    "sample_deletion_requests.csv": (32, ("case_id", "email", "audit_at", "request_text")),
    "sample_output.csv": (32, ("case_id", "deletion_status", "steps_json")),
    "user_identities.csv": (850, ("user_id", "name", "email", "phone", "email_hash", "updated_at")),
    "user_audit_log.csv": (306, ("audit_id", "user_id", "event_time", "field_name",
                                 "old_value", "new_value")),
    "system_activity.csv": (4011, ("activity_id", "event_time", "event_type", "source_system",
                                   "target_system", "resource_id", "reported_result", "details")),
    "deletion_rules.csv": (15, ("rule_id", "data_category", "condition", "required_action")),
    "evidence_files.csv": (94, ("file_id", "file_path", "origin", "captured_at")),
}


@pytest.mark.parametrize("name", sorted(DATASET_FILES))
def test_dataset_files_load_with_expected_shape(name):
    expected_rows, columns = DATASET_FILES[name]
    assert len(read_csv(DATASET_DIR / name, columns)) == expected_rows


def test_quoted_request_text_with_commas_survives():
    rows = {r["case_id"]: r for r in read_csv(SAMPLE_REQUESTS)}
    assert rows["case_01"]["request_text"].startswith("Hi, I haven")
    assert rows["case_01"]["request_text"].endswith("attached to it.")


def test_every_sample_steps_json_parses_as_array():
    for row in read_csv(SAMPLE_OUTPUT):
        assert isinstance(json.loads(row["steps_json"]), list)


def test_every_manifest_file_exists():
    for row in read_csv(DATASET_DIR / "evidence_files.csv"):
        assert (DATASET_DIR / row["file_path"]).is_file(), row["file_path"]


def test_missing_file_raises_clear_error(tmp_path):
    with pytest.raises(DatasetError, match="not found"):
        read_csv(tmp_path / "nope.csv")


def test_missing_column_raises_clear_error(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(DatasetError, match=r"missing required column\(s\) \['c'\]"):
        read_csv(path, ("a", "c"))


def test_empty_file_raises_clear_error(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(DatasetError, match="empty"):
        read_csv(path)


@pytest.mark.parametrize("body,message", [("1,2,3\n", "more fields"), ("1\n", "fewer fields")])
def test_ragged_row_raises_clear_error(tmp_path, body, message):
    path = tmp_path / "x.csv"
    path.write_text("a,b\n" + body, encoding="utf-8")
    with pytest.raises(DatasetError, match=message):
        read_csv(path)
