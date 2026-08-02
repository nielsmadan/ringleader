import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _thread(sid, **kw):
    base = {
        "sessionId": sid,
        "file": f"/nope/{sid}.jsonl",
        "mtime": "2026-06-20T00:00:00+00:00",
        "status": "active",
        "title": sid,
        "whatItIs": "does " + sid,
        "gitProjectLabel": "repo",
        "gitProjectKey": "repo-k",
        "project": "/p",
        "gitBranch": None,
        "classifiedAtMtime": "2026-06-20T00:00:00+00:00",
        "snoozedAt": None,
        "decision": None,
        "reviewedAtMtime": None,
    }
    base.update(kw)
    return base


def _registry(tmp, threads, tombstones=None):
    p = tmp / "registry.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": "2026-07-01T00:00:00+00:00",
                "threads": threads,
                "tombstones": tombstones or [],
                "projectsCleanedAt": {},
            }
        ),
        encoding="utf-8",
    )
    return p


def _run(reg, *args):
    env = dict(os.environ, SESSION_TRACKER_PROJECTS_DIR=str(reg.parent / "empty"))
    return subprocess.run(
        [sys.executable, "-m", "ringleader", "overview", "--registry", str(reg), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _run_mem(reg, *args):
    env = dict(os.environ, SESSION_TRACKER_PROJECTS_DIR=str(reg.parent / "empty"))
    return subprocess.run(
        [sys.executable, "-m", "ringleader", "mem", "overview", "--registry", str(reg), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_default_is_rollup(tmp_path):
    reg = _registry(tmp_path, [_thread("a")])
    r = _run(reg, "--no-scan")
    assert r.returncode == 0
    assert "PROJECT" in r.stdout and "projects/dirs" in r.stdout


def test_sessions_flag_lists_threads(tmp_path):
    reg = _registry(tmp_path, [_thread("a")])
    r = _run(reg, "-s", "--no-scan")
    assert r.returncode == 0
    assert "Live threads" in r.stdout and "does a" in r.stdout


def test_selector_gives_deep_report(tmp_path):
    reg = _registry(tmp_path, [_thread("a")])
    r = _run(reg, "repo", "--no-scan")
    assert r.returncode == 0
    assert "checkouts" in r.stdout


def test_archived_flag(tmp_path):
    reg = _registry(
        tmp_path,
        [],
        tombstones=[
            {
                "sessionId": "z",
                "reason": "archived",
                "deletedAt": "2026-06-01T00:00:00+00:00",
                "project": "/p",
                "summary": "old work",
                "archivePath": "/arc/z.jsonl",
            }
        ],
    )
    r = _run(reg, "--archived", "--no-scan")
    assert r.returncode == 0 and "old work" in r.stdout


def test_triage_header_shows_when_inbox_nonempty(tmp_path):
    reg = _registry(tmp_path, [_thread("a")])  # undecided -> in inbox
    r = _run(reg, "--no-scan")
    assert "awaiting triage — rl triage" in r.stdout


def test_no_triage_header_when_all_reviewed(tmp_path):
    reg = _registry(tmp_path, [_thread("a", reviewedAtMtime="2026-06-20T00:00:00+00:00")])
    r = _run(reg, "--no-scan")
    assert "awaiting triage" not in r.stdout


def test_sessions_flag_hides_snoozed_unless_all(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("alpha"),
            _thread("beta", snoozedAt="2026-07-02T00:00:00+00:00"),
        ],
    )

    r = _run(reg, "-s", "--no-scan")
    assert r.returncode == 0
    assert "does alpha" in r.stdout
    assert "does beta" not in r.stdout
    assert "snoozed hidden" in r.stdout

    r_all = _run(reg, "-s", "-a", "--no-scan")
    assert r_all.returncode == 0
    assert "does beta" in r_all.stdout


def test_selector_hides_triage_header(tmp_path):
    reg = _registry(tmp_path, [_thread("a")])  # undecided -> would show global inbox count
    r = _run(reg, "repo", "--no-scan")
    assert r.returncode == 0
    assert "awaiting triage" not in r.stdout


def _registry_mem(tmp, memory_entries, cleaned_at=None):
    p = tmp / "registry.json"
    p.write_text(
        json.dumps(
            {
                "version": 2,
                "lastScan": "2026-07-01T00:00:00+00:00",
                "threads": [],
                "tombstones": [],
                "projectsCleanedAt": {},
                "memoryEntries": memory_entries,
                "memoryProjectsCleanedAt": cleaned_at or {},
            }
        ),
        encoding="utf-8",
    )
    return p


def _mem_entry(name, **kw):
    base = {
        "path": f"/p/-enc/memory/{name}.md",
        "project": "/p",
        "name": name,
        "gitProjectKey": "repo-k",
        "gitProjectLabel": "repo",
        "gitRoot": "/p",
        "obsoleteSignals": [],
        "reviewedAtMtime": None,
        "mtime": "2026-06-20T00:00:00+00:00",
    }
    base.update(kw)
    return base


def test_overview_memory_rollup(tmp_path):
    reg = _registry_mem(
        tmp_path,
        [
            _mem_entry("a"),
            _mem_entry("b", obsoleteSignals=["FIXED"]),
        ],
    )
    r = _run_mem(reg, "--no-scan")
    assert r.returncode == 0, r.stderr
    assert "repo" in r.stdout
    assert "2 entries" in r.stdout
    assert "1 obsolete-candidate" in r.stdout
    assert "uncleaned" in r.stdout


def test_overview_memory_selector_lists_entries(tmp_path):
    reg = _registry_mem(tmp_path, [_mem_entry("a")])
    r = _run_mem(reg, "repo", "--no-scan")
    assert r.returncode == 0, r.stderr
    assert "a" in r.stdout and "/p" in r.stdout


def test_overview_memory_shows_cleaned_project_with_zero_entries(tmp_path):
    reg = _registry_mem(tmp_path, [], {"github.com/n/gone": "2026-07-01T00:00:00+00:00"})
    r = _run_mem(reg, "--no-scan")
    assert r.returncode == 0, r.stderr
    assert "no memory entries" not in r.stdout
    assert "gone: 0 entries (0 obsolete-candidate) [cleaned" in r.stdout


def test_overview_memory_cleaned_zero_sorts_after_populated(tmp_path):
    reg = _registry_mem(
        tmp_path, [_mem_entry("a")], {"github.com/n/gone": "2026-07-01T00:00:00+00:00"}
    )
    r = _run_mem(reg, "--no-scan")
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert lines[0].startswith("repo: 1 entries")
    assert lines[1].startswith("gone: 0 entries")


def test_overview_memory_cleaned_zero_uses_cwd_key_as_label(tmp_path):
    reg = _registry_mem(tmp_path, [], {"/Users/x/wrksp/thing": "2026-07-01T00:00:00+00:00"})
    r = _run_mem(reg, "--no-scan")
    assert r.returncode == 0, r.stderr
    assert "/Users/x/wrksp/thing: 0 entries" in r.stdout


def test_overview_memory_still_empty_when_nothing_tracked(tmp_path):
    reg = _registry_mem(tmp_path, [])
    r = _run_mem(reg, "--no-scan")
    assert r.returncode == 0, r.stderr
    assert "no memory entries" in r.stdout
