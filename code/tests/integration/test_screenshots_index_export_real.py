"""Manual screenshot transcriptions, ground-truth comparison, lookup index and
JSONL export on the real dataset."""
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

import screenshot_accuracy
import store_manual_transcriptions
from audit.evidence.duplicates import analyse_duplicates
from audit.evidence.export import read_jsonl, write_jsonl
from audit.evidence.extract import extract_evidence
from audit.evidence.index import EvidenceIndex
from audit.evidence.model import Confidence, FileStatus
from audit.identity import IdentityIndex
from audit.loader import load_dataset
from llm.vision import MANUAL_MODEL, ScreenshotTranscriber


@pytest.fixture(scope="module")
def run():
    dataset = load_dataset()
    transcriber = ScreenshotTranscriber.manual()
    result = extract_evidence(dataset, transcriber=transcriber)
    return dataset, result, transcriber, IdentityIndex.from_dataset(dataset)


def test_every_file_is_extracted_with_manual_screenshots(run):
    dataset, result, transcriber, _ = run
    assert {r.status for r in result.files.values()} == {FileStatus.PARSED}
    png = [r for r in result.files.values() if r.kind == "png"]
    assert len(png) == 11 and all(r.details["transcription"] == "manual" for r in png)
    assert [u.method for u in transcriber.usage.records] == ["manual"] * 11
    assert transcriber.usage.api_calls() == []  # no model was called
    screenshot_obs = [o for o in result.observations if o.source_kind == "png"]
    assert len(screenshot_obs) == 31
    assert {o.confidence for o in screenshot_obs} == {Confidence.MANUAL_TRANSCRIPTION}


def test_cached_transcriptions_are_tied_to_their_images(run):
    dataset, *_ = run
    source = json.loads(store_manual_transcriptions.SOURCE.read_text(encoding="utf-8"))
    entries, problems = store_manual_transcriptions.build_entries(dataset, source)
    assert problems == [] and len(entries) == 11
    cache = ScreenshotTranscriber.manual().cache
    for key, entry in entries.items():
        stored = cache.get(key)
        assert stored == entry, entry["subject"]  # cache matches the reviewed source
        image = Path(dataset.root / dataset.evidence_files[entry["subject"]].file_path)
        assert stored["image_sha256"] == hashlib.sha256(image.read_bytes()).hexdigest()
        assert (stored["model"], stored["method"]) == (MANUAL_MODEL, "manual")
    assert store_manual_transcriptions.main(["--check"]) == 0


def test_changed_image_bytes_do_not_reuse_a_transcription(tmp_path, run):
    dataset, *_ = run
    image = (dataset.root / dataset.evidence_files["evidence_04"].file_path).read_bytes()
    from llm.vision import VisionUnavailable
    with pytest.raises(VisionUnavailable, match="no manual transcription"):
        ScreenshotTranscriber.manual().transcribe(image + b"\x00", "evidence_04")


def test_screenshots_match_ground_truth(run):
    _, result, _, _ = run
    rows = screenshot_accuracy.compare(result.observations)
    mismatches = [r for r in rows if not r["match"]]
    assert mismatches == [] and len(rows) == 165
    assert Counter(r["fact"] for r in rows)["record_found"] == 31


@pytest.mark.parametrize("mutate,fact", [
    (lambda gt: gt["files"]["evidence_60"]["records"][0]["identifiers"].append("x@y.com"), "identifiers"),
    (lambda gt: gt["files"]["evidence_14"]["records"][0].update(condition="DISABLED"), "condition"),
    (lambda gt: gt["files"]["evidence_09"].update(screen_time="2026-09-01T06:00:00Z"), "screen_time"),
    (lambda gt: gt["files"]["evidence_04"]["records"][0].update(related=[]), "related"),
    (lambda gt: gt["files"]["evidence_32"]["records"].pop(), "extra_record"),
    # renaming a key: the expected record is missing AND the real one becomes extra
    (lambda gt: gt["files"]["evidence_42"]["records"][0].update(key="tombstone_17"),
     "record_found,extra_record"),
])
def test_comparison_detects_each_kind_of_mismatch(run, mutate, fact):
    _, result, _, _ = run
    truth = json.loads(screenshot_accuracy.GROUND_TRUTH.read_text(encoding="utf-8"))
    altered = copy.deepcopy(truth)
    mutate(altered)
    rows = screenshot_accuracy.compare(result.observations, altered)
    assert [r["fact"] for r in rows if not r["match"]] == fact.split(",")


def test_screen_anomalies_and_the_marketing_conflict_are_reported(run):
    _, result, _, identities = run
    screen_warnings = sorted(i.evidence_id for i in result.issues
                             if i.severity == "warning" and i.evidence_id in
                             {"evidence_09", "evidence_19", "evidence_42"})
    assert screen_warnings == ["evidence_09", "evidence_19", "evidence_42"]
    conflicts = EvidenceIndex(result, identities).link_conflicts()
    assert [(l.resource_id, l.user_id, o.evidence_id) for l, o, _ in conflicts] == [
        ("marketing_001", "user_09", "evidence_60")]


def test_index_partitions_every_observation_without_leakage(run):
    _, result, _, identities = run
    index = EvidenceIndex(result, identities)
    counts = index.counts()
    assert counts["conflicted_observations"] == 0
    assert (counts["attributed_observations"] + counts["unattributed_observations"]
            + counts["no_identity_observations"]) == len(result.observations)
    for user_id in ("user_29", "user_10", "user_04", "user_413"):
        for obs in index.for_person(user_id).observations:
            assert index.owners(obs) == {user_id}
    assert set(index.for_person("user_29").resources) >= {
        "analytics_5208", "audit_record_3954", "data_flow_3827", "search_1993", "support_5281"}
    namesakes = (index.for_person("user_04").resources, index.for_person("user_413").resources)
    assert not set(namesakes[0]) & set(namesakes[1])  # same name, different people


def test_duplicate_accounting_on_real_data(run):
    _, result, _, _ = run
    summary = analyse_duplicates(result.observations).summary()
    assert summary["within_file_groups"] == 0 and summary["duplicate_fact_groups"] == 0
    assert summary["repeated_state_groups"] == 230
    assert not [i for i in result.issues if "identical observations" in i.message]


def test_export_round_trip_and_repeated_runs_are_byte_identical(tmp_path, run):
    dataset, result, _, _ = run
    first = tmp_path / "first.jsonl"
    assert write_jsonl(result.observations, first) == len(result.observations)
    assert sorted(read_jsonl(first), key=lambda o: o.obs_id) == \
        sorted(result.observations, key=lambda o: o.obs_id)
    rerun = extract_evidence(dataset, transcriber=ScreenshotTranscriber.manual())
    second = tmp_path / "second.jsonl"
    write_jsonl(rerun.observations, second)
    assert first.read_bytes() == second.read_bytes()
    ids = [json.loads(line)["obs_id"] for line in first.read_text(encoding="utf-8").splitlines()]
    assert len(ids) == len(set(ids))
