from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).resolve().parent.parent / ".agents" / "skills"


@pytest.fixture(scope="session")
def skill_dirs():
    """The bundled skills, discovered once. Shared so the files that check them cannot
    disagree about what counts as a skill."""
    return sorted(p for p in SKILLS_ROOT.glob("ringleader-*") if (p / "SKILL.md").is_file())


@pytest.fixture(autouse=True)
def isolate_agent_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(tmp_path / "_claude"))
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(tmp_path / "_codex"))
    monkeypatch.setenv("PI_SESSIONS_DIR", str(tmp_path / "_pi"))
    # keeps `rl doctor` from finding — and running — the machine's real hooklinesinker
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "_data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "_state"))
    for name in (
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "PI_SESSION_ID",
        "OPENCODE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "OPENCODE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "PI_CODING_AGENT_DIR",
        "QWEN_HOME",
        "KIMI_CODE_HOME",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True, scope="session")
def guard_live_state(tmp_path_factory):
    """Every rl command defaults `--registry` to the user's real registry, so one missing
    `--registry` in a test would read and overwrite live session state. Point the whole
    state dir at a throwaway path for the session.

    `XDG_DATA_HOME` is the one lever that works: `paths.state_dir()` reads it on every
    call, so every helper built on it moves together — registry, archive root, config,
    publish receipts — whereas patching `default_registry` would miss the copy each
    command module bound at import time.

    Prevention rather than the snapshot-and-restore this used to attempt: `rl data
    migrate` moved the live file under `~/.local/share`, where the user's own session-end
    hooks write it while the suite runs, so restoring a snapshot would sooner or later
    revert one of their writes."""
    patch = pytest.MonkeyPatch()
    patch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("xdg-data")))
    yield
    patch.undo()
