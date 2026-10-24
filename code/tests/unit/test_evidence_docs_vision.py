"""Text handovers, PDFs (from page text), screenshots (from transcriptions), the vision
client (with a stand-in API client), classification rules and the orchestrator."""
import json
from types import SimpleNamespace

import pytest

from audit.evidence.classify import classify_category, exposure_of, identity_refs
from audit.evidence.extract import extract_evidence
from audit.evidence.model import Condition, Confidence, Exposure, FileStatus, TimeBasis
from audit.evidence.pdf_documents import extract_pdf
from audit.evidence.screenshots import observations_from_transcription
from audit.evidence.text_records import extract_text
from audit.models import IdentifierKind
from evidence_helpers import by_resource, evidence, ts
from llm.cache import JsonCache, UsageLedger
from llm.vision import ScreenshotTranscriber, VisionError, VisionUnavailable


# --- text --------------------------------------------------------------------------------

def test_handover_rows_become_lookups():
    observations, issues, report = extract_text(evidence("txt"), [
        "CUSTOMER PLATFORM - SHIFT HANDOVER", "Snapshot: 2026-10-01T12:00:00Z", "",
        "- 2026-10-01T12:00:00Z | account_service | account_146 | USER_PROFILE | NOT_FOUND | "
        "E.L@yahoo.com",
        "- 2026-10-01T12:00:00Z | integration_service | data_flow_1 | DATA_FLOW | ACTIVE | user_9",
        "- a list line that is not a record"])
    assert [o.condition for o in observations] == [Condition.ABSENT, Condition.ACTIVE]
    assert observations[0].identity_refs[0].value == "e.l@yahoo.com"
    assert issues[0].locator == "line 6" and report.status is FileStatus.PARTIAL


# --- pdf ---------------------------------------------------------------------------------

ATTACHMENT_PAGES = [
    "DELIVERY ADDRESS CORRECTION REQUEST\nSupport reference CS-4827 / support_001\n"
    "Detail Priya Raman\nDetail priya.raman@gmail.com\nDetail +91 98765 73142\n",
    "Record file delivery_address_correction.pdf\ncaptured 12 Sep 2026 07 30 UTC\n"]
REGISTER_PAGES = [
    "DISPUTED ORDERS AND TAX RECORDS REGISTER\ngenerated 29 May 2026 08 00 UTC\n",
    "1 ORD-20264118 | user_08 | Card dispute | 04 May 2026 | Retain until 04 May 2033 | AR\n"
    "2 ORD-20264600 | user_29 | Tax record | 28 May 2026 | Retain until 28 May 2033 | SK\n"]


def test_pdf_document_observation_for_each_resource():
    observations, issues, report = extract_pdf(evidence("pdf", ts(12, 7, 30)), ATTACHMENT_PAGES)
    (obs,) = observations
    assert (obs.resource_id, obs.data_category, obs.condition) == (
        "support_001", "SUPPORT_ATTACHMENT", Condition.PRESENT)
    assert obs.observed_at == ts(12, 7, 30) and obs.time_basis is TimeBasis.DOCUMENT
    assert {r.kind for r in obs.identity_refs} == {IdentifierKind.EMAIL, IdentifierKind.PHONE}
    assert obs.exposure is Exposure.IDENTIFIABLE and "CS-4827" in obs.labels
    assert issues == [] and report.status is FileStatus.PARSED


def test_register_rows_keep_their_own_identifiers():
    observations, _, _ = extract_pdf(evidence("pdf", ts(29, 8, month=5)), REGISTER_PAGES)
    rows = [o for o in observations if o.extractor == "pdf/table_row"]
    assert [[r.value for r in o.identity_refs] for o in rows] == [["user_08"], ["user_29"]]
    assert rows[1].labels == ("ORD-20264600",) and rows[1].condition is Condition.MENTIONED
    assert rows[1].attributes["retain_until"] == "2033-05-28T00:00:00Z"
    assert all(o.resource_id is None for o in observations)


