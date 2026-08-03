import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _registry(tmp, threads):
    p = tmp / "registry.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": "2026-07-01T00:00:00+00:00",
                "threads": threads,
                "tombstones": [],
                "projectsCleanedAt": {},
            }
        ),
        encoding="utf-8",
    )
    return p


def _thread(sid, **kw):
    base = {
        "sessionId": sid,
        "file": f"/nope/{sid}.jsonl",
        "mtime": "2026-07-01T00:00:00+00:00",
        "status": "active",
        "title": sid,
        "whatItIs": "w",
        "gitProjectLabel": "repo",
        "gitProjectKey": "repo-k",
        "project": "/p",
        "snoozedAt": None,
        "decision": None,
        "reviewedAtMtime": None,
    }
    base.update(kw)
    return base


def _run(reg, *args):
    env = dict(os.environ, SESSION_TRACKER_PROJECTS_DIR=str(reg.parent / "empty"))
    return subprocess.run(
        [sys.executable, "-m", "ringleader", "triage", "--registry", str(reg), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_triage_lists_only_undecided_with_no_scan(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("new"),
            _thread("hv", decision="harvested"),
        ],
    )
    r = _run(reg, "--no-scan")
    assert r.returncode == 0
    assert "new" in r.stdout
    assert "hv" not in r.stdout
    assert "triage inbox" in r.stdout


def test_triage_source_filter(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("cc", source="claude-code"),
            _thread("cx", source="codex"),
        ],
    )
    r = _run(reg, "--source", "codex", "--no-scan")
    assert r.returncode == 0
    assert "cx" in r.stdout
    assert "cc" not in r.stdout


def test_triage_all_triaged_message(tmp_path):
    reg = _registry(tmp_path, [_thread("hv", decision="harvested")])
    r = _run(reg, "--no-scan")
    assert r.returncode == 0 and "all triaged" in r.stdout.lower()


def _seed_mem(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / "-enc").mkdir(parents=True)
    (tmp_path / "-enc" / "s.jsonl").write_text(
        json.dumps({"type": "user", "cwd": str(proj), "message": {"role": "user", "content": "x"}})
        + "\n"
    )
    d = tmp_path / "-enc" / "memory"
    d.mkdir()
    (d / "a.md").write_text("durable note\n")
    return {**os.environ, "SESSION_TRACKER_PROJECTS_DIR": str(tmp_path)}


def test_triage_memory_lists_unreviewed(tmp_path):
    env = _seed_mem(tmp_path)
    reg = tmp_path / "registry.json"
    r = subprocess.run(
        [sys.executable, "-m", "ringleader", "mem", "triage", "--registry", str(reg)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "a.md" in r.stdout or "proj" in r.stdout


def test_triage_memory_empty_message(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    env = {**os.environ, "SESSION_TRACKER_PROJECTS_DIR": str(empty_dir)}
    reg = tmp_path / "registry.json"
    r = subprocess.run(
        [sys.executable, "-m", "ringleader", "mem", "triage", "--registry", str(reg)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "no memory awaiting review" in r.stdout
