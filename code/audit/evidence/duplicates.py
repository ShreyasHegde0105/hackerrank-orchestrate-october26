"""Duplicate accounting for observations. Nothing is ever deleted.

Definitions (each observation belongs to at most one group of each kind):

* Within-file duplicate: two or more observations from the SAME evidence
  file whose content is identical in every field except their locator:
  system, resource, copy, collection, time, condition, exposure, category,
  identity refs, labels, related resources and all attributes (for log lines
  that includes the event name and trace ID). That can only come from a source
  repeating a row verbatim, so it is reported as a warning. Repeated lookups
  of the same record in different traces are NOT duplicates: their trace IDs
  differ.

* Duplicate fact: two or more observations from DIFFERENT evidence files that
  state the same fact at the same moment: same resource, system, copy
  (replica), condition and exposure, and the same observed_at.

* Repeated state: two or more observations from DIFFERENT evidence files with
  the same resource, system, copy, condition and exposure, at any times.
  This is corroboration over time, reported for information only.

Separate files are independent evidence with their own provenance and
citation date, so duplicate facts and repeated states are counted, never merged.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from audit.evidence.model import Observation


def content_signature(obs: Observation) -> tuple:
    """Everything an observation says, except where it was found (locator)."""
    return (
        obs.evidence_id, obs.system_id, obs.resource_id, obs.replica, obs.collection,
        obs.observed_at, obs.condition, obs.exposure, obs.data_category,
        tuple(sorted((r.kind.value, r.value, r.field, r.basis) for r in obs.identity_refs)),
        obs.labels, obs.related_resources,
        json.dumps(obs.attributes, sort_keys=True, default=str),
    )


def fact_key(obs: Observation) -> tuple | None:
    if obs.resource_id is None:
        return None
    return (obs.resource_id, obs.system_id, obs.replica, obs.condition, obs.exposure)


@dataclass
class DuplicateReport:
    within_file: list[list[str]] = field(default_factory=list)      # groups of obs_ids
    duplicate_facts: list[list[str]] = field(default_factory=list)
    repeated_states: list[list[str]] = field(default_factory=list)

    @staticmethod
    def _members(groups) -> int:
        return sum(len(g) for g in groups)

    def summary(self) -> dict:
        return {
            "within_file_groups": len(self.within_file),
            "within_file_observations": self._members(self.within_file),
            "within_file_extra_copies": self._members(self.within_file) - len(self.within_file),
            "duplicate_fact_groups": len(self.duplicate_facts),
            "duplicate_fact_observations": self._members(self.duplicate_facts),
            "repeated_state_groups": len(self.repeated_states),
            "repeated_state_observations": self._members(self.repeated_states),
        }


def analyse_duplicates(observations: list[Observation]) -> DuplicateReport:
    report = DuplicateReport()
    by_signature: dict[tuple, list[Observation]] = defaultdict(list)
    by_fact_instant: dict[tuple, list[Observation]] = defaultdict(list)
    by_fact: dict[tuple, list[Observation]] = defaultdict(list)
    for obs in observations:
        by_signature[content_signature(obs)].append(obs)
        key = fact_key(obs)
        if key is not None:
            by_fact_instant[key + (obs.observed_at,)].append(obs)
            by_fact[key].append(obs)
    report.within_file = sorted([sorted(o.obs_id for o in group)
                                 for group in by_signature.values() if len(group) > 1])
    for groups, target in ((by_fact_instant, report.duplicate_facts),
                           (by_fact, report.repeated_states)):
        for group in groups.values():
            if len({o.evidence_id for o in group}) > 1:
                target.append(sorted(o.obs_id for o in group))
        target.sort()
    return report
