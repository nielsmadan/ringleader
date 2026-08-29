from ringleader import session_env


def _clear(monkeypatch):
    for var in session_env.PROBES.values():
        monkeypatch.delenv(var, raising=False)


def test_resolves_claude_code(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "93155a0b-f168-4b70-bfb3-264496afca79")
    assert session_env.current_session() == (
        "claude-code",
        "93155a0b-f168-4b70-bfb3-264496afca79",
    )


def test_returns_none_when_no_agent_is_identifiable(monkeypatch):
    _clear(monkeypatch)
    assert session_env.current_session() is None


def test_ignores_an_empty_variable(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
    assert session_env.current_session() is None
