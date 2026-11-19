import pytest


@pytest.fixture(autouse=True)
def default_codex_sqlite_location(isolate_agent_environment, monkeypatch):
    monkeypatch.delenv("CODEX_SQLITE_HOME", raising=False)
