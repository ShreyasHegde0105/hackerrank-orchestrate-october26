"""Identity resolution: request email -> internal user -> every identifier they held.

Design
------
* Every identifier a user has held becomes an IdentifierClaim with a validity
  window. Current values come from user_identities.csv; replaced values come
  from user_audit_log.csv, where each change closes the old value's window and
  opens the new one's. The stable user_id is a claim too.
* Values are normalised before comparison: emails trimmed and case-folded,
  phones reduced to digits, hashes lower-cased, user IDs lower-cased.
* Names are never indexed or matched. Several users share a name, so a name
  is display text only.
* Nothing is guessed. If a value maps to more than one user and the request
  time cannot separate them, the result is AMBIGUOUS with every candidate kept.
  Inconsistencies in the source data are recorded as IdentityIssues, not
  silently repaired.

Matching priority for a request email
-------------------------------------
1. exact match on a current email
2. exact match on a historical email
3. SHA-256 of the email matches a current or historical email_hash
   (email_hash = sha256(lower-cased email) holds for all 957 known values)
Candidates from every tier are pooled. One distinct user means RESOLVED.
Several users are narrowed to those whose matching claim was valid at the
request time. Exactly one survivor means RESOLVED; otherwise AMBIGUOUS.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from audit.loader import Dataset
from audit.models import (
    Activity,
    AuditEvent,
    DeletionRequest,
    IdentifierKind,
    Provenance,
    UserRecord,
)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_USER_ID = re.compile(r"^user_\d+$")
_PHONE_CHARS = re.compile(r"^\+?[\d\s().\-]+$")
PHONE_MIN_DIGITS, PHONE_MAX_DIGITS = 7, 15  # E.164 allows at most 15 digits

FIELD_KINDS = {
    "email": IdentifierKind.EMAIL,
    "phone": IdentifierKind.PHONE,
    "email_hash": IdentifierKind.EMAIL_HASH,
}


# --- normalisation -----------------------------------------------------------

def normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip().casefold()
    return value if _EMAIL.match(value) else None


def normalize_phone(raw: str | None) -> str | None:
    """Digits only, so '+1 (202) 555-0103' == '12025550103'. A leading
    international '00' prefix is dropped ('0044 7700...' == '+44 7700...')."""
    if not raw or not _PHONE_CHARS.match(raw.strip()):
        return None
    stripped = raw.strip()
    digits = re.sub(r"\D", "", stripped)
    if stripped.startswith("00"):
        digits = digits[2:]
    return digits if PHONE_MIN_DIGITS <= len(digits) <= PHONE_MAX_DIGITS else None


def normalize_hash(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip().lower()
    if value.startswith("sha256:"):
        value = value[len("sha256:"):]
    return value if _HASH.match(value) else None


def normalize_user_id(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip().lower()
    return value if _USER_ID.match(value) else None


NORMALIZERS = {
    IdentifierKind.EMAIL: normalize_email,
    IdentifierKind.PHONE: normalize_phone,
    IdentifierKind.EMAIL_HASH: normalize_hash,
    IdentifierKind.USER_ID: normalize_user_id,
}


def email_hash(email: str) -> str:
    """The dataset's email_hash: SHA-256 hex of the normalised email."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()


def classify_identifier(raw: str | None) -> tuple[IdentifierKind, str] | None:
    """Recognise an untyped value as email, user_id, hash or phone. Anything
    else (names, free text, resource IDs) is not an identifier."""
    if not raw:
        return None
    for kind in (IdentifierKind.EMAIL, IdentifierKind.USER_ID,
                 IdentifierKind.EMAIL_HASH, IdentifierKind.PHONE):
        value = NORMALIZERS[kind](raw)
        if value:
            return kind, value
    return None


# --- identity model ----------------------------------------------------------

@dataclass(frozen=True)
class IdentifierClaim:
    """A user held `value` from valid_from (inclusive) to valid_to (exclusive).
    None means unbounded: since before the records start, or still held."""
    kind: IdentifierKind
    value: str
    raw: str
    user_id: str
    provenance: Provenance
    valid_from: datetime | None
    valid_to: datetime | None
    source: str  # "user_identities" or the audit_id that recorded the change

    def held_at(self, moment: datetime) -> bool:
        return ((self.valid_from is None or self.valid_from <= moment)
                and (self.valid_to is None or moment < self.valid_to))


