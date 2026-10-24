import pytest

from audit.evidence.logs import extract_log, parse_log_line
from audit.evidence.model import Condition, Exposure, FileStatus, TimeBasis
from audit.models import IdentifierKind
from evidence_helpers import by_resource, evidence, ts

LAYOUTS = {
    "key_value": '2026-09-15T08:51:45Z level=DEBUG service=search_service trace_id=tr-1 '
                 'event=index_query_completed resource_id=search_7 result=hit',
    "nested_message": '2026-09-30T11:51:03Z requestId=tr-2 level=DEBUG function=profile_service '
                      'message="instance=\\"p-3\\" resource_id=\\"account_7\\" '
                      'event=\\"profile_lookup_completed\\" rows=\\"1\\" http_status=\\"200\\""',
    "pipe": '2026-07-03T07:51:32Z | DEBUG | security_service | tr-3 | audit_record_read_completed | '
            'resource_id="audit_record_7" rows="1" correlation_key="user_211"',
    "bracket": '2026-07-28T05:51:18Z [DEBUG] [account_service] [tr-4] profile_lookup_completed: '
               'resource_id="account_8" rows="0" http_status="404"',
    "json": '2026-09-10T08:56:17Z {"severity":"DEBUG","service":"account_service",'
            '"trace_id":"tr-5","event":"profile_lookup_completed","resource_id":"account_9",'
            '"rows":"1"}',
    "syslog": '2026-03-25T08:53:25Z <15>1 node-8 account_service tr-6 - - level=DEBUG '
              'instance="account-5" resource_id="account_10" event="profile_lookup_completed" '
              'rows="1" http_status="200"',
}


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_each_layout_normalises_to_service_trace_event(layout):
    found, fields = parse_log_line(LAYOUTS[layout])
    assert found == layout
    assert fields["event"].endswith("_completed")
    assert (fields.get("trace_id") or fields.get("requestId")).startswith("tr-")
    assert fields["resource_id"].split("_")[0] in {"search", "account", "audit"}


def test_nested_message_fields_are_unescaped():
    _, fields = parse_log_line(LAYOUTS["nested_message"])
    assert fields["instance"] == "p-3" and fields["http_status"] == "200"
    assert fields["service"] == "profile_service"


@pytest.mark.parametrize("line", ["just some text", "2026-09-10T08:56:17Z {broken json"])
def test_unrecognised_lines_raise(line):
    with pytest.raises(ValueError):
        parse_log_line(line)


def run(lines, captured=None):
    return extract_log(evidence("log", captured), lines)


def test_identifier_on_started_line_joins_result_by_trace():
    observations, issues, report = run([
        "2026-09-26T07:49:50Z level=DEBUG service=account_service trace_id=rb-1 "
        "event=profile_lookup_started resource_id=account_2064 lookup_key=Charlotte.W@outlook.com",
        "2026-09-26T07:49:50Z level=INFO service=account_service trace_id=rb-1 "
        "event=profile_lookup_completed resource_id=account_2064 rows=0 http_status=404",
    ])
    assert issues == [] and report.status is FileStatus.PARSED
    (obs,) = observations  # the started line is a query, not an observation
    assert obs.condition is Condition.ABSENT and obs.time_basis is TimeBasis.LOG_LINE
    assert obs.locator == "line 2" and obs.observed_at == ts(26, 7, 49, second=50)
    (ref,) = obs.identity_refs
    assert (ref.kind, ref.value, ref.field, ref.basis) == (
        IdentifierKind.EMAIL, "charlotte.w@outlook.com", "lookup_key", "trace:rb-1")


def test_syslog_trace_joins_identity_across_infrastructure_lines():
    # evidence_39 regression: the trace ID is positional in the RFC 5424 header, and
    # noise lines (retries, pools) sit between the _started and _completed lines.
    observations, issues, report = run([
        '2026-03-25T08:53:16Z <15>1 node-8 account_service tr-0854521 - - level=DEBUG '
        'event="profile_lookup_started" lookup_key="s.kim14@yahoo.com" attempt="1"',
        '2026-03-25T08:53:21Z <15>1 node-8 account_service tr-0854521 - - level=DEBUG '
        'event="connection_acquired" pool="account_service_replica" wait_ms="53"',
        '2026-03-25T08:53:23Z <12>1 node-8 account_service tr-0854521 - - level=WARN '
        'event="dependency_retry" dependency="account_service_store" attempt="1"',
        '2026-03-25T08:53:25Z <15>1 node-8 account_service tr-0854521 - - level=DEBUG '
        'resource_id="account_4921" event="profile_lookup_completed" rows="1" http_status="200"',
    ])
    assert issues == [] and report.status is FileStatus.PARSED
    (obs,) = observations
    assert obs.resource_id == "account_4921" and obs.condition is Condition.PRESENT
    (ref,) = obs.identity_refs
    assert (ref.kind, ref.value, ref.basis) == (
        IdentifierKind.EMAIL, "s.kim14@yahoo.com", "trace:tr-0854521")
    assert report.details["layouts"] == ["syslog"]


