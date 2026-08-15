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
    assert "backend: None" in r.stdout
    assert "api.provider: anthropic" in r.stdout


def test_config_set_and_show_roundtrip(tmp_path):
    env = _env(tmp_path)

    r = _run("config", "set", "backend", "api", env=env)
    assert r.returncode == 0, r.stderr
    r = _run("config", "set", "api.provider", "openai", env=env)
    assert r.returncode == 0, r.stderr

    r = _run("config", "show", env=env)

    assert r.returncode == 0, r.stderr
    assert "backend: api" in r.stdout
    assert "api.provider: openai" in r.stdout

    cfg_path = tmp_path / "ringleader" / "config.json"
    assert cfg_path.exists()
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data == {
        "backend": "api",
        "model": None,
        "api_provider": "openai",
        "roundup_stale_hours": 8,
    }


def test_config_set_model(tmp_path):
    env = _env(tmp_path)

    r = _run("config", "set", "model", "gpt-x", env=env)

    assert r.returncode == 0, r.stderr
    r = _run("config", "show", env=env)
    assert "model: gpt-x" in r.stdout


def test_config_set_invalid_backend_rejected(tmp_path):
    env = _env(tmp_path)

    r = _run("config", "set", "backend", "bogus", env=env)

    assert r.returncode == 2
    assert "bogus" in r.stderr
    cfg_path = tmp_path / "ringleader" / "config.json"
    assert not cfg_path.exists()


def test_config_set_unknown_key_rejected(tmp_path):
    env = _env(tmp_path)

    r = _run("config", "set", "nope", "value", env=env)

    assert r.returncode == 2
    assert "nope" in r.stderr


def test_config_show_reports_no_active_model_for_api_backend(tmp_path):
    # `llm` is an optional extra (see pyproject.toml `api` group) and is not
    # installed in the test env, so ApiModel.available() is reliably False —
    # this exercises cmd_config's "no active model" branch deterministically.
    env = _env(tmp_path)
    r = _run("config", "set", "backend", "api", env=env)
    assert r.returncode == 0, r.stderr

    r = _run("config", "show", env=env)

    assert r.returncode == 0, r.stderr
    assert "none" in r.stdout.lower()
