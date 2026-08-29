import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
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


def _registry(tmp, threads, archived=None):
    p = tmp / "registry.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": "2026-07-01T00:00:00+00:00",
                "threads": threads,
                "archived": archived or [],
                "deleted": [],
                "projectsCleanedAt": {},
            }
        ),
        encoding="utf-8",
    )
    return p


def _run(reg, *args, cwd=REPO):
    env = dict(os.environ, SESSION_TRACKER_PROJECTS_DIR=str(reg.parent / "empty"))
    env["PYTHONPATH"] = os.pathsep.join(part for part in (str(REPO), env.get("PYTHONPATH")) if part)
    return subprocess.run(
        [sys.executable, "-m", "ringleader", "overview", "--registry", str(reg), *args],
        cwd=str(cwd),
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


def test_selector_status_filter_distinguishes_no_matches(tmp_path):
    reg = _registry(tmp_path, [_thread("active")])

    r = _run(reg, "repo", "--status", "finished", "--no-scan")

    assert r.returncode == 0, r.stderr
    assert "no sessions matching --status finished" in r.stdout


def test_cwd_selects_sessions_under_current_directory(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("inside", project=str(REPO / "nested")),
            _thread("outside", project=str(REPO.parent / "other")),
        ],
    )
    r = _run(reg, "--cwd", "--all", "--no-scan")
    assert r.returncode == 0
    assert "Live threads: 1" in r.stdout
    assert "does inside" in r.stdout
    assert "does outside" not in r.stdout


def test_cwd_uses_invocation_directory_and_git_root(tmp_path):
    invocation = tmp_path / "workspace" / "nested"
    invocation.mkdir(parents=True)
    reg = _registry(
        tmp_path,
        [
            _thread("child", project=str(invocation / "child")),
            _thread("parent-sibling", project=str(invocation.parent / "sibling")),
            _thread(
                "git-root",
                project="/elsewhere/checkout",
                gitRoot=str(invocation),
            ),
        ],
    )

    r = _run(reg, "--cwd", "--status", "all", "--no-scan", cwd=invocation)

    assert r.returncode == 0, r.stderr
    assert "does child" in r.stdout
    assert "does git-root" in r.stdout
    assert "does parent-sibling" not in r.stdout


def test_cwd_status_all_lists_every_scoped_thread(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("active", project=str(REPO / "nested")),
            _thread("finished", project=str(REPO / "nested"), status="finished"),
            _thread(
                "snoozed",
                project=str(REPO / "nested"),
                snoozedAt="2026-07-02T00:00:00+00:00",
            ),
            _thread("kept", project=str(REPO / "nested"), decision="keep"),
            _thread("outside", project=str(REPO.parent / "other")),
        ],
    )

    r = _run(reg, "--cwd", "--status", "all", "--no-scan")

    assert r.returncode == 0, r.stderr
    assert "Sessions by status: active:3, finished:1" in r.stdout
    assert "does active" in r.stdout
    assert "does finished" in r.stdout
    assert "does snoozed" in r.stdout
    assert "does kept" in r.stdout


def test_cwd_repeated_statuses_list_matching_sessions(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("active", project=str(REPO / "nested")),
            _thread(
                "unfinished",
                project=str(REPO / "nested"),
                status="unfinished",
                decision="keep",
            ),
            _thread(
                "blocked",
                project=str(REPO / "nested"),
                status="blocked",
                snoozedAt="2026-07-02T00:00:00+00:00",
            ),
            _thread("finished", project=str(REPO / "nested"), status="finished"),
        ],
    )

    r = _run(
        reg,
        "--cwd",
        "--status",
        "unfinished",
        "--status",
        "blocked",
        "--no-scan",
    )

    assert r.returncode == 0, r.stderr
    assert "Sessions by status: blocked:1, unfinished:1" in r.stdout
    assert "does unfinished" in r.stdout
    assert "does blocked" in r.stdout
    assert "does active" not in r.stdout
    assert "does finished" not in r.stdout


