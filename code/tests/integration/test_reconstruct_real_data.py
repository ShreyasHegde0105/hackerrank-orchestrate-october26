"""Phase 4 on the real dataset: sample-answer comparison and leakage guarantees."""
import pytest

import reconstruction_report
from audit.reconstruct import ABSENT, CONFLICT, CONFLICTED, PRESENT, UNKNOWN


@pytest.fixture(scope="module")
def samples():
    return reconstruction_report.build("sample_deletion_requests.csv")


@pytest.fixture(scope="module")
def evaluation():
    return reconstruction_report.build("deletion_requests.csv")


@pytest.fixture(scope="module")
def comparison(samples):
    rc, cases = samples
    return reconstruction_report.compare(cases, reconstruction_report.load_expectations(), rc)


def test_findings_and_complete_verifications_all_match(comparison):
    for kind in ("finding", "verified (COMPLETE)"):
        found = [r for r in comparison if r["kind"] == kind and r["actual"] != "NOT DISCOVERED"]
        assert all(r["state_match"] for r in found), kind
        assert all(r["citation_match"] for r in found if r["citation_match"] is not None), kind
    assert sum(1 for r in comparison if r["kind"] == "finding" and r["actual"] != "NOT DISCOVERED") == 41


def test_overall_sample_agreement_baseline(comparison):
    found = [r for r in comparison if r["actual"] != "NOT DISCOVERED"]
    cited = [r for r in found if r["citation_match"] is not None]
    assert (len(comparison), len(found), sum(r["state_match"] for r in found)) == (111, 84, 83)
    assert (len(cited), sum(r["citation_match"] for r in cited)) == (60, 60)
    mismatches = [(r["case"], r["resource"], r["actual"]) for r in found if not r["state_match"]]
    assert mismatches == [("case_06", "account_006", UNKNOWN)]  # never-observed: not assumed absent


def test_undiscovered_records_are_explained(comparison):
    reasons = {r["explanation"] for r in comparison if r["actual"] == "NOT DISCOVERED"}
    assert reasons <= {"activity log only", "absent from all evidence and activity",
                       "evidence carries no identifier (e.g. anonymized)",
                       "only evidence dated after audit_at links it to the subject"}


@pytest.mark.parametrize("which", ["samples", "evaluation"])
def test_no_future_evidence_decides_or_is_cited(request, which):
    rc, cases = request.getfixturevalue(which)
    observations = {o.obs_id: o for o in rc.result.observations}
    for case in cases:
        for state in case.resources:
            for obs_id in state.supporting:
                if obs_id.startswith("coverage:"):
                    continue
                obs = observations[obs_id]
                assert obs.observed_at <= case.audit_at, (case.case_id, obs_id)
                assert not rc.is_anomalous(obs)
            if state.citation:
                assert rc.dataset.evidence_files[state.citation].captured_at <= case.audit_at
            if state.state in (UNKNOWN, CONFLICT):
                assert state.citation is None


@pytest.mark.parametrize("which", ["samples", "evaluation"])
def test_absence_always_has_qualifying_evidence(request, which):
    rc, cases = request.getfixturevalue(which)
    observations = {o.obs_id: o for o in rc.result.observations}
    for case in cases:
        for state in case.resources:
            for copy in state.copies:
                if copy.state != ABSENT:
                    continue
                for ref in copy.decided_by:
                    if ref.startswith("coverage:"):
                        evidence_id = ref[len("coverage:"):].split("#")[0]
                        assert rc.dataset.evidence_files[evidence_id].kind == "sql"
                    else:
                        assert observations[ref].condition.value == "ABSENT"


def test_known_cases(samples):
    _, cases = samples
    by_id = {c.case_id: c for c in cases}
    account_005 = by_id["case_05"].resource("account_005")
    assert account_005.state == PRESENT and account_005.citation == "evidence_54"
    assert [c.copy for c in account_005.copies] == ["failover_replica"]
    audit_3954 = by_id["case_29"].resource("audit_record_3954")
    assert audit_3954.state == PRESENT and audit_3954.citation is None  # only in a later file
    support_001 = by_id["case_07"].resource("support_001")
    assert support_001.state == PRESENT and support_001.citation == "evidence_58"
    assert by_id["case_17"].resource("tombstone_17").state == UNKNOWN  # only an anomalous screenshot


def test_marketing_conflict_is_preserved(evaluation):
    _, cases = evaluation
    case_63 = next(c for c in cases if c.case_id == "case_63")
    state = case_63.resource("marketing_001")
    assert (state.state, state.relationship) == (CONFLICT, CONFLICTED)
    assert state.existence == PRESENT and state.citation is None
    assert "relationship conflicted" in state.reason
    assert [c.case_id for c in cases for r in c.resources if r.relationship == CONFLICTED] == ["case_63"]


def test_reconstruction_is_deterministic(samples):
    rc, cases = samples
    again = rc.reconstruct_all()
    assert [c.resources for c in again] == [c.resources for c in cases]
    assert [c.activity_candidates for c in again] == [c.activity_candidates for c in cases]