@dataclass(frozen=True)
class SubjectIdentity:
    user_id: str
    display_name: str  # never used for matching
    claims: tuple[IdentifierClaim, ...]

    def values(self, kind: IdentifierKind) -> frozenset[str]:
        return frozenset(c.value for c in self.claims if c.kind == kind)

    def identifiers(self) -> frozenset[tuple[IdentifierKind, str]]:
        return frozenset((c.kind, c.value) for c in self.claims)

    @property
    def historical_claims(self) -> tuple[IdentifierClaim, ...]:
        return tuple(c for c in self.claims if c.provenance is Provenance.HISTORICAL)

    def match(self, raw: str | None, kind: IdentifierKind | None = None) -> IdentifierClaim | None:
        """The claim an evidence value refers to, or None. With `kind`, the value
        is normalised as that kind; otherwise its kind is detected."""
        if kind is None:
            found = classify_identifier(raw)
            if not found:
                return None
            kind, value = found
        else:
            value = NORMALIZERS[kind](raw)
            if not value:
                return None
        return next((c for c in self.claims if c.kind == kind and c.value == value), None)


@dataclass(frozen=True)
class IdentityIssue:
    category: str  # conflict | ambiguous_identifier | invalid_value | unknown_user
    detail: str
    user_ids: tuple[str, ...] = ()


