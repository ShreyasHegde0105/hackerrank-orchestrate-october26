"""Identity resolution on small hand-built data, one behaviour per test."""
from datetime import datetime, timezone

import pytest

from audit.identity import (
    IdentityIndex,
    ResolutionStatus,
    classify_identifier,
    email_hash,
    normalize_email,
    normalize_hash,
    normalize_phone,
    normalize_user_id,
)
from audit.models import Activity, AuditEvent, IdentifierKind, Provenance, UserRecord


def ts(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 6, day, hour, tzinfo=timezone.utc)


def user(user_id, email, phone="+1 (202) 555-0100", name="Some Person", hash_=None):
    return UserRecord(user_id, name, email, phone,
                      hash_ if hash_ is not None else email_hash(email.lower()), ts(1))


def change(audit_id, user_id, day, field, old, new):
    return AuditEvent(audit_id, user_id, ts(day), field, old, new)


def link(activity_id, resource, email, user_id, day=10, system="search_service", phrase=None):
    details = phrase or f"Data catalog linked {resource} ({resource}) to {email} and {user_id}."
    return Activity(activity_id, ts(day), "LINK_RESOURCE_IDENTITY", "data_catalog", system,
                    resource, "SUCCESS", details)


@pytest.fixture
def index():
    users = [
        user("user_1", "alice@new.com", "+1 (202) 555-0101"),
        user("user_2", "bob@x.com", "+44 7700 900111"),
        user("user_3", "sam.one@x.com", "+1 202 555 0103", name="Sam Lee"),
        user("user_4", "sam.two@x.com", "+1 202 555 0104", name="Sam Lee"),
        user("user_5", "carol@now.com", "+1 202 555 0105"),
        user("user_6", "shared@x.com", "+1 202 555 0106"),
        user("user_9", "dan@x.com", "+1 202 555 0109"),
        user("user_10", "reused@x.com", "+1 202 555 0110"),
        user("user_11", "gina@x.com", "+1 202 555 0111"),
    ]
    events = [
        change("audit_1", "user_1", 5, "email", "alice@old.com", "alice@new.com"),
        change("audit_2", "user_1", 5, "email_hash", email_hash("alice@old.com"),
               email_hash("alice@new.com")),
        change("audit_3", "user_1", 7, "phone", "+1 202 555 0199", "+1 (202) 555-0101"),
        # user_5 used shared@x.com until day 8; user_6 holds it now (snapshot)
        change("audit_4", "user_5", 8, "email", "shared@x.com", "carol@now.com"),
        change("audit_4h", "user_5", 8, "email_hash", email_hash("shared@x.com"),
               email_hash("carol@now.com")),
        # user_9's old email survives only as a hash
        change("audit_5", "user_9", 4, "email_hash", email_hash("ghost@x.com"),
               email_hash("dan@x.com")),
        # reused@x.com: user_11 released it on day 8, user_10 took it on day 9.
        # As in the real data, every email change is paired with a hash change.
        change("audit_6", "user_11", 8, "email", "reused@x.com", "gina@x.com"),
        change("audit_7", "user_11", 8, "email_hash", email_hash("reused@x.com"),
               email_hash("gina@x.com")),
        change("audit_8", "user_10", 9, "email", "frank@x.com", "reused@x.com"),
        change("audit_9", "user_10", 9, "email_hash", email_hash("frank@x.com"),
               email_hash("reused@x.com")),
    ]
    return IdentityIndex.build(users, events)


# --- normalisation -------------------------------------------------------------

@pytest.mark.parametrize("raw", ["+1 (202) 555-0101", "1-202-555-0101", "001 202 555 0101",
                                 "12025550101", " +1.202.555.0101 "])
def test_phone_formats_normalise_to_the_same_digits(raw):
    assert normalize_phone(raw) == "12025550101"


@pytest.mark.parametrize("raw", ["", None, "555-01", "call me", "+1 202 555 0101 ext 7",
                                 "1234567890123456"])
