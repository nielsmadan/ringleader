from pathlib import Path

from ringleader import paths, registry
from ringleader.paths import state_dir


def test_state_dir_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert state_dir() == tmp_path / "ringleader"


def test_state_dir_defaults_to_local_share(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert state_dir() == Path.home() / ".local" / "share" / "ringleader"


def test_ensure_state_dir_creates_it(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "fresh"))
    d = paths.ensure_state_dir()
    assert d.is_dir()
    assert paths.ensure_state_dir() == d  # idempotent


def test_save_registry_creates_a_missing_state_dir(tmp_path, monkeypatch):
    """A fresh XDG_DATA_HOME must not crash the first write — several writers live
    under state_dir() and none of them can assume it exists yet."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "fresh"))
    target = paths.default_registry()
    assert not target.parent.exists()
    registry.save_registry(target, registry.empty_registry())
    assert target.exists()
