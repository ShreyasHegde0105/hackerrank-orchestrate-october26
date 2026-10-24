"""Log extraction for the six layouts found in the dataset.

    1. key=value                 2026-..Z level=INFO service=x trace_id=t event=e k=v
    2. key=value inside message  2026-..Z requestId=t level=.. function=x message="k=\"v\" .."
    3. pipe header               2026-..Z | LEVEL | service | trace | event | k="v" ..
    4. bracket header            2026-..Z [LEVEL] [service] [trace] event: k="v" ..
    5. JSON object               2026-..Z {"severity": .., "service": .., "event": .., ..}
    6. syslog (RFC 5424) header  2026-..Z <15>1 host service trace - - level=DEBUG k="v" ..

Every layout is normalised to one dict with `service`, `trace_id` and `event`.
A lookup usually logs the identifier it searched for on a *_started line and
the record it found on the *_completed line of the same trace, so identifiers
are joined across lines by trace_id. That join is refused, with an issue, if
a trace mixes identifiers or resources.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass

from audit.evidence.classify import IDENTITY_FIELDS, identity_refs, is_null
from audit.evidence.model import (
    Condition,
    ExtractionIssue,
    FileReport,
    FileStatus,
    IdentityRef,
    Observation,
    TimeBasis,
)
from audit.evidence.records import observation_from_record
from audit.loader import DatasetError, parse_timestamp
from audit.models import EvidenceFile

_PAIR = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
_PIPE = re.compile(r"^(?P<ts>\S+) \| (?P<level>\w+) \| (?P<service>\S+) \| (?P<trace>\S+) \| "
                   r"(?P<event>\w+) \| (?P<rest>.*)$")
_BRACKET = re.compile(r"^(?P<ts>\S+) \[(?P<level>\w+)\] \[(?P<service>[^\]]+)\] "
                      r"\[(?P<trace>[^\]]+)\] (?P<event>\w+): (?P<rest>.*)$")
# RFC 5424 syslog: PRI+version, host, app-name (the service), procid (the trace),
# msgid and structured-data (both "-" in the dataset), then key=value pairs.
_SYSLOG = re.compile(r"^(?P<ts>\S+) <\d+>\d+ (?P<host>\S+) (?P<service>\S+) "
                     r"(?P<trace>\S+) \S+ \S+ (?P<rest>.*)$")
_MESSAGE = re.compile(r'message="(?P<inner>(?:[^"\\]|\\.)*)"')

# Events that describe infrastructure, not customer records.
NOISE_EVENTS = {
    "connection_acquired", "connection_pool_pressure", "pool_sample", "dependency_retry",
    "dependency_recovered", "cache_lookup", "cache_refresh", "certificate_checked",
    "checkpoint_saved", "consumer_lag", "delivery_abandoned", "disk_latency",
    "export_started", "export_completed", "flag_snapshot_loaded", "lease_extended",
    "lease_renewed", "properties_reloaded", "slow_call_rate", "telemetry_flushed",
    "timer_tick", "batch_started",
}


@dataclass
class LogLine:
    number: int
    timestamp: str
    layout: str
    fields: dict

    @property
    def trace(self) -> str | None:
        trace = self.fields.get("trace_id") or self.fields.get("requestId")
        return None if is_null(trace) else trace

    @property
    def event(self) -> str:
        return self.fields.get("event", "")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('\\"', '"')
    return value


def _pairs(text: str) -> dict:
    return {key: _unquote(value) for key, value in _PAIR.findall(text)}


def parse_log_line(line: str) -> tuple[str, dict]:
    """Return (layout, fields). Raises ValueError for an unrecognised line."""
    header = _PIPE.match(line) or _BRACKET.match(line)
    if header:
        fields = _pairs(header.group("rest"))
        fields.update(level=header.group("level"), service=header.group("service"),
                      trace_id=header.group("trace"), event=header.group("event"))
        return ("pipe" if header.re is _PIPE else "bracket"), fields | {"_ts": header.group("ts")}
    syslog = _SYSLOG.match(line)
    if syslog:
        fields = _pairs(syslog.group("rest"))
        fields.update(service=syslog.group("service"))
        if syslog.group("trace") != "-":
            fields["trace_id"] = syslog.group("trace")
        if "event" not in fields:
            raise ValueError("no event field")
        return "syslog", fields | {"_ts": syslog.group("ts")}
    timestamp, _, body = line.partition(" ")
    if body.startswith("{"):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"bad JSON payload ({exc.msg})") from None
        fields = {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in payload.items()}
        return "json", fields | {"_ts": timestamp}
    message = _MESSAGE.search(body)
    if message:
        fields = _pairs(body[:message.start()] + body[message.end():])
        fields.update(_pairs(message.group("inner").replace('\\"', '"')))
        fields.setdefault("service", fields.get("function"))
        return "nested_message", fields | {"_ts": timestamp}
    fields = _pairs(body)
    if "event" not in fields:
        raise ValueError("no event field")
    return "key_value", fields | {"_ts": timestamp}


def extract_log(evidence: EvidenceFile, lines: list[str]) -> tuple[
        list[Observation], list[ExtractionIssue], FileReport]:
    issues: list[ExtractionIssue] = []
    parsed: list[LogLine] = []
    for number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            layout, fields = parse_log_line(raw)
            timestamp = fields.pop("_ts")
            parse_timestamp(timestamp, "log line")
        except (ValueError, DatasetError) as exc:
            issues.append(ExtractionIssue(evidence.file_id, f"line {number}", "warning",
                                          f"unparsed log line: {exc}"))
            continue
        parsed.append(LogLine(number, timestamp, layout, fields))

    trace_refs = _trace_identity(evidence, parsed, issues)
    observations, skipped = [], 0
    for line in parsed:
        if line.event in NOISE_EVENTS or line.event.endswith("_started"):
            skipped += 1  # queries carry identity only; it reaches results via the trace
            continue
        if is_null(line.fields.get("resource_id")):
            skipped += 1
            continue
        observation = _line_observation(evidence, line, trace_refs.get(line.trace, []))
        if observation is not None:
            observations.append(observation)
    report = FileReport(evidence.file_id, evidence.kind, "logs",
                        FileStatus.PARTIAL if issues else FileStatus.PARSED,
                        observations=len(observations), units_seen=len(parsed),
                        units_skipped=skipped,
                        details={"layouts": sorted({line.layout for line in parsed})})
    return observations, issues, report


def _trace_identity(evidence: EvidenceFile, lines: list[LogLine],
                    issues: list[ExtractionIssue]) -> dict[str, list[IdentityRef]]:
    """Identifiers each trace searched for, if the trace is about one identifier
    and one resource. Mixed traces are reported and not joined."""
    by_trace: dict[str, list[LogLine]] = defaultdict(list)
    for line in lines:
        if line.trace:
            by_trace[line.trace].append(line)
    joined = {}
    for trace, members in by_trace.items():
        refs = [ref for member in members
                for ref in identity_refs({k: v for k, v in member.fields.items()
                                          if k in IDENTITY_FIELDS}, f"trace:{trace}")]
        values = {(ref.kind, ref.value) for ref in refs}
        resources = {m.fields["resource_id"] for m in members
                     if not is_null(m.fields.get("resource_id"))
                     and m.fields["resource_id"] != m.fields.get("lookup_key")}
        if len(values) > 1 or len(resources) > 1:
            issues.append(ExtractionIssue(
                evidence.file_id, f"trace {trace}", "warning",
                f"trace mixes {len(values)} identifiers and {len(resources)} resources; "
                "identity not joined across its lines"))
            continue
        if refs:
            joined[trace] = refs
    return joined


def _line_observation(evidence: EvidenceFile, line: LogLine,
                      trace_refs: list[IdentityRef]) -> Observation | None:
    fields = {k: v for k, v in line.fields.items()
              if k not in ("level", "severity", "logger", "instance", "pod", "region",
                           "requestId", "function", "service")}
    fields["log_service"] = line.fields.get("service")
    if line.event in ("analytics_identity_scan", "message_pii_scan", "attachment_pii_scan") \
            and fields.get("detector") == "direct_identifier":
        fields.setdefault("direct_identifiers", "1")
    if fields.get("direct_identifiers") == "0":
        # analytics_profile_read: identifiers are explicitly reported as empty
        for name in ("email", "phone", "email_hash", "customer_reference"):
            fields.setdefault(name, "null")
    # a line naming its own subject (lookup_key on a completed line) is same-record
    observation = observation_from_record(
        evidence, f"line {line.number}", fields,
        observed_at=parse_timestamp(line.timestamp), time_basis=TimeBasis.LOG_LINE,
        system_id=None, extractor=f"logs/{line.event}",
        default_condition=_default_condition(line.event, fields),
        extra_refs=trace_refs, event=line.event)
    if observation is not None:
        observation.attributes["event"] = line.event
        if line.trace:
            observation.attributes["trace_id"] = line.trace
    return observation


def _default_condition(event: str, fields: dict) -> Condition:
    """Condition when no explicit state field decides it."""
    if event.endswith(("_completed", "_loaded", "_read", "_scan", "_evaluated",
                       "_heartbeat", "_check")) or event == "record_lookup":
        return Condition.PRESENT
    return Condition.MENTIONED
