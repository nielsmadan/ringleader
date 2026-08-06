import json
import types

import pytest
from ringleader import models
from ringleader.models.base import Model, ModelError
from ringleader.models.claude_cli import ClaudeCliModel
from ringleader.models.codex_cli import CodexCliModel

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}


def test_backends_satisfy_protocol():
    assert isinstance(ClaudeCliModel(), Model)
    assert isinstance(CodexCliModel(), Model)


def test_claude_available_follows_which(monkeypatch):
    monkeypatch.setattr("ringleader.models.claude_cli.shutil.which", lambda n: "/bin/claude")
    assert ClaudeCliModel().available() is True
    monkeypatch.setattr("ringleader.models.claude_cli.shutil.which", lambda n: None)
    assert ClaudeCliModel().available() is False


def test_claude_complete_json_builds_command_and_parses_result(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        # claude --output-format json wraps the answer in a {"result": "..."} envelope
        return types.SimpleNamespace(
            returncode=0, stdout=json.dumps({"result": json.dumps({"x": "hi"})}), stderr=""
        )

    monkeypatch.setattr("ringleader.models.claude_cli.subprocess.run", fake_run)
    out = ClaudeCliModel().complete_json("classify this", SCHEMA)
    assert out == {"x": "hi"}
    assert "claude" in seen["cmd"][0] or seen["cmd"][0] == "claude"
    assert "-p" in seen["cmd"]


def test_claude_raises_modelerror_on_nonzero(monkeypatch):
    monkeypatch.setattr(
        "ringleader.models.claude_cli.subprocess.run",
        lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(ModelError):
        ClaudeCliModel().complete_json("p", SCHEMA)


def test_codex_complete_json_parses_schema_output(monkeypatch):
    monkeypatch.setattr(
        "ringleader.models.codex_cli.subprocess.run",
        lambda cmd, **kw: types.SimpleNamespace(
            returncode=0, stdout=json.dumps({"x": "yo"}), stderr=""
        ),
    )
    assert CodexCliModel().complete_json("p", SCHEMA) == {"x": "yo"}


def test_active_model_autopicks_first_available_agent(monkeypatch):
    monkeypatch.setattr("ringleader.models.claude_cli.shutil.which", lambda n: None)
    monkeypatch.setattr("ringleader.models.codex_cli.shutil.which", lambda n: "/bin/codex")
    from ringleader.config import Config

    m = models.active_model(Config())  # no explicit backend
    assert m is not None and m.name == "codex"


def test_active_model_none_when_nothing_available(monkeypatch):
    monkeypatch.setattr("ringleader.models.claude_cli.shutil.which", lambda n: None)
    monkeypatch.setattr("ringleader.models.codex_cli.shutil.which", lambda n: None)
    from ringleader.config import Config

    assert models.active_model(Config()) is None
