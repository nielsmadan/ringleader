"""End-to-end CLI tests for `rl config show` / `rl config set`.

Every test isolates the config file via XDG_DATA_HOME so the real
~/.local/share/ringleader/config.json is never touched.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

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
        "llm_provider": "openai",
        "roundup_stale_hours": 8,
        "archive_retention_days": 0,
        "session_filters": [],
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
