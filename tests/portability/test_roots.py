from __future__ import annotations

from pathlib import Path

import pytest
from ringleader.portability.adapters import builtin_adapters
from ringleader.portability.model import PreservationError
from ringleader.sources.claude_code import default_projects_dir
from ringleader.sources.codex import default_codex_dir
from ringleader.sources.pi import default_sessions_dir
from tests.portability.test_cli import environment as cli_environment


@pytest.fixture
def environment(tmp_path, monkeypatch):
    return cli_environment.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "case",
    [
        ("claude-code", ".claude", "CLAUDE_CONFIG_DIR", "projects", "SESSION_TRACKER_PROJECTS_DIR"),
        ("codex", ".codex", "CODEX_HOME", "sessions", "CODEX_SESSIONS_DIR"),
        ("pi", ".pi/agent", "PI_CODING_AGENT_DIR", "sessions", "PI_SESSIONS_DIR"),
    ],
)
def test_native_and_scan_override_precedence(environment, monkeypatch, case):
    agent, store, variable, leaf, scan = case
    adapters = builtin_adapters()
    assert adapters.roots(agent, environment).store == environment / "home" / store
    native = environment / "custom-native"
    monkeypatch.setenv(variable, str(native))
    assert adapters.roots(agent, environment).store == native
    scanners = {
        "claude-code": default_projects_dir,
        "codex": default_codex_dir,
        "pi": default_sessions_dir,
    }
    assert scanners[agent]() == native / leaf
    monkeypatch.setenv(scan, str(native / leaf))
    assert adapters.roots(agent, environment).store == native
    monkeypatch.setenv(scan, str(environment / "other" / leaf))
    with pytest.raises(PreservationError, match="disagrees"):
        adapters.roots(agent, environment)
    monkeypatch.delenv(variable)
    assert adapters.roots(agent, environment).store == environment / "other"
    monkeypatch.setenv(scan, str(environment / "arbitrary-leaf"))
    with pytest.raises(PreservationError, match="must end"):
        adapters.roots(agent, environment)
    assert (
        adapters.roots(
            agent, environment, source_ref=str(environment / "archive" / "source.jsonl")
        ).cwd
        == environment
    )


def test_real_xdg_defaults_and_opencode_override_consistency(environment, monkeypatch):
    for key in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(key)
    roots = builtin_adapters().roots("opencode", environment)
    home = environment / "home"
    assert (roots.data, roots.state, roots.config, roots.cache) == (
        home / ".local/share",
        home / ".local/state",
        home / ".config",
        home / ".cache",
    )
    assert roots.store == home / ".local/share/opencode"
    monkeypatch.setenv("OPENCODE_DB", str(environment / "custom/opencode/opencode.db"))
    with pytest.raises(PreservationError, match="disagrees"):
        builtin_adapters().roots("opencode", environment)
    monkeypatch.setenv("XDG_DATA_HOME", str(environment / "custom"))
    assert (
        builtin_adapters().roots("opencode", environment).store == environment / "custom/opencode"
    )
    assert isinstance(roots.temporary, Path)
