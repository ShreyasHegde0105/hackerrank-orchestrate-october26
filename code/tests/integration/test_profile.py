import profile_dataset


def test_profile_runs_and_recognises_every_log_format(tmp_path, monkeypatch):
    out = tmp_path / "profile.md"
    monkeypatch.setattr(profile_dataset, "OUT_PATH", out)
    assert profile_dataset.main() == 0
    text = out.read_text(encoding="utf-8")
    assert "| free text |" not in text
    assert "listed but missing: none; on disk but unlisted: none" in text
    assert "Cited evidence captured after the case's audit_at: 0" in text
