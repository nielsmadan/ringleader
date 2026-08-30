from ringleader import session_env


def _clear(monkeypatch):
    for var in session_env.PROBES.values():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(session_env._CODEX_FALLBACK, raising=False)


def test_resolves_claude_code(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "93155a0b-f168-4b70-bfb3-264496afca79")
    assert session_env.current_session() == (
        "claude-code",
        "93155a0b-f168-4b70-bfb3-264496afca79",
    )


def test_resolves_codex_thread_id(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CODEX_THREAD_ID", "019a0544-4395-7b72-a274-b58dee63fed5")
    assert session_env.current_session() == ("codex", "019a0544-4395-7b72-a274-b58dee63fed5")


def test_codex_fallback_to_session_id(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CODEX_SESSION_ID", "019a0544-4395-7b72-a274-b58dee63fed5")
    assert session_env.current_session() == ("codex", "019a0544-4395-7b72-a274-b58dee63fed5")


def test_codex_prefers_thread_id_over_session_id(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-id")
    monkeypatch.setenv("CODEX_SESSION_ID", "session-id")
    assert session_env.current_session() == ("codex", "thread-id")


def test_resolves_pi(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("PI_SESSION_ID", "019f9a29-e722-7f9d-ac7e-3e401ee8e83b")
    assert session_env.current_session() == ("pi", "019f9a29-e722-7f9d-ac7e-3e401ee8e83b")


def test_resolves_opencode(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENCODE_SESSION_ID", "ses_abc123")
    assert session_env.current_session() == ("opencode", "ses_abc123")


def test_prefers_inner_agent_over_claude_code(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "outer-claude-id")
    monkeypatch.setenv("CODEX_THREAD_ID", "inner-codex-id")
    assert session_env.current_session() == ("codex", "inner-codex-id")
    _clear(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "outer-claude-id")
    monkeypatch.setenv("PI_SESSION_ID", "inner-pi-id")
    assert session_env.current_session() == ("pi", "inner-pi-id")


def test_returns_none_when_no_agent_is_identifiable(monkeypatch):
    _clear(monkeypatch)
    assert session_env.current_session() is None


def test_ignores_an_empty_variable(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
    assert session_env.current_session() is None


def test_ignores_whitespace_only(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CODEX_THREAD_ID", "   ")
    assert session_env.current_session() is None