def test_cwd_does_not_imply_all_statuses(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("active", project=str(REPO / "nested")),
            _thread("finished", project=str(REPO / "nested"), status="finished"),
        ],
    )

    live = _run(reg, "--cwd", "--no-scan")
    all_statuses = _run(reg, "--cwd", "--all", "--no-scan")

    assert "does active" in live.stdout
    assert "does finished" not in live.stdout
    assert "does active" in all_statuses.stdout
    assert "does finished" in all_statuses.stdout


def test_cwd_scopes_json_sessions(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("inside", project=str(REPO / "nested")),
            _thread("outside", project=str(REPO.parent / "other")),
        ],
    )

    r = _run(reg, "--cwd", "--sessions", "--json", "--status", "all", "--no-scan")

    assert r.returncode == 0, r.stderr
    assert [row["session_id"] for row in json.loads(r.stdout)] == ["inside"]


def test_cwd_scopes_kept_and_snoozed_views(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread(
                "inside",
                project=str(REPO / "nested"),
                decision="keep",
                snoozedAt="2026-07-02T00:00:00+00:00",
            ),
            _thread(
                "outside",
                project=str(REPO.parent / "other"),
                decision="keep",
                snoozedAt="2026-07-02T00:00:00+00:00",
            ),
        ],
    )

    kept = _run(reg, "--cwd", "--kept", "--json", "--no-scan")
    snoozed = _run(reg, "--cwd", "--snoozed", "--json", "--no-scan")

    assert [row["session_id"] for row in json.loads(kept.stdout)] == ["inside"]
    assert [row["session_id"] for row in json.loads(snoozed.stdout)] == ["inside"]


def test_cwd_scopes_archived_view(tmp_path):
    reg = _registry(
        tmp_path,
        [],
        archived=[
            _thread("inside-archive", project=str(REPO / "nested"), summary="inside archive"),
            _thread(
                "outside-archive",
                project=str(REPO.parent / "other"),
                summary="outside archive",
            ),
        ],
    )

    r = _run(reg, "--cwd", "--archived", "--no-scan")

    assert r.returncode == 0, r.stderr
    assert "inside archive" in r.stdout
    assert "outside archive" not in r.stdout


def test_cwd_renders_multiple_projects_as_separate_groups(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread(
                "alpha",
                project=str(REPO / "alpha"),
                gitProjectKey="alpha-k",
                gitProjectLabel="alpha-repo",
            ),
            _thread(
                "beta",
                project=str(REPO / "beta"),
                gitProjectKey="beta-k",
                gitProjectLabel="beta-repo",
            ),
        ],
    )

    r = _run(reg, "--cwd", "--status", "all", "--no-scan")

    assert r.returncode == 0, r.stderr
    assert "## alpha-repo" in r.stdout
    assert "## beta-repo" in r.stdout


def test_cwd_rejects_explicit_project_selectors(tmp_path):
    reg = _registry(tmp_path, [_thread("inside", project=str(REPO / "nested"))])

    positional = _run(reg, "repo", "--cwd", "--no-scan")
    option = _run(reg, "--project", "repo", "--cwd", "--no-scan")

    assert positional.returncode == 2
    assert option.returncode == 2