def test_pdf_time_after_capture_is_ignored_and_empty_pdf_fails():
    observations, issues, _ = extract_pdf(evidence("pdf", ts(1, month=6)), ATTACHMENT_PAGES)
    assert observations[0].time_basis is TimeBasis.MANIFEST
    assert "after the manifest capture" in issues[0].message
    _, issues, report = extract_pdf(evidence("pdf"), ["", "  "])
    assert report.status is FileStatus.FAILED and "no text layer" in issues[0].message


# --- screenshots -------------------------------------------------------------------------

TRANSCRIPTION = {
    "page_title": "Haruka Sato", "application": "Customer Operations",
    "captured_at_text": "Captured 13 Feb 2026 08:00 UTC",
    "records": [
        {"resource_id": "account_5762", "label": None, "kind_hint": "customer profile",
         "status_text": "Silver", "identifiers": [
             {"field": "Email", "value": "haruka.s1838@zoho.com"},
             {"field": "Name", "value": "Haruka Sato"}, {"field": "ID", "value": "user_11"}],
         "fields": [], "related_references": ["support_9538", "CS-8538"]},
        {"resource_id": "NOT AN ID", "label": "delivery_confirmation_19.pdf",
         "kind_hint": "ticket attachment", "status_text": None, "identifiers": [],
         "fields": [], "related_references": []},
        {"resource_id": "data_flow_1", "label": None, "kind_hint": "connector",
         "status_text": "Paused", "identifiers": [], "fields": [], "related_references": []},
        {"resource_id": "backup_9", "label": None, "kind_hint": "backup archive",
         "status_text": None, "identifiers": [],
         "fields": [{"field": "Restore access", "value": "Allowed"}], "related_references": []},
    ],
    "other_identifiers": ["sj.park@naver.com"],
}


def test_transcription_becomes_ai_extracted_observations():
    observations, issues = observations_from_transcription(
        evidence("png", ts(13, 9, month=2)), TRANSCRIPTION)
    account = by_resource(observations, "account_5762")[0]
    assert account.confidence is Confidence.AI_EXTRACTED
    assert account.observed_at == ts(13, 8, month=2) and account.time_basis is TimeBasis.DOCUMENT
    assert {r.value for r in account.identity_refs} == {"haruka.s1838@zoho.com", "user_11"}
    assert account.exposure is Exposure.IDENTIFIABLE and account.related_resources == ("support_9538",)
    attachment = next(o for o in observations if "delivery_confirmation_19.pdf" in o.labels)
    assert attachment.resource_id is None and "NOT AN ID" in attachment.labels
    assert attachment.data_category == "SUPPORT_ATTACHMENT"
    assert by_resource(observations, "data_flow_1")[0].condition is Condition.DISABLED
    assert by_resource(observations, "backup_9")[0].condition is Condition.RESTORE_ALLOWED
    loose = next(o for o in observations if o.locator == "page")
    assert loose.identity_refs[0].value == "sj.park@naver.com"
    assert any("not a resource ID" in i.message for i in issues)


class FakeClient:
    def __init__(self, reply=None, stop_reason="end_turn", error=None):
        self.calls = []
        self.reply, self.stop_reason, self.error = reply, stop_reason, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=self.reply)],
            usage=SimpleNamespace(input_tokens=1500, output_tokens=300))


def transcriber(tmp_path, client, allow_api=True):
    return ScreenshotTranscriber(cache=JsonCache(tmp_path, "shots"), usage=UsageLedger(),
                                 client=client, model="test-model", allow_api=allow_api)


def test_transcriber_calls_once_then_reuses_cache(tmp_path):
    client = FakeClient(reply=json.dumps(TRANSCRIPTION))
    first = transcriber(tmp_path, client)
    assert first.transcribe(b"png-bytes", "evidence_04") == TRANSCRIPTION
    request = client.calls[0]
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["messages"][0]["content"][0]["type"] == "image"
    second = transcriber(tmp_path, FakeClient(error=AssertionError("must not be called")))
    assert second.transcribe(b"png-bytes", "evidence_04") == TRANSCRIPTION
    assert [r.cache_hit for r in first.usage.records + second.usage.records] == [False, True]
    assert first.usage.records[0].input_tokens == 1500


