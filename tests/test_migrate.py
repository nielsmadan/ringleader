from ringleader.migrate import migrate


def test_migrate_moves_legacy_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home"))
    old = tmp_path / "repo"
    old.mkdir()
    (old / "registry.json").write_text('{"threads": []}')
    (old / "archive").mkdir()
    moved = migrate(old)
    assert (tmp_path / "home" / "ringleader" / "registry.json").exists()
    assert moved  # non-empty
    # idempotent second run is a no-op
    assert migrate(old) == []