def test_archived_flag(tmp_path):
    reg = _registry(
        tmp_path,
        [],
        archived=[
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


def test_overview_scan_does_not_archive_a_finished_session(tmp_path):
    projects = tmp_path / "projects" / "-w-x"
    projects.mkdir(parents=True)
    transcript = projects / "s1.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"hi"}}\n')
    when = time.time() - 600
    os.utime(transcript, (when, when))

    reg = _registry(
        tmp_path,
        [
            _thread(
                "s1",
                file=str(transcript),
                project="/w/x",
                decision="harvested",
                finishOnExit=True,
            )
        ],
    )

    env = dict(
        os.environ,
        HOME=str(tmp_path / "home"),
        SESSION_TRACKER_PROJECTS_DIR=str(tmp_path / "projects"),
    )
    env["PYTHONPATH"] = os.pathsep.join(part for part in (str(REPO), env.get("PYTHONPATH")) if part)
    r = subprocess.run(
        [sys.executable, "-m", "ringleader", "overview", "--registry", str(reg)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert r.returncode == 0, r.stderr
    out = json.loads(reg.read_text())
    assert out.get("archived", []) == []
    assert transcript.exists()


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


def test_sessions_text_filters_by_status(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("active"),
            _thread("finished", status="finished"),
        ],
    )

    r = _run(reg, "--sessions", "--status", "finished", "--no-scan")

    assert r.returncode == 0, r.stderr
    assert "does finished" in r.stdout
    assert "does active" not in r.stdout


def test_rollup_text_filters_by_status(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("active", gitProjectKey="active-k", gitProjectLabel="active-repo"),
            _thread(
                "finished",
                status="finished",
                gitProjectKey="finished-k",
                gitProjectLabel="finished-repo",
            ),
        ],
    )

    r = _run(reg, "--status", "finished", "--no-scan")

    assert r.returncode == 0, r.stderr
    assert "finished-repo" in r.stdout
    assert "active-repo" not in r.stdout


def test_sessions_flag_auto_wakes_after_until(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("alpha"),
            _thread(
                "beta",
                snoozedAt="2026-07-02T00:00:00+00:00",
                snoozedUntil="2020-01-01T00:00:00+00:00",  # long expired
            ),
            _thread(
                "gamma",
                snoozedAt="2026-07-02T00:00:00+00:00",
                snoozedUntil="2099-01-01T00:00:00+00:00",  # still snoozed
            ),
        ],
    )

    r = _run(reg, "-s", "--no-scan")
    assert r.returncode == 0
    assert "does alpha" in r.stdout
    assert "does beta" in r.stdout  # expired snooze -> auto-woken, included
    assert "does gamma" not in r.stdout  # still within its snooze window


def test_sessions_list_shows_source_badge(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("cc", source="claude-code"),
            _thread("cx", source="codex"),
        ],
    )
    r = _run(reg, "-s", "--no-scan")
    assert r.returncode == 0
    assert "(claude)" in r.stdout
    assert "(codex)" in r.stdout


def test_source_filter_limits_to_codex(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("cc", source="claude-code"),
            _thread("cx", source="codex"),
        ],
    )
    r = _run(reg, "-s", "--source", "codex", "--no-scan")
    assert r.returncode == 0
    assert "does cx" in r.stdout
    assert "does cc" not in r.stdout


def test_selector_hides_triage_header(tmp_path):
    reg = _registry(tmp_path, [_thread("a")])  # undecided -> would show global inbox count
    r = _run(reg, "repo", "--no-scan")
    assert r.returncode == 0
    assert "awaiting triage" not in r.stdout


def test_json_flag_emits_rollup_not_session_array(tmp_path):
    reg = _registry(tmp_path, [_thread("a")])
    r = _run(reg, "--json", "--no-scan")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert isinstance(data, dict)
    assert "live_count" in data


def test_json_rollup_hides_snoozed_but_counts_auto_woken(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("alpha"),
            _thread(
                "beta",
                snoozedAt="2026-07-02T00:00:00+00:00",
                snoozedUntil="2020-01-01T00:00:00+00:00",  # long expired -> auto-woken
            ),
            _thread(
                "gamma",
                snoozedAt="2026-07-02T00:00:00+00:00",
                snoozedUntil="2099-01-01T00:00:00+00:00",  # still snoozed -> hidden
            ),
        ],
    )
    r = _run(reg, "--json", "--no-scan")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert data["live_count"] == 2  # alpha + auto-woken beta, gamma hidden
    assert data["by_project"] == {"repo-k": 2}


