"""Phase 4: reconstruct each case's records as they stood at its audit_at.

Consumes Phase 2 (IdentityIndex) and Phase 3 (ExtractionResult, EvidenceIndex)
without changing them. For one request it answers: which records belong to
this person, what state was each in at audit_at, and which evidence may be
cited for that state. State and citation are decided separately.

Rules (decided in review; see code/docs/phase4_assumptions.md):

Time
  T1  A fact is usable for the state when its observed_at <= audit_at
      (inclusive), even if it sits in a file captured later (assumption P4-1).
  T2  A file may be cited only if its manifest captured_at <= audit_at.
  T3  Time-anomalous evidence is kept for diagnostics but never decides a
      state or is cited: an item dated after its own file's capture, and every
      observation of a screenshot that shows a past event dated after its
      capture (P4-4).
  T4  Nothing dated after audit_at affects the case; such observations are
      only counted.

Records belonging to the case
  R1  Observations that the Phase 3 index attributes to the person alone, and
      usable under T1/T3, give the person's records.
  R2  Resources those observations point to (related_resources) are added.
  R3  Trusted Phase 2 catalog links known by audit_at add resources; their
      labels join label-only observations (a file name, a statement number).
  R4  Account deletions in the activity log near reported_at are listed as
      unverified candidates only; they get no state and no citation (P4-3).
      Each record keeps its strongest link: record_content (the person's
      identifiers on the record), catalog_link, related, or lookup_only (only
      lookups keyed by the person that found nothing; the weakest link).
  R5  If an observation of a case record carries identifiers that do not
      belong to the person (someone else, or nobody known), the relationship
      is CONFLICTED and the record's state is CONFLICT (P4-5).

State, per copy (primary, read_replica, failover_replica, ...)
  S1  The latest usable sighting in that copy decides it (PRESENT, ABSENT,
      ACTIVE, DISABLED, RESTORE_ALLOWED, RESTORE_BLOCKED). Sightings at the same
      instant that disagree make the copy CONFLICT.
  S2  A copy last seen present is ABSENT only if a later qualifying snapshot,
      taken on or before audit_at, lacks the record: a SQL snapshot of the same
      system, the same copy and the same kind of data. JSON bundles and
      collections never prove absence (P4-2). Missing evidence never does.
  S3  Combining copies: any CONFLICT copy -> CONFLICT; any copy holding the
      record -> PRESENT (exposure = the most exposed such copy); all evaluated
      copies gone -> ABSENT; no usable sighting -> UNKNOWN.

Citation
  C1  Chosen only from citable evidence (T2, T3) that supports the decided
      state: for a present-like state, the latest citable observation that is
      not an absence; for ABSENT, the latest citable explicit absence, else the
      citable snapshot that proved it. UNKNOWN and CONFLICT have no citation;
      CONFLICT lists the competing observations.
  C2  When the state was decided only from facts in later-captured files
      (T1 without T2), the citation is empty and says so.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from audit.evidence.classify import COLLECTION_CATEGORY, system_for_resource
from audit.evidence.index import EvidenceIndex
from audit.evidence.model import Condition, Coverage, Exposure, ExtractionResult, Observation
from audit.evidence.screenshots import PAST_EVENT_WORDS, screen_time
from audit.identity import IdentityIndex, ResolutionStatus
from audit.loader import Dataset
from audit.models import DeletionRequest

PRESENT, ABSENT, UNKNOWN, CONFLICT = "PRESENT", "ABSENT", "UNKNOWN", "CONFLICT"
CONFIRMED, CONFLICTED = "CONFIRMED", "CONFLICTED"
PROCESS_CONDITIONS = {Condition.ACTIVE, Condition.DISABLED, Condition.RESTORE_ALLOWED,
                      Condition.RESTORE_BLOCKED}
EXPOSURE_RANK = {Exposure.IDENTIFIABLE: 0, Exposure.REFERENCE_ONLY: 1, Exposure.UNLINKED: 2,
                 Exposure.UNKNOWN: 3}
CANDIDATE_WINDOW = (timedelta(hours=2), timedelta(minutes=10))  # before / after reported_at
# How a record was tied to the person, strongest first.
LINK_STRENGTHS = ("record_content", "catalog_link", "related", "lookup_only")


@dataclass(frozen=True)
class CopyState:
    copy: str                 # primary, read_replica, failover_replica, replica_eu_west, ...
    state: str                # PRESENT, ABSENT, CONFLICT
    exposure: Exposure
    process_state: str | None  # ACTIVE/DISABLED/RESTORE_ALLOWED/RESTORE_BLOCKED, if stated
    decided_by: tuple[str, ...]  # obs_ids, or "coverage:<evidence_id>#<locator>"
    as_of: datetime


@dataclass(frozen=True)
class ResourceState:
    resource_id: str
    system_id: str | None
    data_category: str | None
    state: str                  # PRESENT, ABSENT, UNKNOWN, CONFLICT
    existence: str              # the evidence-only verdict, before any relationship conflict
    exposure: Exposure          # of the most exposed present copy
    process_state: str | None
    relationship: str           # CONFIRMED or CONFLICTED
    relationship_basis: tuple[str, ...]
    link_strength: str          # record_content, lookup_only, related, catalog_link (strongest wins)
    copies: tuple[CopyState, ...]
    supporting: tuple[str, ...]   # obs_ids that decided the state
    citation: str | None          # evidence_id a later step may cite, or None
    citation_obs: str | None      # the observation or coverage that citation stands on
    competing: tuple[str, ...]    # for CONFLICT: the disagreeing observations
    reason: str


@dataclass(frozen=True)
class ActivityCandidate:
    """An account deletion near reported_at: NOT a verified link (rule R4)."""
    activity_id: str
    resource_id: str
    event_time: datetime
    reported_result: str
    unique_in_window: bool


@dataclass
class CaseReconstruction:
    case_id: str
    audit_at: datetime
    resolution: str                    # ResolutionStatus value
    user_id: str | None
    resources: tuple[ResourceState, ...] = ()
    activity_candidates: tuple[ActivityCandidate, ...] = ()
    excluded_future: tuple[str, ...] = ()     # subject observations dated after audit_at
    excluded_anomalous: tuple[str, ...] = ()  # subject observations with untrustworthy times
    unattributed_mentions: tuple[str, ...] = ()  # case records also carrying unknown identifiers
    diagnostics: list[str] = field(default_factory=list)

    def resource(self, resource_id: str) -> ResourceState | None:
        return next((r for r in self.resources if r.resource_id == resource_id), None)


def _num(evidence_id: str) -> int:
    tail = evidence_id.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def anomalous_files(result: ExtractionResult) -> set[str]:
    """Screenshots showing a past event dated after their own capture (A14)."""
    flagged = set()
    for obs in result.observations:
        if obs.source_kind != "png":
            continue
        shown = [screen_time(obs.attr("captured_at_text"))]
        shown += [screen_time(str(v)) for k, v in obs.attributes.items()
                  if set(str(k).split("_")) & PAST_EVENT_WORDS]
        if any(t and t > obs.file_captured_at for t in shown):
            flagged.add(obs.evidence_id)
    return flagged


class Reconstructor:
    def __init__(self, dataset: Dataset, identities: IdentityIndex, result: ExtractionResult):
        self.dataset = dataset
        self.identities = identities
        self.result = result
        self.index = EvidenceIndex(result, identities)
        self.anomalous_files = anomalous_files(result)
        self.sql_coverage: dict[str, list[Coverage]] = defaultdict(list)
        for coverage in result.coverages:
            if dataset.evidence_files[coverage.evidence_id].kind == "sql":
                self.sql_coverage[coverage.system_id].append(coverage)
        self.deletions = [a for a in dataset.activities
                          if a.event_type == "DELETE_RECORD" and a.source_system == "deletion_worker"
                          and a.target_system == "account_service"]

    # --- eligibility ---------------------------------------------------------------------

    def is_anomalous(self, obs: Observation) -> bool:
        return obs.observed_at > obs.file_captured_at or obs.evidence_id in self.anomalous_files

    def usable(self, obs: Observation, audit_at: datetime) -> bool:
        """T1 + T3: may decide state at audit_at."""
        return obs.observed_at <= audit_at and not self.is_anomalous(obs)

    def citable(self, obs: Observation, audit_at: datetime) -> bool:
        """T2 + T3: may be cited for a case audited at audit_at."""
        return self.usable(obs, audit_at) and obs.file_captured_at <= audit_at

    # --- the case ----------------------------------------------------------------------------

    def reconstruct(self, request: DeletionRequest) -> CaseReconstruction:
        audit_at = request.audit_at
        resolution = self.identities.resolve_request(request)
        case = CaseReconstruction(request.case_id, audit_at, resolution.status.value,
                                  resolution.user_id)
        if resolution.status is not ResolutionStatus.RESOLVED:
            case.diagnostics.append(f"identity {resolution.status.value}: "
                                    f"{'; '.join(resolution.notes)}; no records reconstructed")
            return case
        user_id = resolution.user_id
        person = self.index.for_person(user_id)

        bases: dict[str, set[str]] = defaultdict(set)
        strength: dict[str, set[str]] = defaultdict(set)
        label_resource: dict[str, str] = {}
        future, anomalous = [], []
        for obs in person.observations:
            if obs.observed_at > audit_at:
                future.append(obs.obs_id)
                continue
            if self.is_anomalous(obs):
                anomalous.append(obs.obs_id)
                continue
            if obs.resource_id:
                bases[obs.resource_id].add(f"evidence:{obs.obs_id}")
                # a lookup keyed by the person that found nothing is a weaker link
                strength[obs.resource_id].add(
                    "lookup_only" if obs.condition is Condition.ABSENT else "record_content")
            for related in obs.related_resources:
                bases[related].add(f"related:{obs.obs_id}")
                strength[related].add("related")
        for link in self.identities.links_for(user_id, audit_at):
            bases[link.resource_id].add(f"catalog_link:{link.activity_id}")
            strength[link.resource_id].add("catalog_link")
            label_resource[link.label] = link.resource_id
        for conflicted in person.conflicted:
            case.diagnostics.append(f"{conflicted.obs_id} carries identifiers of several people; "
                                    "not attributed")

        states, unattributed = [], set()
        for resource_id in sorted(bases):
            link = next(s for s in LINK_STRENGTHS if s in strength[resource_id])
            state = self._resource_state(resource_id, user_id, audit_at, tuple(sorted(bases[resource_id])),
                                         [lbl for lbl, rid in label_resource.items()
                                          if rid == resource_id], link)
            if state.relationship == CONFLICTED:
                unattributed.update(state.competing)
            states.append(state)
        case.resources = tuple(states)
        case.excluded_future = tuple(future)
        case.excluded_anomalous = tuple(anomalous)
        case.unattributed_mentions = tuple(sorted(unattributed))
        case.activity_candidates = self._candidates(request)
        return case

    def reconstruct_all(self) -> list[CaseReconstruction]:
        return [self.reconstruct(r) for r in self.dataset.requests]

    # --- one record ------------------------------------------------------------------------------

    def _resource_state(self, resource_id, user_id, audit_at, basis, labels,
                        link_strength) -> ResourceState:
        observations = list(self.index.by_resource(resource_id))
        for label in labels:
            observations += [o for o in self.index.by_label(label) if o.resource_id is None]
        usable = [o for o in observations if self.usable(o, audit_at)]
        decisive = [o for o in usable if o.condition is not Condition.MENTIONED]
        categories = sorted({o.data_category for o in usable if o.data_category})
        category = categories[0] if len(categories) == 1 else (
            _most_common([o.data_category for o in usable if o.data_category]) if categories else None)
        system = system_for_resource(resource_id) or next(
            (o.system_id for o in usable if o.system_id), None)

        # R5: someone else's (or an unknown person's) identifiers on this record
        foreign = [o for o in usable if o.identity_refs and self.index.owners(o) != {user_id}]
        relationship = CONFLICTED if foreign else CONFIRMED

        by_copy: dict[str, list[Observation]] = defaultdict(list)
        for obs in decisive:
            by_copy[obs.replica or "primary"].append(obs)
        copies = tuple(self._copy_state(resource_id, system, category, copy, items, audit_at)
                       for copy, items in sorted(by_copy.items()))

        if any(c.state == CONFLICT for c in copies):
            existence = CONFLICT
        elif any(c.state == PRESENT for c in copies):
            existence = PRESENT
        elif copies:
            existence = ABSENT
        else:
            existence = UNKNOWN
        present = [c for c in copies if c.state == PRESENT]
        exposure = min((c.exposure for c in present), key=EXPOSURE_RANK.get, default=Exposure.UNKNOWN)
        process = next((c.process_state for c in sorted(present, key=lambda c: c.as_of, reverse=True)
                        if c.process_state), None)
        state = CONFLICT if relationship == CONFLICTED else existence
        supporting = tuple(sorted({o for c in copies for o in c.decided_by}))

        competing = ()
        if existence == CONFLICT:
            competing = tuple(sorted(o for c in copies if c.state == CONFLICT for o in c.decided_by))
        if relationship == CONFLICTED:
            competing = tuple(sorted(set(competing) | {o.obs_id for o in foreign}))

        citation, citation_obs, cite_note = self._citation(state, copies, observations, audit_at)
        reason = self._reason(state, existence, relationship, copies, foreign, basis, cite_note)
        return ResourceState(resource_id, system, category, state, existence, exposure, process,
                             relationship, basis, link_strength, copies, supporting, citation,
                             citation_obs, competing, reason)

    def _copy_state(self, resource_id, system, category, copy, items, audit_at) -> CopyState:
        latest_time = max(o.observed_at for o in items)
        latest = [o for o in items if o.observed_at == latest_time]
        conditions = {o.condition for o in latest}
        if len(conditions) > 1:
            return CopyState(copy, CONFLICT, Exposure.UNKNOWN, None,
                             tuple(sorted(o.obs_id for o in latest)), latest_time)
        condition = conditions.pop()
        if condition is Condition.ABSENT:
            return CopyState(copy, ABSENT, Exposure.UNKNOWN, None,
                             tuple(sorted(o.obs_id for o in latest)), latest_time)
        # S2: a later qualifying SQL snapshot of this copy that lacks the record
        for coverage in sorted(self.sql_coverage.get(system, ()), key=lambda c: c.observed_at):
            if (latest_time < coverage.observed_at <= audit_at
                    and coverage.file_captured_at <= audit_at
                    and (coverage.replica or "primary") == copy
                    and COLLECTION_CATEGORY.get(coverage.collection) in (None, category)
                    and resource_id not in coverage.resource_ids):
                return CopyState(copy, ABSENT, Exposure.UNKNOWN, None,
                                 (f"coverage:{coverage.evidence_id}#{coverage.locator}",),
                                 coverage.observed_at)
        exposure = min((o.exposure for o in latest), key=EXPOSURE_RANK.get)
        process = condition.value if condition in PROCESS_CONDITIONS else None
        return CopyState(copy, PRESENT, exposure, process, tuple(sorted(o.obs_id for o in latest)),
                         latest_time)

    def _citation(self, state, copies, observations, audit_at):
        citable = [o for o in observations if self.citable(o, audit_at)]
        key = lambda o: (o.file_captured_at, o.observed_at, _num(o.evidence_id), o.locator)  # noqa: E731
        if state == PRESENT:
            candidates = [o for o in citable if o.condition not in (Condition.ABSENT, Condition.MENTIONED)]
            if candidates:
                best = max(candidates, key=key)
                return best.evidence_id, best.obs_id, ""
            return None, None, "no citable file captured by audit_at shows the record"
        if state == ABSENT:
            negatives = [o for o in citable if o.condition is Condition.ABSENT]
            if negatives:
                best = max(negatives, key=key)
                return best.evidence_id, best.obs_id, ""
            proofs = [ref for c in copies for ref in c.decided_by if ref.startswith("coverage:")]
            for ref in sorted(proofs, reverse=True):
                evidence_id = ref[len("coverage:"):].split("#")[0]
                if self.dataset.evidence_files[evidence_id].captured_at <= audit_at:
                    return evidence_id, ref, ""
            return None, None, "absence shown only by evidence captured after audit_at"
        return None, None, ""

    def _reason(self, state, existence, relationship, copies, foreign, basis, cite_note) -> str:
        parts = []
        if relationship == CONFLICTED:
            parts.append("relationship conflicted: " + "; ".join(
                f"{o.obs_id} shows {[r.raw for r in o.identity_refs]}" for o in foreign))
        if not copies:
            parts.append("no usable sighting at or before audit_at; absence is not assumed")
        for c in copies:
            parts.append(f"{c.copy}: {c.state}{'/' + c.process_state if c.process_state else ''} "
                         f"as of {c.as_of:%Y-%m-%dT%H:%M:%SZ} by {', '.join(c.decided_by)}")
        if state == CONFLICT and existence == CONFLICT:
            parts.append("sightings at the same instant disagree")
        if cite_note:
            parts.append(f"citation: none ({cite_note})")
        parts.append("linked by " + ", ".join(basis))
        return " | ".join(parts)

    def _candidates(self, request) -> tuple[ActivityCandidate, ...]:
        before, after = CANDIDATE_WINDOW
        low, high = request.reported_at - before, request.reported_at + after
        found = [a for a in self.deletions if low <= a.event_time <= high
                 and a.event_time <= request.audit_at]
        return tuple(ActivityCandidate(a.activity_id, a.resource_id, a.event_time,
                                       a.reported_result, len(found) == 1)
                     for a in sorted(found, key=lambda a: (a.event_time, a.activity_id)))


def _most_common(values):
    counts = defaultdict(int)
    for value in values:
        counts[value] += 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
