"""Deterministic lookups over extracted evidence.

Three ways in, all returning the original Observation objects (so every
answer carries its full provenance), sorted by (observed_at, evidence number,
locator):

  by_resource(resource_id)   every observation of a resource
  by_identifier(value)       observations carrying that identifier, in any format
  for_person(user_id)        observations attributed to one resolved person

Attribution uses the Phase 2 identity index only: an observation belongs to
a person when its identity refs resolve to that person and to nobody else.
Observations whose refs resolve to two or more people are "conflicted" and
returned separately, never mixed into either person's evidence. Refs that
resolve to nobody (decoys, unknown people) are "unattributed". Observations
with no refs at all are "no_identity". No time filtering happens here:
deciding what was true at audit_at is Phase 4's job.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from audit.evidence.model import ExtractionResult, Observation
from audit.identity import NORMALIZERS, IdentityIndex, classify_identifier
from audit.models import IdentifierKind


def _order(obs: Observation) -> tuple:
    number = obs.evidence_id.rsplit("_", 1)[-1]
    return (obs.observed_at, int(number) if number.isdigit() else 0, obs.evidence_id,
            obs.locator)


def _sorted(observations) -> tuple[Observation, ...]:
    return tuple(sorted(observations, key=_order))


@dataclass(frozen=True)
class PersonEvidence:
    user_id: str
    observations: tuple[Observation, ...]   # attributed to this person only
    conflicted: tuple[Observation, ...]     # refs also resolve to someone else

    @property
    def resources(self) -> tuple[str, ...]:
        return tuple(sorted({o.resource_id for o in self.observations if o.resource_id}))


class EvidenceIndex:
    def __init__(self, result: ExtractionResult, identities: IdentityIndex):
        self.identities = identities
        self._by_resource: dict[str, list[Observation]] = defaultdict(list)
        self._by_identifier: dict[tuple[IdentifierKind, str], list[Observation]] = defaultdict(list)
        self._by_label: dict[str, list[Observation]] = defaultdict(list)
        self._by_person: dict[str, list[Observation]] = defaultdict(list)
        self._conflicted: dict[str, list[Observation]] = defaultdict(list)
        self._unattributed: list[Observation] = []
        self._no_identity: list[Observation] = []
        for obs in result.observations:
            if obs.resource_id:
                self._by_resource[obs.resource_id].append(obs)
            for label in obs.labels:
                self._by_label[label].append(obs)
            for key in {(r.kind, r.value) for r in obs.identity_refs}:
                self._by_identifier[key].append(obs)
            owners = self.owners(obs)
            if len(owners) == 1:
                self._by_person[next(iter(owners))].append(obs)
            elif len(owners) > 1:
                for user_id in owners:
                    self._conflicted[user_id].append(obs)
            elif obs.identity_refs:
                self._unattributed.append(obs)
            else:
                self._no_identity.append(obs)

    def owners(self, obs: Observation) -> set[str]:
        """Every known person any of the observation's refs belongs to (any time)."""
        return {claim.user_id for ref in obs.identity_refs
                for claim in self.identities.claims(ref.kind, ref.value)}

    def by_resource(self, resource_id: str) -> tuple[Observation, ...]:
        return _sorted(self._by_resource.get(resource_id, ()))

    def by_label(self, label: str) -> tuple[Observation, ...]:
        return _sorted(self._by_label.get(label, ()))

    def by_identifier(self, raw: str, kind: IdentifierKind | None = None) -> tuple[Observation, ...]:
        if kind is None:
            found = classify_identifier(raw)
            if not found:
                return ()  # names and free text are not identifiers
            kind, value = found
        else:
            value = NORMALIZERS[kind](raw)
            if value is None:
                return ()
        return _sorted(self._by_identifier.get((kind, value), ()))

    def for_person(self, user_id: str) -> PersonEvidence:
        return PersonEvidence(user_id, _sorted(self._by_person.get(user_id, ())),
                              _sorted(self._conflicted.get(user_id, ())))

    def unattributed(self) -> tuple[Observation, ...]:
        """Observations whose identifiers match no known person."""
        return _sorted(self._unattributed)

    def no_identity(self) -> tuple[Observation, ...]:
        """Observations carrying no identifier at all (e.g. anonymized records)."""
        return _sorted(self._no_identity)

    def link_conflicts(self) -> list[tuple]:
        """Catalog links (Phase 2 ResourceLinks) contradicted by evidence: the
        linked resource is observed carrying identifiers that do not resolve
        to the linked person (another person, or nobody known). Returned as
        (link, observation, owners_seen); nothing is resolved here."""
        conflicts = []
        for link in self.identities.links:
            for obs in self.by_resource(link.resource_id):
                if not obs.identity_refs:
                    continue
                owners = self.owners(obs)
                if owners != {link.user_id}:
                    conflicts.append((link, obs, tuple(sorted(owners))))
        return conflicts

    def counts(self) -> dict:
        return {
            "resources": len(self._by_resource),
            "identifiers": len(self._by_identifier),
            "people_with_evidence": len(self._by_person),
            "attributed_observations": sum(len(v) for v in self._by_person.values()),
            "conflicted_observations": len({id(o) for v in self._conflicted.values() for o in v}),
            "unattributed_observations": len(self._unattributed),
            "no_identity_observations": len(self._no_identity),
        }