def test_json_rollup_filters_by_status(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("active"),
            _thread("finished", status="finished"),
        ],
    )

    r = _run(reg, "--json", "--status", "finished", "--no-scan")

    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["by_status"] == {"finished": 1}
    assert data["live_count"] == 0


def test_json_rollup_status_all_includes_snoozed(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("active"),
            _thread("snoozed", snoozedAt="2026-07-02T00:00:00+00:00"),
        ],
    )

    r = _run(reg, "--json", "--status", "all", "--no-scan")

    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["live_count"] == 2


def test_sessions_flag_json_emits_session_array(tmp_path):
    reg = _registry(tmp_path, [_thread("a")])
    r = _run(reg, "-s", "--json", "--no-scan")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert isinstance(data, list)
    assert data[0]["session_id"] == "a"


def test_sessions_json_filters_by_status(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("active"),
            _thread("finished", status="finished"),
        ],
    )

    r = _run(reg, "-s", "--json", "--status", "finished", "--no-scan")

    assert r.returncode == 0, r.stderr
    assert [row["session_id"] for row in json.loads(r.stdout)] == ["finished"]


def test_sessions_status_filter_uses_effective_status(tmp_path):
    unclassified = _thread("unclassified")
    unclassified.pop("status")
    reg = _registry(
        tmp_path,
        [
            _thread("abandoned", status="unfinished", mtime="2020-01-01T00:00:00+00:00"),
            _thread("harvested", decision="harvested"),
            unclassified,
        ],
    )

    for status, expected in (
        ("abandoned", "abandoned"),
        ("harvested", "harvested"),
        ("unclassified", "unclassified"),
    ):
        r = _run(reg, "--sessions", "--json", "--status", status, "--no-scan")
        assert r.returncode == 0, r.stderr
        assert [row["session_id"] for row in json.loads(r.stdout)] == [expected]


def test_snoozed_json_excludes_expired_includes_active(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread("alpha"),
            _thread(
                "beta",
                snoozedAt="2026-07-02T00:00:00+00:00",
                snoozedUntil="2020-01-01T00:00:00+00:00",  # long expired -> auto-woken
            ),
            _thread(
                "gamma",
                snoozedAt="2026-07-02T00:00:00+00:00",
                snoozedUntil="2099-01-01T00:00:00+00:00",  # still snoozed
            ),
        ],
    )
    r = _run(reg, "--snoozed", "--json", "--no-scan")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert [row["session_id"] for row in data] == ["gamma"]
    assert data[0]["snoozed_until"] == "2099-01-01T00:00:00+00:00"


def test_snoozed_text_lists_wake_time(tmp_path):
    reg = _registry(
        tmp_path,
        [
            _thread(
                "gamma",
                snoozedAt="2026-07-02T00:00:00+00:00",
                snoozedUntil="2099-01-01T00:00:00+00:00",
            ),
        ],
    )
    r = _run(reg, "--snoozed", "--no-scan")
    assert r.returncode == 0
    assert "gamma" in r.stdout
    assert "2099-01-01T00:00:00+00:00" in r.stdout


def test_snoozed_text_empty_state(tmp_path):
    reg = _registry(tmp_path, [_thread("alpha")])  # no thread is snoozed
    r = _run(reg, "--snoozed", "--no-scan")
    assert r.returncode == 0
    assert "no snoozed sessions" in r.stdout


def test_sessions_json_recent_narrows_by_age(tmp_path):
    now = datetime.now(UTC)
    old_mtime = (now - timedelta(days=50)).isoformat()
    reg = _registry(tmp_path, [_thread("old", mtime=old_mtime)])
    r = _run(reg, "-s", "--json", "--recent", "1", "--no-scan")
    assert r.returncode == 0
    assert json.loads(r.stdout) == []


def _registry_mem(tmp, memory_entries, cleaned_at=None):
    p = tmp / "registry.json"
    p.write_text(
        json.dumps(
            {
                "version": 2,
                "lastScan": "2026-07-01T00:00:00+00:00",
                "threads": [],
                "archived": [],
                "deleted": [],
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