def test_trace_mixing_two_identifiers_is_not_joined():
    observations, issues, report = run([
        "2026-09-26T07:49:50Z level=DEBUG service=x trace_id=t1 event=a_started lookup_key=a@x.com",
        "2026-09-26T07:49:51Z level=DEBUG service=x trace_id=t1 event=b_started lookup_key=b@x.com",
        "2026-09-26T07:49:52Z level=INFO service=x trace_id=t1 event=profile_lookup_completed "
        "resource_id=account_1 rows=1",
    ])
    assert observations[0].identity_refs == ()
    assert "trace mixes 2 identifiers" in issues[0].message
    assert report.status is FileStatus.PARTIAL


def test_noise_events_are_skipped_and_counted():
    observations, _, report = run([
        "2026-09-15T08:51:53Z level=DEBUG service=s trace_id=t event=connection_acquired pool=p",
        "2026-09-15T08:51:55Z level=WARN service=s trace_id=t event=dependency_retry reason=x",
    ])
    assert observations == [] and report.units_skipped == 2


@pytest.mark.parametrize("line,condition,exposure", [
    ("event=connector_config_loaded resource_id=data_flow_1 enabled=false",
     Condition.DISABLED, Exposure.UNKNOWN),
    ("event=connector_heartbeat resource_id=data_flow_1 enabled=true",
     Condition.ACTIVE, Exposure.UNKNOWN),
    ("event=restore_authorization_completed resource_id=backup_1 decision=deny",
     Condition.RESTORE_BLOCKED, Exposure.UNKNOWN),
    ("event=restore_candidate_evaluated resource_id=backup_1 eligible=true deletion_block=false",
     Condition.RESTORE_ALLOWED, Exposure.UNKNOWN),
    ("event=index_query_completed resource_id=search_1 result=miss",
     Condition.ABSENT, Exposure.UNKNOWN),
    ("event=message_pii_scan detector=direct_identifier matched_value=a@x.com resource_id=support_1",
     Condition.PRESENT, Exposure.IDENTIFIABLE),
    ("event=analytics_profile_read resource_id=analytics_1 direct_identifiers=0 email=null",
     Condition.PRESENT, Exposure.UNLINKED),
    ('event=message_read resource_id=support_2 message_text="[REDACTED]" subject_ref=user_4',
     Condition.PRESENT, Exposure.REFERENCE_ONLY),
    ("event=thumbnail_failed resource_id=support_3 reason=unsupported_media",
     Condition.MENTIONED, Exposure.UNKNOWN),
])
def test_event_states(line, condition, exposure):
    observations, _, _ = run([f"2026-09-15T08:00:00Z level=INFO service=s {line}"])
    assert (observations[0].condition, observations[0].exposure) == (condition, exposure)


def test_record_lookup_lines_use_their_stated_system_and_type():
    observations, _, _ = run([
        '2026-03-15T07:00:00Z INFO service=downstream event=record_lookup seq=1 '
        'system="account_service" resource_id="account_022" record_type="USER_PROFILE" '
        'result="NOT_FOUND" subject_reference="zofia@x.com"'])
    obs = observations[0]
    assert (obs.system_id, obs.data_category, obs.condition) == (
        "account_service", "USER_PROFILE", Condition.ABSENT)
    assert obs.identity_refs[0].value == "zofia@x.com"


def test_support_categories_come_from_events_not_the_system():
    observations, _, _ = run([
        "2026-09-15T08:00:00Z level=INFO service=support event=message_pii_scan "
        "resource_id=support_1 detector=direct_identifier matched_value=a@x.com",
        "2026-09-15T08:00:01Z level=ERROR service=attachment_preview event=thumbnail_failed "
        "resource_id=support_2",
        "2026-09-15T08:00:02Z level=INFO service=support_replica event=contact_query_completed "
        "resource_id=support_3 rows=1",
    ])
    categories = {o.resource_id: o.data_category for o in observations}
    assert categories == {"support_1": "SUPPORT_MESSAGE", "support_2": "SUPPORT_ATTACHMENT",
                          "support_3": None}  # nothing says what a contact-index row is


def test_unparseable_and_badly_timed_lines_are_reported_not_fatal():
    observations, issues, report = run([
        "garbage line with no structure",
        "2026-13-45T99:00:00Z level=INFO service=s event=profile_lookup_completed resource_id=account_1",
        "2026-09-15T08:00:00Z level=INFO service=s event=profile_lookup_completed "
        "resource_id=account_2 rows=1",
    ])
    assert [o.resource_id for o in observations] == ["account_2"]
    assert [i.locator for i in issues] == ["line 1", "line 2"]
    assert report.status is FileStatus.PARTIAL


def test_provenance_is_kept():
    observations, _, _ = run(["", LAYOUTS["key_value"]], captured=ts(16))
    obs = observations[0]
    assert (obs.evidence_id, obs.locator, obs.source_kind) == ("evidence_99", "line 2", "log")
    assert obs.file_captured_at == ts(16) and obs.attributes["trace_id"] == "tr-1"
    assert by_resource(observations, "search_7")
