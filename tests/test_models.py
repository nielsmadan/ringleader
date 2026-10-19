import json
import threading
import time
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

    monkeypatch.setattr("ringleader.models.proc.run_model_process", fake_run)
    out = ClaudeCliModel().complete_json("classify this", SCHEMA)
    assert out == {"x": "hi"}
    assert "claude" in seen["cmd"][0] or seen["cmd"][0] == "claude"
    assert "-p" in seen["cmd"]


def test_claude_complete_json_prefers_structured_output(monkeypatch):
    monkeypatch.setattr(
        "ringleader.models.proc.run_model_process",
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
        "ringleader.models.proc.run_model_process",
        lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(ModelError):
        ClaudeCliModel().complete_json("p", SCHEMA)


def test_claude_surfaces_the_envelope_reason_when_it_exits_nonzero(monkeypatch):
    # the CLI puts the reason in its stdout envelope and leaves stderr empty
    monkeypatch.setattr(
        "ringleader.models.proc.run_model_process",
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
        "ringleader.models.proc.run_model_process",
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
        "ringleader.models.proc.run_model_process",
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
        "ringleader.models.proc.run_model_process",
        lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="not json", stderr="boom"),
    )
    with pytest.raises(ModelError, match="boom"):
        ClaudeCliModel().complete_json("p", SCHEMA)


def test_codex_complete_json_parses_schema_output(monkeypatch):
    monkeypatch.setattr(
        "ringleader.models.proc.run_model_process",
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

    monkeypatch.setattr("ringleader.models.proc.run_model_process", fake_run)
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

    monkeypatch.setattr("ringleader.models.proc.run_model_process", fake_run)
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


def _cfg(**kw):
    from ringleader.config import Config

    return Config(**kw)


@pytest.mark.parametrize(
    ("cfg", "engine", "task", "expected"),
    [
        (_cfg(enrich_model="sonnet", model="opus"), "claude", "enrich", "sonnet"),
        (_cfg(), "claude", "enrich", "haiku"),
        (_cfg(model="gpt-x"), "codex", "enrich", "gpt-x"),
        (_cfg(), "codex", "enrich", None),
        (_cfg(enrich_model="sonnet", model="opus"), "claude", "harvest", "opus"),
        (_cfg(enrich_model="sonnet"), "claude", "harvest", None),
        (_cfg(enrich_model="sonnet", model="opus"), "claude", "analyze", "haiku"),
        (_cfg(analyze_model="sonnet", enrich_model="opus"), "claude", "analyze", "sonnet"),
        (_cfg(model="gpt-x"), "codex", "analyze", "gpt-x"),
        (_cfg(), "codex", "analyze", None),
    ],
)
def test_model_name_for(cfg, engine, task, expected):
    assert models.model_name_for(cfg, engine, task) == expected


def test_active_model_for_enrich_builds_haiku_with_timeout(monkeypatch):
    monkeypatch.setattr("ringleader.models.claude_cli.shutil.which", lambda n: "/bin/claude")
    m = models.active_model(_cfg(ai="claude"), "enrich")
    assert m == ClaudeCliModel(model="haiku", timeout=120.0)


def test_active_model_for_analyze_builds_haiku_with_its_own_timeout(monkeypatch):
    monkeypatch.setattr("ringleader.models.claude_cli.shutil.which", lambda n: "/bin/claude")
    m = models.active_model(_cfg(ai="claude"), "analyze")
    assert m == ClaudeCliModel(model="haiku", timeout=180.0)


def test_active_model_defaults_to_harvest_task(monkeypatch):
    monkeypatch.setattr("ringleader.models.claude_cli.shutil.which", lambda n: "/bin/claude")
    m = models.active_model(_cfg(ai="claude", enrich_model="sonnet"))
    assert m == ClaudeCliModel(model=None, timeout=300.0)


def test_autopick_resolves_the_per_candidate_enrich_default(monkeypatch):
    monkeypatch.setattr("ringleader.models.claude_cli.shutil.which", lambda n: "/bin/claude")
    m = models.active_model(_cfg(), "enrich")
    assert m == ClaudeCliModel(model="haiku", timeout=120.0)


def test_autopick_falls_back_to_codex_with_the_global_model(monkeypatch):
    monkeypatch.setattr(
        "ringleader.models.codex_cli.shutil.which",
        lambda n: "/bin/codex" if n == "codex" else None,
    )
    m = models.active_model(_cfg(model="gpt-x"), "enrich")
    assert m == CodexCliModel(model="gpt-x", timeout=120.0)


def test_with_cancel_returns_a_copy_carrying_the_check():
    def check() -> bool:
        return True

    original = ClaudeCliModel(model="haiku")
    copy = models.with_cancel(original, check)
    assert copy.cancelled is check
    assert original.cancelled is None


def test_describe_names_the_model_or_the_default():
    assert models.describe(ClaudeCliModel(model="haiku")) == "claude (haiku)"
    assert models.describe(CodexCliModel()) == "codex (default model)"


def test_claude_argv_carries_the_trim_flags_and_model():
    argv = ClaudeCliModel(model="haiku").argv(SCHEMA)
    assert argv[:4] == ["claude", "-p", "--output-format", "json"]
    for flag in (
        "--no-session-persistence",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-chrome",
    ):
        assert flag in argv
    assert argv[argv.index("--settings") + 1] == '{"disableAllHooks":true}'
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert argv[argv.index("--model") + 1] == "haiku"


def test_claude_call_sends_the_prompt_on_stdin_and_reads_stats(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        envelope = {
            "structured_output": {"x": "hi"},
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 2,
                "output_tokens": 7,
            },
            "total_cost_usd": 0.01,
        }
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(envelope), stderr="")

    monkeypatch.setattr("ringleader.models.proc.run_model_process", fake_run)
    out, stats = ClaudeCliModel(timeout=9.0).complete_json_with_stats("the prompt", SCHEMA)
    assert out == {"x": "hi"}
    assert (stats.input_tokens, stats.output_tokens, stats.cost_usd) == (17, 7, 0.01)
    assert stats.seconds >= 0
    assert seen["stdin_text"] == "the prompt"
    assert seen["timeout"] == 9.0


