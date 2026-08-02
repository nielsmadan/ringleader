from pathlib import Path

from ringleader.paths import state_dir


def test_state_dir_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert state_dir() == tmp_path / "ringleader"


def test_state_dir_defaults_to_local_share(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert state_dir() == Path.home() / ".local" / "share" / "ringleader"
