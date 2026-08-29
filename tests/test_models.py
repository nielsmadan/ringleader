import json
import types
from pathlib import Path

import pytest
from ringleader import models
from ringleader.models.base import Model, ModelError
from ringleader.models.claude_cli import ClaudeCliModel
from ringleader.models.codex_cli import CodexCliModel
from ringleader.models.llm import LlmModel

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
NESTED_SCHEMA = {
    "type": "object",
    "properties": {
        "x": {"type": "string"},
        "y": {
            "type": "object",
            "properties": {"z": {"type": "string"}},
            "required": ["z"],
        },
    },
    "required": ["x", "y"],
}


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


def test_claude_complete_json_prefers_structured_output(monkeypatch):
    monkeypatch.setattr(
        "ringleader.models.claude_cli.subprocess.run",
        lambda cmd, **kw: types.SimpleNamespace(
            returncode=0,
            # "result" deliberately disagrees with structured_output to prove precedence
            stdout=json.dumps(
                {"result": json.dumps({"x": "wrong"}), "structured_output": {"x": "hi"}}
            ),
            stderr="",
        ),
    )
    assert ClaudeCliModel().complete_json("p", SCHEMA) == {"x": "hi"}


def test_claude_raises_modelerror_on_nonzero(monkeypatch):
    monkeypatch.setattr(
        "ringleader.models.claude_cli.subprocess.run",
        lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(ModelError):
        ClaudeCliModel().complete_json("p", SCHEMA)


def test_claude_surfaces_the_envelope_reason_when_it_exits_nonzero(monkeypatch):
    # the CLI puts the reason in its stdout envelope and leaves stderr empty
    monkeypatch.setattr(
        "ringleader.models.claude_cli.subprocess.run",
        lambda cmd, **kw: types.SimpleNamespace(
            returncode=1,
            stdout=json.dumps(
                {
                    "is_error": True,
                    "result": "Not logged in · Please run /login",
                    "subtype": "success",
                }
            ),
            stderr="",
        ),
    )
    with pytest.raises(ModelError, match="Not logged in"):
        ClaudeCliModel().complete_json("p", SCHEMA)


def test_claude_surfaces_the_envelope_reason_when_it_exits_zero(monkeypatch):
    # an API-side failure still exits 0; only is_error in the envelope marks it
    monkeypatch.setattr(
        "ringleader.models.claude_cli.subprocess.run",
        lambda cmd, **kw: types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"is_error": True, "result": "Credit balance is too low", "api_error_status": 400}
            ),
            stderr="",
        ),
    )
    with pytest.raises(ModelError, match="Credit balance is too low"):
        ClaudeCliModel().complete_json("p", SCHEMA)


def test_claude_reports_api_status_alongside_the_reason(monkeypatch):
    monkeypatch.setattr(
        "ringleader.models.claude_cli.subprocess.run",
        lambda cmd, **kw: types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"is_error": True, "result": "Credit balance is too low", "api_error_status": 400}
            ),
            stderr="",
        ),
    )
    with pytest.raises(ModelError, match="400"):
        ClaudeCliModel().complete_json("p", SCHEMA)


def test_claude_falls_back_to_stderr_when_stdout_has_no_envelope(monkeypatch):
    monkeypatch.setattr(
        "ringleader.models.claude_cli.subprocess.run",
        lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="not json", stderr="boom"),
    )
    with pytest.raises(ModelError, match="boom"):
        ClaudeCliModel().complete_json("p", SCHEMA)


def test_codex_complete_json_parses_schema_output(monkeypatch):
    monkeypatch.setattr(
        "ringleader.models.codex_cli.subprocess.run",
        lambda cmd, **kw: types.SimpleNamespace(
            returncode=0, stdout=json.dumps({"x": "yo"}), stderr=""
        ),
    )
    assert CodexCliModel().complete_json("p", SCHEMA) == {"x": "yo"}


def test_codex_complete_json_builds_command(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        schema_path = cmd[cmd.index("--output-schema") + 1]
        seen["schema"] = json.loads(Path(schema_path).read_text())
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"x": "yo"}), stderr="")

    monkeypatch.setattr("ringleader.models.codex_cli.subprocess.run", fake_run)
    CodexCliModel().complete_json("classify this", SCHEMA)

    cmd = seen["cmd"]
    assert cmd[0] == "codex"
    assert cmd[1] == "exec"
    assert "--output-schema" in cmd
    assert "--skip-git-repo-check" in cmd
    assert "--sandbox" in cmd and "read-only" in cmd
    assert "classify this" in cmd
    assert seen["schema"]["additionalProperties"] is False


def test_codex_injects_additional_properties_recursively(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        schema_path = cmd[cmd.index("--output-schema") + 1]
        seen["schema"] = json.loads(Path(schema_path).read_text())
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"x": "yo"}), stderr="")

    monkeypatch.setattr("ringleader.models.codex_cli.subprocess.run", fake_run)
    CodexCliModel().complete_json("p", NESTED_SCHEMA)

    schema = seen["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["y"]["additionalProperties"] is False


def test_active_model_autopicks_first_available_agent(monkeypatch):
    monkeypatch.setattr("ringleader.models.claude_cli.shutil.which", lambda n: None)
    monkeypatch.setattr("ringleader.models.codex_cli.shutil.which", lambda n: "/bin/codex")
    from ringleader.config import Config

    m = models.active_model(Config())  # no explicit engine
    assert m is not None and m.name == "codex"


def test_active_model_none_when_nothing_available(monkeypatch):
    monkeypatch.setattr("ringleader.models.claude_cli.shutil.which", lambda n: None)
    monkeypatch.setattr("ringleader.models.codex_cli.shutil.which", lambda n: None)
    from ringleader.config import Config

    assert models.active_model(Config()) is None


def test_active_model_explicit_engine_unavailable_fails_closed(monkeypatch):
    # An explicit engine that isn't available must return None, not silently
    # fall back to auto-picking a different engine.
    monkeypatch.setattr("ringleader.models.claude_cli.shutil.which", lambda n: None)
    from ringleader.config import Config

    assert models.active_model(Config(ai="claude")) is None


def test_llm_missing_library_is_unavailable_and_raises(monkeypatch):
    def raise_modelerror(self):
        raise ModelError("the llm engine needs `pip install llm`")

    monkeypatch.setattr(LlmModel, "_llm", raise_modelerror)
    m = LlmModel()
    assert m.available() is False
    with pytest.raises(ModelError):
        m.complete_json("p", SCHEMA)


def test_llm_complete_json_success(monkeypatch):
    captured = {}

    class FakeResponse:
        def __init__(self, text):
            self._text = text

        def text(self):
            return self._text

    class FakeLlmModel:
        def prompt(self, prompt, schema=None):
            captured["prompt"] = prompt
            captured["schema"] = schema
            return FakeResponse(json.dumps({"x": "hi"}))

    class FakeLlm:
        def get_model(self, name):
            captured["model_name"] = name
            return FakeLlmModel()

    monkeypatch.setattr(LlmModel, "_llm", lambda self: FakeLlm())
    out = LlmModel().complete_json("classify this", SCHEMA)
    assert out == {"x": "hi"}
    assert captured["prompt"] == "classify this"
    assert captured["schema"] == SCHEMA


def test_llm_complete_json_wraps_provider_failure(monkeypatch):
    class FakeLlm:
        def get_model(self, name):
            raise RuntimeError("provider boom")

    monkeypatch.setattr(LlmModel, "_llm", lambda self: FakeLlm())
    with pytest.raises(ModelError):
        LlmModel().complete_json("p", SCHEMA)