def test_claude_stats_are_none_when_the_envelope_has_no_usage(monkeypatch):
    monkeypatch.setattr(
        "ringleader.models.proc.run_model_process",
        lambda argv, **kw: types.SimpleNamespace(
            returncode=0, stdout=json.dumps({"result": json.dumps({"x": "hi"})}), stderr=""
        ),
    )
    _, stats = ClaudeCliModel().complete_json_with_stats("p", SCHEMA)
    assert (stats.input_tokens, stats.output_tokens, stats.cost_usd) == (None, None, None)


def test_codex_call_is_ephemeral_without_stdin(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen.update(kw)
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"x": "yo"}), stderr="")

    monkeypatch.setattr("ringleader.models.proc.run_model_process", fake_run)
    _, stats = CodexCliModel().complete_json_with_stats("p", SCHEMA)
    assert "--ephemeral" in seen["argv"] and "--ignore-rules" in seen["argv"]
    assert "--output-schema" not in CodexCliModel().argv(SCHEMA)
    assert seen["stdin_text"] is None
    assert (stats.input_tokens, stats.output_tokens, stats.cost_usd) == (None, None, None)


@pytest.mark.parametrize("cls", [ClaudeCliModel, CodexCliModel])
def test_cli_adapters_launch_through_the_command_prefix(monkeypatch, cls):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"x": "hi"}), stderr="")

    monkeypatch.setattr("ringleader.models.proc.run_model_process", fake_run)
    cls(command_prefix=("sops-exec", "--quiet")).complete_json("p", SCHEMA)

    assert seen["cmd"][:3] == ["sops-exec", "--quiet", cls().name]


@pytest.mark.parametrize("cls", [ClaudeCliModel, CodexCliModel])
def test_cli_adapter_needs_the_prefix_binary_on_path(monkeypatch, cls):
    binary = cls().name
    monkeypatch.setattr(
        "ringleader.models.claude_cli.shutil.which",
        lambda n: f"/bin/{n}" if n == binary else None,
    )

    assert cls().available() is True
    assert cls(command_prefix=("sops-exec",)).available() is False


def test_active_model_launches_through_the_configured_prefix(monkeypatch):
    monkeypatch.setattr("ringleader.models.claude_cli.shutil.which", lambda n: f"/bin/{n}")
    cfg = _cfg(ai_command_prefix=["sops-exec"])

    assert models.active_model(cfg, "enrich") == ClaudeCliModel(
        model="haiku", timeout=120.0, command_prefix=("sops-exec",)
    )
    assert models.active_model(_cfg(ai="codex", ai_command_prefix=["sops-exec"])) == (
        CodexCliModel(timeout=300.0, command_prefix=("sops-exec",))
    )


def test_describe_names_the_command_prefix():
    model = ClaudeCliModel(model="haiku", command_prefix=("env", "A=b c"))

    assert models.describe(model) == "claude (haiku) via env 'A=b c'"


def test_a_cancelled_llm_call_returns_before_the_request_finishes(monkeypatch):
    release = threading.Event()

    def slow(self, prompt, schema):
        release.wait(5)
        return {}

    monkeypatch.setattr(LlmModel, "_complete", slow)
    model = models.with_cancel(LlmModel(), lambda: True)
    started = time.monotonic()
    try:
        with pytest.raises(ModelError, match="cancelled"):
            model.complete_json("p", {})
    finally:
        release.set()
    assert time.monotonic() - started < 2


def test_an_llm_call_that_is_not_cancelled_returns_its_result(monkeypatch):
    monkeypatch.setattr(LlmModel, "_complete", lambda self, prompt, schema: {"ok": prompt})
    model = models.with_cancel(LlmModel(), lambda: False)
    assert model.complete_json("p", {}) == {"ok": "p"}
