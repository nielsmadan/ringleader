"""End-to-end CLI tests for `rl session refresh` / `rl session set`.

These guard against the --registry-unreachable regression: cmd_refresh/cmd_set must
give --registry to the underlying command, not just accept and drop it.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from ringleader.commands import sync as sync_mod

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
    empty_pi = tmp_path / "empty-pi"
    empty_pi.mkdir()
    reg = tmp_path / "registry.json"
    env = {
        **os.environ,
        "SESSION_TRACKER_PROJECTS_DIR": str(empty_projects),
        "CODEX_SESSIONS_DIR": str(empty_codex),
        "XDG_DATA_HOME": str(tmp_path / "xdg"),
        "PI_SESSIONS_DIR": str(empty_pi),
    }

    r = _run("session", "refresh", "--registry", str(reg), env=env)

    assert r.returncode == 0, r.stderr
    assert '"pendingCount"' in r.stdout
    payload = json.loads(r.stdout)
    assert payload == {"pendingCount": 0, "total": 0, "pending": []}
    assert reg.exists()


def test_session_refresh_project_filter_is_reachable(tmp_path):
    empty_projects = tmp_path / "empty"
    empty_projects.mkdir()
    empty_codex = tmp_path / "empty-codex"
    empty_codex.mkdir()
    empty_pi = tmp_path / "empty-pi"
    empty_pi.mkdir()
    reg = tmp_path / "registry.json"
    env = {
        **os.environ,
        "SESSION_TRACKER_PROJECTS_DIR": str(empty_projects),
        "CODEX_SESSIONS_DIR": str(empty_codex),
        "XDG_DATA_HOME": str(tmp_path / "xdg"),
        "PI_SESSIONS_DIR": str(empty_pi),
    }

    r = _run("session", "refresh", "--registry", str(reg), "--project", "nope", env=env)

    assert r.returncode == 0, r.stderr
    assert '"pendingCount"' in r.stdout


def test_session_refresh_keeps_stdout_pure_json_when_sweeping(tmp_path):
    projects = tmp_path / "projects" / "-w-x"
    projects.mkdir(parents=True)
    transcript = projects / "s1.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"hi"}}\n')
    when = time.time() - 600
    os.utime(transcript, (when, when))
    empty_codex = tmp_path / "empty-codex"
    empty_codex.mkdir()

    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "archived": [],
                "deleted": [],
                "threads": [
                    {
                        "sessionId": "s1",
                        "file": str(transcript),
                        "project": "/w/x",
                        "source": "claude-code",
                        "decision": "harvested",
                        "finishOnExit": True,
                        "gitProjectLabel": None,
                        "gitBranch": None,
                        "title": None,
                        "aiTitle": None,
                        "lastPrompt": None,
                        "messageCount": 0,
                    }
                ],
            }
        )
    )
    env = {
        **os.environ,
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "SESSION_TRACKER_PROJECTS_DIR": str(tmp_path / "projects"),
        "CODEX_SESSIONS_DIR": str(empty_codex),
        "PI_SESSIONS_DIR": str(tmp_path / "empty-pi"),
    }

    r = _run("session", "refresh", "--registry", str(reg), env=env)

    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)  # fails if the sweep's note leaked onto stdout
    assert "pendingCount" in payload
    assert "archived s1" in r.stderr

    out = json.loads(reg.read_text())
    assert [a["sessionId"] for a in out["archived"]] == ["s1"]
    assert not transcript.exists()


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
    thread = json.loads(reg.read_text())["threads"][0]
    assert thread["status"] == "active"
    assert thread["whatItIs"] == "x"
    assert thread["suggestedName"] == "x"


def test_session_scan_omits_pending_threads_from_an_unavailable_source(
    tmp_path, monkeypatch, capsys
):
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "lastScan": None,
                "archived": [],
                "deleted": [],
                "threads": [
                    {
                        "source": "opencode",
                        "sessionId": "ses_1",
                        "file": None,
                        "mtime": "2026-08-30T00:00:00+00:00",
                        "status": "unclassified",
                    }
                ],
            }
        )
    )

    def unavailable_sources(*, unavailable_sources):
        unavailable_sources.add("opencode")
        return []

    monkeypatch.setattr(sync_mod.scanner, "active_sources", unavailable_sources)
    monkeypatch.setattr(sync_mod.scanner, "scan", lambda **kwargs: [])

    sync_mod.cmd_scan(SimpleNamespace(registry=str(reg), project=None))

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"pendingCount": 0, "total": 1, "pending": []}
    assert [thread["sessionId"] for thread in json.loads(reg.read_text())["threads"]] == ["ses_1"]
