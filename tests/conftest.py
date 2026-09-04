from pathlib import Path

import pytest

LIVE_REGISTRY = Path(__file__).resolve().parent.parent / "registry.json"


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
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True, scope="session")
def guard_live_registry():
    # the rl commands default to the repo's real registry, so one missing --registry
    # in a test silently overwrites the user's live cache
    before = LIVE_REGISTRY.read_bytes() if LIVE_REGISTRY.exists() else None
    yield
    after = LIVE_REGISTRY.read_bytes() if LIVE_REGISTRY.exists() else None
    if after == before:
        return
    if before is None:
        LIVE_REGISTRY.unlink()
    else:
        LIVE_REGISTRY.write_bytes(before)
    pytest.fail(
        "a test wrote to the live registry.json (restored) — "
        "pass --registry <tmp path> to every rl invocation"
    )
