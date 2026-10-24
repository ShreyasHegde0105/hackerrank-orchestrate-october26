import identity_report


def test_identity_report_runs_and_reports_no_unresolved(tmp_path, monkeypatch):
    out = tmp_path / "identity.md"
    monkeypatch.setattr(identity_report, "OUT_PATH", out)
    assert identity_report.main() == 0
    text = out.read_text(encoding="utf-8")
    assert text.count("Ambiguous, unresolved or invalid requests: none.") == 2
    assert "- Status: {'RESOLVED': 518}" in text
    assert "Request emails that match only after case/whitespace normalisation: 20" in text
    assert "Integrity issues (shared identifiers, broken audit chains, email/hash " \
           "mismatches, unknown users): none." in text
