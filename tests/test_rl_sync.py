"""End-to-end CLI tests for `rl session refresh` / `rl session set`.

These guard against the --registry-unreachable regression: cmd_refresh/cmd_set must
give --registry to the underlying command, not just accept and drop it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "ringleader", *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_session_refresh_honors_registry_and_emits_pending(tmp_path):
    empty_projects = tmp_path / "empty"
    empty_projects.mkdir()
    empty_codex = tmp_path / "empty-codex"
    empty_codex.mkdir()
    reg = tmp_path / "registry.json"
    env = {
        **os.environ,
        "SESSION_TRACKER_PROJECTS_DIR": str(empty_projects),
        "CODEX_SESSIONS_DIR": str(empty_codex),
    }

    r = _run("session", "refresh", "--registry", str(reg), env=env)

    assert r.returncode == 0, r.stderr
    assert '"pendingCount"' in r.stdout
    payload = json.loads(r.stdout)
    assert payload == {"pendingCount": 0, "total": 0, "pending": []}
    # --registry actually reached cmd_scan: it wrote the tmp registry, not the default.
    assert reg.exists()


def test_session_refresh_project_filter_is_reachable(tmp_path):
    empty_projects = tmp_path / "empty"
    empty_projects.mkdir()
    empty_codex = tmp_path / "empty-codex"
    empty_codex.mkdir()
    reg = tmp_path / "registry.json"
    env = {
        **os.environ,
        "SESSION_TRACKER_PROJECTS_DIR": str(empty_projects),
        "CODEX_SESSIONS_DIR": str(empty_codex),
    }

    r = _run("session", "refresh", "--registry", str(reg), "--project", "nope", env=env)

    assert r.returncode == 0, r.stderr
    assert '"pendingCount"' in r.stdout


def test_session_set_honors_registry_and_applies_classification(tmp_path):
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": None,
                "tombstones": [],
                "threads": [
                    {
                        "sessionId": "id1",
                        "file": "x",
                        "mtime": "2026-06-01T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    r = _run(
        "session",
        "set",
        "--registry",
        str(reg),
        "--session",
        "id1",
        "--status",
        "active",
        "--what",
        "x",
        "--name",
        "x",
    )

    assert r.returncode == 0, r.stderr
    assert "applied id1" in r.stdout
    # --registry actually reached cmd_apply: the tmp registry was updated in place.
    thread = json.loads(reg.read_text())["threads"][0]
    assert thread["status"] == "active"
    assert thread["whatItIs"] == "x"
    assert thread["suggestedName"] == "x"
