"""End-to-end CLI tests for `rl config show` / `rl config set`.

Every test isolates the config file via XDG_DATA_HOME so the real
~/.local/share/ringleader/config.json is never touched.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from ringleader.config import key_preset

REPO = Path(__file__).resolve().parent.parent


def _run(*args, env):
    return subprocess.run(
        [sys.executable, "-m", "ringleader", *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _env(tmp_path):
    return {**os.environ, "XDG_DATA_HOME": str(tmp_path)}


def test_config_show_defaults(tmp_path):
    env = _env(tmp_path)

    r = _run("config", "show", env=env)

    assert r.returncode == 0, r.stderr
    assert "ai: None" in r.stdout
    assert "llm.provider: anthropic" in r.stdout
    assert "archive.retention_days: 0" in r.stdout
    assert "memory.recovery_retention_days: 30" in r.stdout
    assert "memory.codex_sqlite_home: default" in r.stdout
    assert "memory.claude_trust_file: default" in r.stdout
    assert "inbox.keys: vim" in r.stdout


def test_config_set_and_show_roundtrip(tmp_path):
    env = _env(tmp_path)

    r = _run("config", "set", "ai", "llm", env=env)
    assert r.returncode == 0, r.stderr
    r = _run("config", "set", "llm.provider", "openai", env=env)
    assert r.returncode == 0, r.stderr

    r = _run("config", "show", env=env)

    assert r.returncode == 0, r.stderr
    assert "ai: llm" in r.stdout
    assert "llm.provider: openai" in r.stdout

    cfg_path = tmp_path / "ringleader" / "config.json"
    assert cfg_path.exists()
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data == {
        "ai": "llm",
        "model": None,
        "ai_command_prefix": [],
        "enrich_model": None,
        "analyze_model": None,
        "llm_provider": "openai",
        "roundup_stale_hours": 8,
        "archive_retention_days": 0,
        "memory_recovery_retention_days": 30,
        "memory_codex_sqlite_home": None,
        "memory_claude_trust_file": None,
        "session_filters": [],
        "inbox_keys": "vim",
        "usage_claude_auth": "env",
        "usage_codex_auth": "disabled",
        "usage_cache_seconds": 60,
    }


def test_config_set_model(tmp_path):
    env = _env(tmp_path)

    r = _run("config", "set", "model", "gpt-x", env=env)

    assert r.returncode == 0, r.stderr
    r = _run("config", "show", env=env)
    assert "model: gpt-x" in r.stdout


def test_config_set_invalid_engine_rejected(tmp_path):
    env = _env(tmp_path)

    r = _run("config", "set", "ai", "bogus", env=env)

    assert r.returncode == 2
    assert "bogus" in r.stderr
    cfg_path = tmp_path / "ringleader" / "config.json"
    assert not cfg_path.exists()


def test_config_set_archive_retention_roundtrip(tmp_path):
    env = _env(tmp_path)

    r = _run("config", "set", "archive.retention_days", "30", env=env)
    assert r.returncode == 0, r.stderr

    r = _run("config", "show", env=env)
    assert "archive.retention_days: 30" in r.stdout
    data = json.loads((tmp_path / "ringleader" / "config.json").read_text(encoding="utf-8"))
    assert data["archive_retention_days"] == 30


def test_config_set_invalid_archive_retention_rejected(tmp_path):
    env = _env(tmp_path)

    for bad in ("banana", "-1"):
        r = _run("config", "set", "archive.retention_days", bad, env=env)
        assert r.returncode == 2, bad
        assert bad in r.stderr
    assert not (tmp_path / "ringleader" / "config.json").exists()


def test_config_memory_retention_can_expire_immediately_and_archive_can_keep_forever(tmp_path):
    env = _env(tmp_path)
    for key in ("archive.retention_days", "memory.recovery_retention_days"):
        r = _run("config", "set", key, "0", env=env)
        assert r.returncode == 0, r.stderr
    r = _run("config", "show", env=env)
    assert "archive.retention_days: 0" in r.stdout
    assert "memory.recovery_retention_days: 0" in r.stdout
    for bad in ("-1", "1.5", "never"):
        r = _run("config", "set", "memory.recovery_retention_days", bad, env=env)
        assert r.returncode == 2 and "0 = expire on completed removal" in r.stderr


@pytest.mark.parametrize("key", ["memory.codex_sqlite_home", "memory.claude_trust_file"])
def test_config_memory_paths_accept_absolute_override_and_default(tmp_path, key):
    env = _env(tmp_path)
    for bad in ("relative/path", str(tmp_path) + "/../other"):
        r = _run("config", "set", key, bad, env=env)
        assert r.returncode == 2 and "absolute path without traversal" in r.stderr
    override = str(tmp_path / "chosen path")
    for value in (override, "default"):
        r = _run("config", "set", key, value, env=env)
        assert r.returncode == 0, r.stderr
        r = _run("config", "show", env=env)
        assert f"{key}: {value}" in r.stdout


def test_config_set_unknown_key_rejected(tmp_path):
    env = _env(tmp_path)

    r = _run("config", "set", "nope", "value", env=env)

    assert r.returncode == 2
    assert "nope" in r.stderr


def test_config_show_reports_no_active_model_for_llm_engine(tmp_path):
    # The `llm` library is not a dependency and is not installed in the test
    # env, so LlmModel.available() is reliably False — this exercises
    # cmd_config's "no active model" branch deterministically.
    env = _env(tmp_path)
    r = _run("config", "set", "ai", "llm", env=env)
    assert r.returncode == 0, r.stderr

    r = _run("config", "show", env=env)

    assert r.returncode == 0, r.stderr
    assert "none" in r.stdout.lower()


def test_config_filter_add_list_remove_roundtrip(tmp_path):
    env = _env(tmp_path)

    r = _run("config", "filter", "add", "[cadet-city]*", env=env)
    assert r.returncode == 0, r.stderr
    assert "[cadet-city]*" in r.stdout

    r = _run("config", "filter", "list", env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[cadet-city]*"

    data = json.loads((tmp_path / "ringleader" / "config.json").read_text(encoding="utf-8"))
    assert data["session_filters"] == ["[cadet-city]*"]

    r = _run("config", "filter", "remove", "[cadet-city]*", env=env)
    assert r.returncode == 0, r.stderr

    r = _run("config", "filter", "list", env=env)
    assert "no session filters" in r.stdout
    data = json.loads((tmp_path / "ringleader" / "config.json").read_text(encoding="utf-8"))
    assert data["session_filters"] == []


def test_config_filter_shows_in_config_show(tmp_path):
    env = _env(tmp_path)
    _run("config", "filter", "add", "[x]*", env=env)

    r = _run("config", "show", env=env)

    assert "session.filters: [x]*" in r.stdout


def test_config_filter_rejects_duplicate_and_absent_patterns(tmp_path):
    env = _env(tmp_path)
    _run("config", "filter", "add", "[x]*", env=env)

    r = _run("config", "filter", "add", "[x]*", env=env)
    assert r.returncode == 2
    assert "already a filter" in r.stderr

    r = _run("config", "filter", "remove", "[nope]*", env=env)
    assert r.returncode == 2
    assert "not a filter" in r.stderr

    data = json.loads((tmp_path / "ringleader" / "config.json").read_text(encoding="utf-8"))
    assert data["session_filters"] == ["[x]*"]


def test_config_filter_requires_a_pattern(tmp_path):
    env = _env(tmp_path)

    r = _run("config", "filter", "add", env=env)

    assert r.returncode == 2
    assert "usage: rl config filter add" in r.stderr


def test_config_set_enrich_model_persists_and_default_clears(tmp_path):
    env = _env(tmp_path)

    assert _run("config", "set", "enrich.model", "sonnet", env=env).returncode == 0
    shown = _run("config", "show", env=env)
    assert "enrich.model: sonnet" in shown.stdout

    assert _run("config", "set", "enrich.model", "default", env=env).returncode == 0
    shown = _run("config", "show", env=env)
    assert "enrich.model: default" in shown.stdout


def test_config_set_analyze_model_persists_and_default_clears(tmp_path):
    env = _env(tmp_path)
    assert _run("config", "set", "analyze.model", "sonnet", env=env).returncode == 0
    shown = _run("config", "show", env=env)
    assert "analyze.model: sonnet" in shown.stdout
    data = json.loads((tmp_path / "ringleader" / "config.json").read_text(encoding="utf-8"))
    assert data["analyze_model"] == "sonnet"
    assert _run("config", "set", "analyze.model", "default", env=env).returncode == 0
    assert "analyze.model: default" in _run("config", "show", env=env).stdout


def test_config_show_prints_both_model_lines(tmp_path):
    env = _env(tmp_path)
    assert _run("config", "set", "ai", "llm", env=env).returncode == 0

    out = _run("config", "show", env=env).stdout

    assert "enrich model: none" in out
    assert "harvest model: none" in out
    assert "analyze model: none" in out


def test_config_show_describes_the_resolved_model_per_task(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    env = {**_env(tmp_path), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    assert _run("config", "set", "ai", "claude", env=env).returncode == 0

    out = _run("config", "show", env=env).stdout

    assert "enrich model: claude (haiku)" in out
    assert "harvest model: claude (default model)" in out
    assert "analyze model: claude (haiku)" in out


def test_config_set_ai_command_prefix_splits_and_none_clears(tmp_path):
    env = _env(tmp_path)

    assert _run("config", "set", "ai.command_prefix", "env 'A=b c'", env=env).returncode == 0
    shown = _run("config", "show", env=env).stdout
    data = json.loads((tmp_path / "ringleader" / "config.json").read_text(encoding="utf-8"))
    assert "ai.command_prefix: env 'A=b c'" in shown
    assert data["ai_command_prefix"] == ["env", "A=b c"]

    assert _run("config", "set", "ai.command_prefix", "none", env=env).returncode == 0
    assert "ai.command_prefix: none" in _run("config", "show", env=env).stdout


def test_config_set_ai_command_prefix_rejects_unbalanced_quotes(tmp_path):
    r = _run("config", "set", "ai.command_prefix", "sops-exec 'x", env=_env(tmp_path))

    assert r.returncode == 2
    assert "rl config set ai.command_prefix: No closing quotation" in r.stderr


def test_config_set_inbox_keys_roundtrip(tmp_path):
    env = _env(tmp_path)

    r = _run("config", "set", "inbox.keys", "circus", env=env)

    assert r.returncode == 0, r.stderr
    assert "inbox.keys: circus" in _run("config", "show", env=env).stdout


def test_config_set_invalid_inbox_keys_rejected(tmp_path):
    env = _env(tmp_path)

    r = _run("config", "set", "inbox.keys", "emacs", env=env)

    assert r.returncode == 2
    assert "rl config set: inbox.keys is 'emacs'; it must be one of vim, circus" in r.stderr


@pytest.mark.parametrize("value", ["emacs", ["circus"], None])
def test_key_preset_names_the_setting_for_any_bad_value(value):
    with pytest.raises(ValueError) as raised:
        key_preset(value)
    assert str(raised.value) == f"inbox.keys is {value!r}; it must be one of vim, circus"


@pytest.mark.parametrize("stored", ["emacs", ["circus"]])
def test_rl_inbox_refuses_a_stored_preset_it_does_not_know(tmp_path, stored):
    env = _env(tmp_path)
    config_file = tmp_path / "ringleader" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(json.dumps({"inbox_keys": stored}))

    r = _run("inbox", "--registry", str(tmp_path / "registry.json"), env=env)

    assert r.returncode == 2
    assert r.stderr == (
        f"rl inbox: inbox.keys is {stored!r}; it must be one of vim, circus"
        " — set it with rl config set inbox.keys\n"
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("usage.claude_auth", "agent"),
        ("usage.codex_auth", "agent"),
        ("usage.cache_seconds", "3600"),
    ],
)
def test_config_usage_preferences_roundtrip_and_reset(tmp_path, key, value):
    env = _env(tmp_path)
    assert _run("config", "set", key, value, env=env).returncode == 0
    assert f"{key}: {value}" in _run("config", "show", env=env).stdout
    assert _run("config", "set", key, "default", env=env).returncode == 0
    defaults = {
        "usage.claude_auth": "env",
        "usage.codex_auth": "disabled",
        "usage.cache_seconds": "60",
    }
    assert f"{key}: {defaults[key]}" in _run("config", "show", env=env).stdout


@pytest.mark.parametrize(
    "key,value",
    [
        ("usage.claude_auth", "automatic"),
        ("usage.codex_auth", "env"),
        ("usage.cache_seconds", "true"),
        ("usage.cache_seconds", "60.0"),
        ("usage.cache_seconds", "-1"),
        ("usage.cache_seconds", "3601"),
    ],
)
def test_config_usage_rejects_invalid_preferences(tmp_path, key, value):
    env = _env(tmp_path)
    result = _run("config", "set", key, value, env=env)
    assert result.returncode == 2 and key in result.stderr
    assert not (tmp_path / "ringleader" / "config.json").exists()


def test_invalid_usage_config_does_not_break_offline_local_and_show_is_safe(tmp_path):
    env = _env(tmp_path)
    config_file = tmp_path / "ringleader" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(
        json.dumps(
            {"usage_cache_seconds": "fake-secret", "usage_claude_auth": {"token": "fake-secret"}}
        )
    )
    shown = _run("config", "show", env=env)
    assert shown.returncode == 0
    assert "usage.cache_seconds: invalid" in shown.stdout
    assert "fake-secret" not in shown.stdout
    local = _run("usage", "local", "--json", env=env)
    assert local.returncode == 0
    assert json.loads(local.stdout)["total_tokens"] == 0
