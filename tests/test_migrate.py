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


def test_migrate_moves_leftover_archive_when_registry_already_moved(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home"))
    old = tmp_path / "repo"
    old.mkdir()
    # registry already lives in state_dir; only a stale archive/ remains behind
    (tmp_path / "home" / "ringleader").mkdir(parents=True)
    (tmp_path / "home" / "ringleader" / "registry.json").write_text('{"threads": []}')
    (old / "archive").mkdir()
    moved = migrate(old)
    assert (tmp_path / "home" / "ringleader" / "archive").exists()
    assert not (old / "archive").exists()
    assert moved