def test_invalid_phones_are_rejected(raw):
    assert normalize_phone(raw) is None


def test_email_normalisation():
    assert normalize_email("  Alice@NEW.com ") == "alice@new.com"
    for raw in ("", None, "not-an-email", "a@b", "two@@x.com", "a b@x.com"):
        assert normalize_email(raw) is None


def test_hash_and_user_id_normalisation():
    digest = email_hash("alice@new.com")
    assert normalize_hash(digest.upper()) == digest
    assert normalize_hash("sha256:" + digest) == digest
    assert normalize_hash("abc123") is None
    assert normalize_user_id(" USER_12 ") == "user_12"
    assert normalize_user_id("account_12") is None


def test_classify_identifier_recognises_kinds_and_rejects_names():
    assert classify_identifier("Bob@X.com") == (IdentifierKind.EMAIL, "bob@x.com")
    assert classify_identifier("user_7") == (IdentifierKind.USER_ID, "user_7")
    assert classify_identifier(email_hash("a@b.com"))[0] is IdentifierKind.EMAIL_HASH
    assert classify_identifier("+44 7700 900111") == (IdentifierKind.PHONE, "447700900111")
    for not_identifier in ("Sam Lee", "account_5", "search_12", "", None, "null"):
        assert classify_identifier(not_identifier) is None


# --- resolution ------------------------------------------------------------------

def test_exact_current_email(index):
    result = index.resolve_email("bob@x.com")
    assert result.status is ResolutionStatus.RESOLVED
    assert result.user_id == "user_2" and result.method == "current_email"


def test_case_and_whitespace_insensitive_email(index):
    result = index.resolve_email("  ALICE@New.COM ")
    assert result.user_id == "user_1" and result.normalized == "alice@new.com"


def test_historical_email(index):
    result = index.resolve_email("alice@old.com", at=ts(20))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.user_id == "user_1" and result.method == "historical_email"
    claim = next(c.claim for c in result.candidates if c.method == "historical_email")
    assert claim.source == "audit_1" and claim.valid_to == ts(5)


def test_email_known_only_by_its_historical_hash(index):
    result = index.resolve_email("Ghost@X.com")
    assert result.status is ResolutionStatus.RESOLVED
    assert result.user_id == "user_9" and result.method == "email_hash"


def test_subject_carries_all_current_and_historical_identifiers(index):
    subject = index.resolve_email("alice@new.com").subject
    assert subject.values(IdentifierKind.EMAIL) == {"alice@new.com", "alice@old.com"}
    assert subject.values(IdentifierKind.EMAIL_HASH) == {email_hash("alice@new.com"),
                                                        email_hash("alice@old.com")}
    assert subject.values(IdentifierKind.PHONE) == {"12025550101", "12025550199"}
    assert subject.values(IdentifierKind.USER_ID) == {"user_1"}
    assert {c.source for c in subject.historical_claims} == {"audit_1", "audit_2", "audit_3"}
    # evidence values in any format match the right claim
    assert subject.match("ALICE@OLD.COM").provenance is Provenance.HISTORICAL
    assert subject.match("(202) 555-0199", IdentifierKind.PHONE) is None  # no country code
    assert subject.match("+1 202-555-0199").source == "audit_3"
    assert subject.match(email_hash("alice@old.com").upper()).source == "audit_2"
    assert subject.match("user_1").kind is IdentifierKind.USER_ID


def test_claim_validity_windows(index):
    subject = index.subjects["user_1"]
    old = subject.match("alice@old.com")
    new = subject.match("alice@new.com")
    assert old.held_at(ts(4)) and not old.held_at(ts(5))
    assert new.held_at(ts(5)) and not new.held_at(ts(4))


def test_unknown_identity_is_unresolved(index):
    result = index.resolve_email("nobody@nowhere.com")
    assert result.status is ResolutionStatus.UNRESOLVED and result.subject is None