def test_transcriber_refuses_to_guess(tmp_path):
    with pytest.raises(VisionUnavailable):
        transcriber(tmp_path, FakeClient(), allow_api=False).transcribe(b"x")
    with pytest.raises(VisionError, match="declined"):
        transcriber(tmp_path, FakeClient(reply="{}", stop_reason="refusal")).transcribe(b"y")
    with pytest.raises(VisionError, match="not valid JSON"):
        transcriber(tmp_path, FakeClient(reply="not json")).transcribe(b"z")
    with pytest.raises(VisionError, match="records list"):
        transcriber(tmp_path, FakeClient(reply='{"page_title": "x"}')).transcribe(b"w")
    with pytest.raises(VisionError, match="request failed"):
        transcriber(tmp_path, FakeClient(error=RuntimeError("boom"))).transcribe(b"v")
    assert not list((tmp_path / "shots").glob("*.json")) if (tmp_path / "shots").exists() else True


# --- classification rules ------------------------------------------------------------------

@pytest.mark.parametrize("args,expected", [
    (("analytics_1", "profiles", {"email_opt_in": "true"}, "ANALYTICS_PROFILE"), "ANALYTICS_PROFILE"),
    (("analytics_1", "profiles", {"email_opt_in": "true"}), "MARKETING_PROFILE"),
    (("support_1", "support_conversations", {"file_name": "a.pdf"}), "SUPPORT_ATTACHMENT"),
    (("support_1", None, {"message_text": "hello"}), "SUPPORT_MESSAGE"),
    (("account_1", "customer_profiles", {}), "USER_PROFILE"),
    (("tombstone_1", None, {}), "ACCOUNT_TOMBSTONE"),
    (("support_1", None, {}), None),
    (("object_1", None, {}), None),
])
def test_category_precedence(args, expected):
    assert classify_category(*args)[0] == expected


@pytest.mark.parametrize("fields,exposure", [
    ({"email": "a@x.com", "user_id": "user_1"}, Exposure.IDENTIFIABLE),
    ({"full_name": "A B"}, Exposure.IDENTIFIABLE),
    ({"message_text": "send to a@x.com"}, Exposure.IDENTIFIABLE),
    ({"message_text": "[REDACTED]", "subject_reference": "user_1"}, Exposure.REFERENCE_ONLY),
    ({"subject_reference": "a@x.com"}, Exposure.IDENTIFIABLE),
    ({"email": None, "phone": "null", "user_id": None}, Exposure.UNLINKED),
    ({"rows": "1"}, Exposure.UNKNOWN),
])
def test_exposure(fields, exposure):
    assert exposure_of(fields) is exposure


def test_identity_refs_never_include_names_or_resource_ids():
    refs = identity_refs({"full_name": "Sam Lee", "lookup_key": "analytics_6980",
                          "message_text": "call +44 7700 900111 or mail x@y.com",
                          "subject_reference": "user_5"}, "same_record")
    assert sorted((r.kind.value, r.field) for r in refs) == [
        ("email", "message_text"), ("phone", "message_text"), ("user_id", "subject_reference")]


# --- orchestrator ----------------------------------------------------------------------------

def test_one_broken_file_does_not_stop_the_run(tmp_path):
    from test_dataset_loader import base_rows, write_dataset
    from audit.loader import load_dataset
    root = write_dataset(tmp_path, base_rows())
    (root / "evidence" / "data.json").write_text("{broken", encoding="utf-8")
    result = extract_evidence(load_dataset(root))
    assert result.files["evidence_2"].status is FileStatus.FAILED
    # the synthetic snapshot's generic table "t" is read but reported as unrecognised
    assert result.files["evidence_1"].status is FileStatus.PARTIAL
    assert any("unrecognised table" in i.message for i in result.issues)
    assert result.files["evidence_3"].status is FileStatus.PENDING_AI
    assert any(i.evidence_id == "evidence_2" and i.severity == "error" for i in result.issues)