class ResolutionStatus(str, Enum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"    # several users fit; all kept as candidates
    UNRESOLVED = "UNRESOLVED"  # a valid email that matches nobody
    INVALID = "INVALID"        # not an email at all


@dataclass(frozen=True)
class Candidate:
    user_id: str
    method: str  # current_email | historical_email | email_hash
    claim: IdentifierClaim


@dataclass(frozen=True)
class Resolution:
    query: str
    normalized: str | None
    status: ResolutionStatus
    subject: SubjectIdentity | None
    candidates: tuple[Candidate, ...]
    notes: tuple[str, ...] = ()

    @property
    def user_id(self) -> str | None:
        return self.subject.user_id if self.subject else None

    @property
    def method(self) -> str | None:
        """How the resolved user was matched (highest-priority tier)."""
        if not self.subject:
            return None
        return next(c.method for c in self.candidates if c.user_id == self.subject.user_id)


@dataclass(frozen=True)
class ResourceLink:
    """A data-catalog assertion (LINK_RESOURCE_IDENTITY activity) that a
    resource belongs to a user. Trusted only if its email belongs to its user_id."""
    activity_id: str
    event_time: datetime
    system_id: str
    resource_id: str
    label: str        # the catalog's display label, e.g. a file name
    email: str
    user_id: str
    exclusive: bool   # the catalog says no other subject is linked
    trusted: bool
    note: str = ""


_LINK = re.compile(
    r"(?:linked|confirmed) (?P<resource>\S+) \((?P<label>[^)]*)\) "
    r"(?:to|belongs only to) (?P<email>[^\s;,]+@[^\s;,]+?) and (?P<user>user_\d+)")


# --- index -------------------------------------------------------------------

_METHOD_RANK = {"current_email": 0, "historical_email": 1, "email_hash": 2}


@dataclass
class IdentityIndex:
    subjects: dict[str, SubjectIdentity]
    issues: list[IdentityIssue] = field(default_factory=list)
    links: list[ResourceLink] = field(default_factory=list)
    _by_key: dict[tuple[IdentifierKind, str], list[IdentifierClaim]] = field(default_factory=dict)

    @classmethod
    def from_dataset(cls, dataset: Dataset) -> "IdentityIndex":
        return cls.build(dataset.users.values(), dataset.audit_events, dataset.activities)

    @classmethod
    def build(cls, users, audit_events, activities=()) -> "IdentityIndex":
        issues: list[IdentityIssue] = []
        users = {u.user_id: u for u in users}
        events_by_user: dict[str, list[AuditEvent]] = defaultdict(list)
        for event in audit_events:
            if event.user_id not in users:
                issues.append(IdentityIssue(
                    "unknown_user", f"{event.audit_id} changes {event.field_name} of "
                    f"unknown user {event.user_id}; ignored", (event.user_id,)))
                continue
            events_by_user[event.user_id].append(event)

        subjects = {}
        for user in users.values():
            claims = _claims_for_user(user, events_by_user.get(user.user_id, []), issues)
            subjects[user.user_id] = SubjectIdentity(user.user_id, user.name, tuple(claims))

        by_key: dict[tuple[IdentifierKind, str], list[IdentifierClaim]] = defaultdict(list)
        for subject in subjects.values():
            for claim in subject.claims:
                by_key[(claim.kind, claim.value)].append(claim)
        index = cls(subjects, issues, [], dict(by_key))
        index._flag_shared_identifiers()
        index.links = index._extract_links(activities)
        return index

    # lookups ------------------------------------------------------------------

    def claims(self, kind: IdentifierKind, value: str) -> list[IdentifierClaim]:
        """Claims on an already-normalised value."""
        return list(self._by_key.get((kind, value), ()))

    def owners(self, raw: str | None, kind: IdentifierKind | None = None,
               at: datetime | None = None) -> set[str]:
        """Users who held this identifier (at time `at`, if given). Names and
        other non-identifiers own nothing."""
        if kind is None:
            found = classify_identifier(raw)
            if not found:
                return set()
            kind, value = found
        else:
            value = NORMALIZERS[kind](raw)
            if not value:
                return set()
        return {c.user_id for c in self.claims(kind, value) if at is None or c.held_at(at)}

    def links_for(self, user_id: str, as_of: datetime | None = None) -> list[ResourceLink]:
        """Trusted resource links for a user, optionally only those known by `as_of`."""
        return [l for l in self.links if l.trusted and l.user_id == user_id
                and (as_of is None or l.event_time <= as_of)]

    # resolution ---------------------------------------------------------------

    def resolve_request(self, request: DeletionRequest) -> Resolution:
        return self.resolve_email(request.email, at=request.requested_at)

    def resolve_email(self, raw: str, at: datetime | None = None) -> Resolution:
        """Resolve an email to one user; `at` (the request time) is used only to
        separate users who held the same value at different times."""
        email = normalize_email(raw)
        if email is None:
            return Resolution(raw, None, ResolutionStatus.INVALID, None, (),
                              ("not a valid email address",))

        candidates = []
        for claim in self.claims(IdentifierKind.EMAIL, email):
            method = "current_email" if claim.provenance is Provenance.CURRENT else "historical_email"
            candidates.append(Candidate(claim.user_id, method, claim))
        for claim in self.claims(IdentifierKind.EMAIL_HASH, email_hash(email)):
            candidates.append(Candidate(claim.user_id, "email_hash", claim))
        candidates.sort(key=lambda c: (_METHOD_RANK[c.method], c.user_id, c.claim.source))

        users = sorted({c.user_id for c in candidates})
        if not users:
            return Resolution(raw, email, ResolutionStatus.UNRESOLVED, None, (),
                              ("no current or historical identifier matches",))
        if len(users) == 1:
            return Resolution(raw, email, ResolutionStatus.RESOLVED,
                              self.subjects[users[0]], tuple(candidates))

        notes = [f"value held by {len(users)} users: {', '.join(users)}"]
        if at is not None:
            held = sorted({c.user_id for c in candidates if c.claim.held_at(at)})
            if len(held) == 1:
                notes.append(f"only {held[0]} held it at the request time")
                return Resolution(raw, email, ResolutionStatus.RESOLVED,
                                  self.subjects[held[0]], tuple(candidates), tuple(notes))
            notes.append(f"{len(held)} users held it at the request time")
        return Resolution(raw, email, ResolutionStatus.AMBIGUOUS, None,
                          tuple(candidates), tuple(notes))

    # integrity checks -------------------------------------------------------------

    def _flag_shared_identifiers(self) -> None:
        for (kind, value), claims in sorted(self._by_key.items()):
            users = sorted({c.user_id for c in claims})
            if len(users) < 2:
                continue
            pairs = [(a, b) for i, a in enumerate(claims) for b in claims[i + 1:]
                     if a.user_id != b.user_id]
            if any(a.valid_to is None and b.valid_to is None for a, b in pairs):
                when = "at the same time (both current)"
            elif any(_overlaps(a, b) for a, b in pairs):
                when = "at possibly overlapping times (a start or end is unknown)"
            else:
                when = "at different times"
            self.issues.append(IdentityIssue(
                "ambiguous_identifier", f"{kind.value} {value} is held by {', '.join(users)} {when}",
                tuple(users)))

    def _extract_links(self, activities) -> list[ResourceLink]:
        links = []
        for activity in activities:
            if activity.event_type != "LINK_RESOURCE_IDENTITY":
                continue
            link = self._parse_link(activity)
            if link is None:
                self.issues.append(IdentityIssue(
                    "invalid_value", f"{activity.activity_id}: unrecognised link text"))
                continue
            if not link.trusted:
                self.issues.append(IdentityIssue(
                    "conflict", f"{activity.activity_id}: {link.note}", (link.user_id,)))
            links.append(link)
        return links

    def _parse_link(self, activity: Activity) -> ResourceLink | None:
        match = _LINK.search(activity.details)
        if not match:
            return None
        email, user_id = match.group("email"), match.group("user")
        owners = self.owners(email, IdentifierKind.EMAIL)
        problems = []
        if match.group("resource") != activity.resource_id:
            problems.append(f"details name {match.group('resource')} but the row targets "
                            f"{activity.resource_id}")
        if user_id not in self.subjects:
            problems.append(f"unknown user {user_id}")
        elif owners != {user_id}:
            problems.append(f"email {email} belongs to {sorted(owners) or 'nobody'}, "
                            f"not {user_id}")
        exclusive = "belongs only to" in activity.details or "no other subject" in activity.details
        return ResourceLink(activity.activity_id, activity.event_time, activity.target_system,
                            activity.resource_id, match.group("label"), email, user_id,
                            exclusive, trusted=not problems, note="; ".join(problems))


def _overlaps(a: IdentifierClaim, b: IdentifierClaim) -> bool:
    """True unless known bounds prove the two windows are disjoint. An unknown
    start (valid_from=None) may reach back arbitrarily far, so it can overlap."""
    start_a, start_b = a.valid_from, b.valid_from
    end_a, end_b = a.valid_to, b.valid_to
    a_before_b = end_a is not None and start_b is not None and end_a <= start_b
    b_before_a = end_b is not None and start_a is not None and end_b <= start_a
    return not (a_before_b or b_before_a)


def _claims_for_user(user: UserRecord, events: list[AuditEvent],
                     issues: list[IdentityIssue]) -> list[IdentifierClaim]:
    """Rebuild each field's value timeline from the audit trail and snapshot."""
    claims = [IdentifierClaim(IdentifierKind.USER_ID, normalize_user_id(user.user_id) or user.user_id,
                              user.user_id, user.user_id, Provenance.CURRENT, None, None,
                              "user_identities")]
    for field_name, kind in FIELD_KINDS.items():
        changes = sorted((e for e in events if e.field_name == field_name),
                         key=lambda e: (e.event_time, e.audit_id))
        current_raw = getattr(user, field_name)
        start = None
        for i, change in enumerate(changes):
            if i and changes[i - 1].new_value != change.old_value:
                issues.append(IdentityIssue(
                    "conflict", f"{user.user_id} {field_name}: {changes[i - 1].audit_id} set "
                    f"{changes[i - 1].new_value!r} but {change.audit_id} replaces "
                    f"{change.old_value!r}", (user.user_id,)))
            _add(claims, issues, kind, change.old_value, user.user_id, Provenance.HISTORICAL,
                 start, change.event_time, change.audit_id)
            start = change.event_time
        if changes and changes[-1].new_value != current_raw:
            issues.append(IdentityIssue(
                "conflict", f"{user.user_id} {field_name}: audit trail ends at "
                f"{changes[-1].new_value!r} but the snapshot holds {current_raw!r}; both kept",
                (user.user_id,)))
            _add(claims, issues, kind, changes[-1].new_value, user.user_id,
                 Provenance.HISTORICAL, start, None, changes[-1].audit_id)
        _add(claims, issues, kind, current_raw, user.user_id, Provenance.CURRENT,
             start, None, "user_identities")
    _check_email_hash_pairs(user.user_id, claims, issues)
    return claims


def _check_email_hash_pairs(user_id: str, claims: list[IdentifierClaim],
                            issues: list[IdentityIssue]) -> None:
    """Each email should have a hash claim of sha256(email) over the same window,
    as in every real record. Mismatches are reported; both claims are kept."""
    hashes = {c.value: c for c in claims if c.kind is IdentifierKind.EMAIL_HASH}
    paired = set()
    for claim in (c for c in claims if c.kind is IdentifierKind.EMAIL):
        expected = email_hash(claim.value)
        hash_claim = hashes.get(expected)
        if hash_claim is None:
            issues.append(IdentityIssue(
                "conflict", f"{user_id} email {claim.value} has no matching email_hash record",
                (user_id,)))
            continue
        paired.add(expected)
        if (hash_claim.valid_from, hash_claim.valid_to) != (claim.valid_from, claim.valid_to):
            issues.append(IdentityIssue(
                "conflict", f"{user_id} email {claim.value} and its email_hash were held over "
                "different periods", (user_id,)))
    for value in sorted(set(hashes) - paired):
        issues.append(IdentityIssue(
            "conflict", f"{user_id} email_hash {value[:12]}... matches none of the user's "
            "recorded emails", (user_id,)))


def _add(claims, issues, kind, raw, user_id, provenance, valid_from, valid_to, source) -> None:
    value = NORMALIZERS[kind](raw)
    if value is None:
        if raw:
            issues.append(IdentityIssue(
                "invalid_value", f"{user_id} {kind.value} {raw!r} from {source} cannot be "
                "normalised; ignored", (user_id,)))
        return
    if any(c.kind == kind and c.value == value and c.provenance == provenance
           and c.valid_from == valid_from for c in claims):
        return
    claims.append(IdentifierClaim(kind, value, raw, user_id, provenance,
                                  valid_from, valid_to, source))