@pytest.mark.parametrize("raw", ["", None, "Sam Lee", "user_3", "+1 202 555 0103"])
def test_missing_or_non_email_input_is_invalid(index, raw):
    result = index.resolve_email(raw)
    assert result.status is ResolutionStatus.INVALID and result.user_id is None


def test_reused_email_is_ambiguous_without_a_time(index):
    result = index.resolve_email("shared@x.com")
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert {c.user_id for c in result.candidates} == {"user_5", "user_6"}
    assert result.subject is None


def test_reuse_with_recorded_handover_is_separated_by_request_time(index):
    assert index.resolve_email("reused@x.com", at=ts(3)).user_id == "user_11"
    later = index.resolve_email("reused@x.com", at=ts(20))
    assert later.user_id == "user_10"
    assert any("only user_10" in note for note in later.notes)
    # between release (day 8) and takeover (day 9) nobody held it
    assert index.resolve_email("reused@x.com",
                               at=ts(8, 12)).status is ResolutionStatus.AMBIGUOUS
    issue = next(i for i in index.issues if "reused@x.com" in i.detail)
    assert "different times" in issue.detail


def test_reuse_with_unknown_takeover_date_stays_ambiguous_before_release(index):
    # user_6's start on shared@x.com is unknown, so on day 3 either user may
    # have held it: the resolver must not guess user_5.
    early = index.resolve_email("shared@x.com", at=ts(3))
    assert early.status is ResolutionStatus.AMBIGUOUS
    assert {c.user_id for c in early.candidates} == {"user_5", "user_6"}
    # after user_5's recorded release, only user_6 can hold it
    assert index.resolve_email("shared@x.com", at=ts(20)).user_id == "user_6"
    issue = next(i for i in index.issues if "shared@x.com" in i.detail)
    assert "possibly overlapping" in issue.detail


def test_simultaneous_owners_stay_ambiguous_even_with_a_time():
    users = [user("user_7", "dup@x.com", "+1 202 555 0107"),
             user("user_8", "DUP@x.com", "+1 202 555 0108")]
    index = IdentityIndex.build(users, [])
    result = index.resolve_email("dup@x.com", at=ts(20))
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert {c.user_id for c in result.candidates} == {"user_7", "user_8"}
    assert any("both current" in i.detail for i in index.issues)


def test_names_never_resolve_or_own_anything(index):
    assert index.owners("Sam Lee") == set()
    assert index.resolve_email("Sam Lee").status is ResolutionStatus.INVALID
    assert index.subjects["user_3"].display_name == index.subjects["user_4"].display_name
    assert index.subjects["user_3"].identifiers().isdisjoint(index.subjects["user_4"].identifiers())


def test_owners_by_any_identifier_kind_and_time(index):
    assert index.owners("ALICE@old.com") == {"user_1"}
    assert index.owners("+1 202 555 0199") == {"user_1"}
    assert index.owners(email_hash("ghost@x.com")) == {"user_9"}
    assert index.owners("user_2") == {"user_2"}
    assert index.owners("reused@x.com", at=ts(3)) == {"user_11"}
    assert index.owners("shared@x.com", at=ts(3)) == {"user_5", "user_6"}
    assert index.owners("alice@old.com", at=ts(20)) == set()


def test_missing_identifier_field_creates_no_claim_and_no_issue():
    index = IdentityIndex.build([user("user_1", "a@x.com", phone="")], [])
    assert index.subjects["user_1"].values(IdentifierKind.PHONE) == set()
    assert index.issues == []


# --- conflicting source data -------------------------------------------------------

def test_snapshot_disagreeing_with_audit_trail_keeps_both_values():
    users = [user("user_1", "c@x.com")]
    events = [change("audit_1", "user_1", 5, "email", "a@x.com", "b@x.com")]
    index = IdentityIndex.build(users, events)
    assert index.subjects["user_1"].values(IdentifierKind.EMAIL) == {"a@x.com", "b@x.com", "c@x.com"}
    assert any(i.category == "conflict" and "both kept" in i.detail for i in index.issues)


def test_broken_audit_chain_is_a_conflict():
    users = [user("user_1", "c@x.com")]
    events = [change("audit_1", "user_1", 5, "email", "a@x.com", "b@x.com"),
              change("audit_2", "user_1", 6, "email", "z@x.com", "c@x.com")]
    index = IdentityIndex.build(users, events)
    assert any("audit_2 replaces 'z@x.com'" in i.detail for i in index.issues)


def test_email_and_hash_histories_must_agree():
    # email changed on day 5 but the hash change was never recorded
    users = [user("user_1", "b@x.com")]
    index = IdentityIndex.build(users, [change("audit_1", "user_1", 5, "email", "a@x.com", "b@x.com")])
    details = " | ".join(i.detail for i in index.issues)
    assert "email a@x.com has no matching email_hash record" in details
    assert "email b@x.com and its email_hash were held over different periods" in details


def test_orphan_hash_is_reported_but_still_resolvable(index):
    assert any("user_9 email_hash" in i.detail and "matches none" in i.detail
               for i in index.issues)
    assert index.resolve_email("ghost@x.com").user_id == "user_9"


def test_consistent_fixture_users_raise_no_conflicts(index):
    flagged = {u for i in index.issues if i.category == "conflict" for u in i.user_ids}
    assert flagged == {"user_9"}  # only the deliberate orphan-hash user


def test_audit_event_for_unknown_user_is_ignored_and_reported():
    index = IdentityIndex.build([user("user_1", "a@x.com")],
                                [change("audit_9", "user_404", 5, "email", "q@x.com", "r@x.com")])
    assert index.owners("q@x.com") == set()
    assert index.issues[0].category == "unknown_user"


def test_unnormalisable_value_is_reported_not_indexed():
    index = IdentityIndex.build([user("user_1", "a@x.com", phone="n/a")], [])
    assert index.subjects["user_1"].values(IdentifierKind.PHONE) == set()
    assert index.issues[0].category == "invalid_value"


# --- resource links -----------------------------------------------------------------

def test_consistent_link_is_trusted_and_time_filtered():
    index = IdentityIndex.build([user("user_1", "a@x.com")],
                               [], [link("activity_1", "search_5", "a@x.com", "user_1", day=10)])
    assert [l.resource_id for l in index.links_for("user_1")] == ["search_5"]
    assert index.links_for("user_1", as_of=ts(9)) == []
    assert index.links_for("user_1", as_of=ts(10))[0].system_id == "search_service"


def test_link_whose_email_belongs_to_someone_else_is_untrusted():
    users = [user("user_1", "a@x.com"), user("user_2", "b@x.com")]
    index = IdentityIndex.build(users, [], [link("activity_1", "search_5", "b@x.com", "user_1")])
    assert index.links_for("user_1") == [] and not index.links[0].trusted
    assert any(i.category == "conflict" and "belongs to ['user_2']" in i.detail
               for i in index.issues)


def test_exclusive_attachment_link_is_detected():
    phrase = ("Attachment catalog confirmed object_19 (delivery_confirmation_19.pdf) belongs only "
              "to a@x.com and user_1; no other subject is linked.")
    index = IdentityIndex.build([user("user_1", "a@x.com")], [],
                                [link("activity_1", "object_19", "", "", system="object_storage",
                                      phrase=phrase)])
    found = index.links_for("user_1")[0]
    assert found.exclusive and found.label == "delivery_confirmation_19.pdf"


def test_unrecognised_link_text_is_reported():
    index = IdentityIndex.build([user("user_1", "a@x.com")], [],
                                [link("activity_1", "x_1", "", "", phrase="something else")])
    assert index.links == [] and index.issues[0].category == "invalid_value"
