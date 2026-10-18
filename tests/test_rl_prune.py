import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ringleader.commands import prune
from ringleader.sources.base import stat_fingerprint

REPO = Path(__file__).resolve().parent.parent

_OLD_ARCHIVE = "2026-01-01T00:00:00+00:00"  # far past any retention against real `now`


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    # cmd_prune reads config via load_config(); pin XDG so no test touches the real store.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    # stub detection scans installed sources; point both at test dirs so no test
    # touches the real ~/.claude/projects or ~/.codex/sessions on this machine.
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(tmp_path / "claude-projects"))
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(tmp_path / "codex-sessions"))


_STORES = {"claude-code": "claude-projects", "codex": "codex-sessions"}


def _thread(  # noqa: PLR0913 -- test data-builder helper, one arg per fixture field
    tmp,
    sid,
    *,
    size_bytes,
    msgs=1,
    mtime="2026-01-01T00:00:00+00:00",
    decision=None,
    git_label="repo",
    project="/p",
    source="claude-code",
):
    """Write a real transcript file + sidecar dir, return the registry thread."""
    store = tmp / _STORES[source] / "-p"
    store.mkdir(parents=True, exist_ok=True)
    f = store / f"{sid}.jsonl"
    f.write_text("x" * size_bytes, encoding="utf-8")
    side = store / sid
    side.mkdir()
    (side / "note.txt").write_text("sidecar", encoding="utf-8")
    return {
        "sessionId": sid,
        "source": source,
        "file": str(f),
        "fileFingerprint": stat_fingerprint(f.stat()),
        "project": project,
        "title": None,
        "aiTitle": None,
        "summary": None,
        "lastPrompt": "lp",
        "mtime": mtime,
        "sizeBytes": size_bytes,
        "messageCount": msgs,
        "gitBranch": None,
        "gitProjectKey": git_label + "-k",
        "gitProjectLabel": git_label,
        "gitRoot": None,
        "gitRemote": None,
        "status": "blocked",
        "statusConfidence": 0.5,
        "whatItIs": "w",
        "suggestedName": f"{sid}-name",
        "appliedName": None,
        "namedAtMtime": None,
        "classifiedAtMtime": None,
        "decision": decision,
        "harvestPath": None,
        "todoId": None,
        "tags": [],
    }


def _write_registry(tmp, threads):
    p = tmp / "registry.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": "2026-06-30T00:00:00+00:00",
                "threads": threads,
                "archived": [],
                "deleted": [],
            }
        ),
        encoding="utf-8",
    )
    return p


def _run(reg, *args, stdin=""):
    return subprocess.run(
        [sys.executable, "-m", "ringleader", "prune", "--registry", str(reg), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
        input=stdin,
    )


def test_preview_deletes_nothing(tmp_path):
    t = _thread(tmp_path, "a", size_bytes=100)
    reg = _write_registry(tmp_path, [t])
    r = _run(reg)
    assert r.returncode == 0
    assert "a-name" in r.stdout and "preview" in r.stdout
    assert Path(t["file"]).exists()  # nothing deleted
    assert json.loads(reg.read_text())["threads"]  # registry unchanged


def test_delete_yes_removes_file_sidecar_and_tombstones(tmp_path):
    t = _thread(tmp_path, "a", size_bytes=100)
    reg = _write_registry(tmp_path, [t])
    r = _run(reg, "--delete", stdin="y\n")
    assert r.returncode == 0 and "deleted 1" in r.stdout
    assert not Path(t["file"]).exists()  # transcript gone
    assert not Path(t["file"]).with_suffix("").exists()  # sidecar dir gone
    data = json.loads(reg.read_text())
    assert data["threads"] == []
    assert data["deleted"][0]["sessionId"] == "a"
    assert data["deleted"][0]["reason"] == "prune"


def test_delete_no_keeps_everything(tmp_path):
    t = _thread(tmp_path, "a", size_bytes=100)
    reg = _write_registry(tmp_path, [t])
    r = _run(reg, "--delete", stdin="n\n")
    assert "aborted" in r.stdout
    assert Path(t["file"]).exists()
    assert json.loads(reg.read_text())["threads"]


def test_project_scope_limits_candidates(tmp_path):
    a = _thread(tmp_path, "a", size_bytes=100, git_label="repo-a")
    b = _thread(tmp_path, "b", size_bytes=100, git_label="repo-b")
    reg = _write_registry(tmp_path, [a, b])
    r = _run(reg, "--project", "repo-a", "--delete", stdin="y\n")
    assert "deleted 1" in r.stdout
    assert not Path(a["file"]).exists()
    assert Path(b["file"]).exists()  # repo-b untouched


def test_missing_transcript_is_tolerated(tmp_path):
    t = _thread(tmp_path, "a", size_bytes=100)
    Path(t["file"]).unlink()  # gone out-of-band
    reg = _write_registry(tmp_path, [t])
    r = _run(reg, "--delete", stdin="y\n")
    assert r.returncode == 0 and "deleted 1" in r.stdout
    assert not Path(t["file"]).with_suffix("").exists()  # sidecar still cleaned up
    assert json.loads(reg.read_text())["deleted"][0]["sessionId"] == "a"


def test_delete_refuses_a_transcript_changed_after_the_scan(tmp_path):
    t = _thread(tmp_path, "a", size_bytes=100)
    reg = _write_registry(tmp_path, [t])
    with open(t["file"], "a", encoding="utf-8") as handle:
        handle.write("more")
    r = _run(reg, "--delete", stdin="y\n")
    assert r.returncode == 0 and "deleted 0 sessions, skipped 1" in r.stdout
    assert "skipped a: session changed since selection" in r.stderr
    assert Path(t["file"]).exists()


def test_codex_thread_tiny_by_message_count_despite_large_bytes(tmp_path):
    """Codex transcripts are event-heavy (full request/response payloads), so a
    genuinely tiny session (few real messages) can sit well above the byte
    ceiling. Tininess must key on messageCount, not sizeBytes."""
    t = _thread(tmp_path, "codex-a", size_bytes=200 * 1024, msgs=2, source="codex")
    reg = _write_registry(tmp_path, [t])
    r = _run(reg, "--delete", stdin="y\n")
    assert r.returncode == 0 and "deleted 1" in r.stdout
    assert not Path(t["file"]).exists()


def test_keep_marked_session_never_listed(tmp_path):
    t = _thread(tmp_path, "a", size_bytes=100, decision="keep")
    reg = _write_registry(tmp_path, [t])
    r = _run(reg)
    assert "nothing to prune" in r.stdout
    assert Path(t["file"]).exists()


# --- archive retention GC ---


def _archive_root(tmp_path):
    return tmp_path / "xdg" / "ringleader" / "archive"  # matches paths.archive_root() under XDG


def _archive_files(tmp_path, slug, sid):
    d = _archive_root(tmp_path) / slug
    d.mkdir(parents=True, exist_ok=True)
    jsonl = d / f"{sid}.jsonl"
    jsonl.write_text("t", encoding="utf-8")
    side = d / sid
    side.mkdir()
    (side / "n.txt").write_text("s", encoding="utf-8")
    (d / f"{sid}.md").write_text("summary", encoding="utf-8")
    return jsonl


def _archived_rec(sid, archive_path, archived_at, project="repo"):
    return {
        "sessionId": sid,
        "archivedAt": archived_at,
        "archivePath": str(archive_path),
        "harvestPath": None,
        "summary": "s",
        "project": project,
    }


def _write_reg_full(tmp_path, threads, archived):
    p = tmp_path / "registry.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": "2026-06-30T00:00:00+00:00",
                "threads": threads,
                "archived": archived,
                "deleted": [],
            }
        ),
        encoding="utf-8",
    )
    return p


def _write_config(tmp_path, days):
    d = tmp_path / "xdg" / "ringleader"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps({"archive_retention_days": days}), encoding="utf-8")


def test_archive_preview_lists_and_deletes_nothing(tmp_path):
    _write_config(tmp_path, 30)
    jsonl = _archive_files(tmp_path, "repo", "arch1")
    reg = _write_reg_full(tmp_path, [], [_archived_rec("arch1", jsonl, _OLD_ARCHIVE)])

    r = _run(reg)

    assert r.returncode == 0
    assert "arch1" in r.stdout and "past 30d retention" in r.stdout and "preview" in r.stdout
    assert jsonl.exists()  # nothing deleted
    assert json.loads(reg.read_text())["archived"][0].get("reason", "archived") == "archived"


def test_archive_delete_purges_files_and_flips_tombstone(tmp_path):
    _write_config(tmp_path, 30)
    jsonl = _archive_files(tmp_path, "repo", "arch1")
    reg = _write_reg_full(tmp_path, [], [_archived_rec("arch1", jsonl, _OLD_ARCHIVE)])

    r = _run(reg, "--delete", stdin="y\n")

    assert r.returncode == 0 and "purged 1 archived" in r.stdout
    assert not jsonl.exists()
    assert not (_archive_root(tmp_path) / "repo" / "arch1").exists()  # sidecar gone
    assert not (_archive_root(tmp_path) / "repo" / "arch1.md").exists()  # summary gone
    ts = json.loads(reg.read_text())["deleted"][0]
    assert ts["reason"] == "purged" and ts["purgedAt"]
    assert ts["archivedAt"] == _OLD_ARCHIVE  # archive date preserved


def test_archive_retention_unset_lists_nothing(tmp_path):
    # no config written -> archive_retention_days defaults to 0
    jsonl = _archive_files(tmp_path, "repo", "arch1")
    reg = _write_reg_full(tmp_path, [], [_archived_rec("arch1", jsonl, _OLD_ARCHIVE)])

    r = _run(reg)

    assert "nothing to prune" in r.stdout
    assert jsonl.exists()


def test_archive_delete_tolerates_missing_files(tmp_path):
    _write_config(tmp_path, 30)
    missing = _archive_root(tmp_path) / "repo" / "gone.jsonl"  # never created
    reg = _write_reg_full(tmp_path, [], [_archived_rec("gone", missing, _OLD_ARCHIVE)])

    r = _run(reg, "--delete", stdin="y\n")

    assert r.returncode == 0 and "purged 1 archived" in r.stdout
    assert json.loads(reg.read_text())["deleted"][0]["reason"] == "purged"


def test_prune_reports_both_live_and_archived(tmp_path):
    _write_config(tmp_path, 30)
    t = _thread(tmp_path, "live", size_bytes=100)
    jsonl = _archive_files(tmp_path, "repo", "arch1")
    reg = _write_reg_full(tmp_path, [t], [_archived_rec("arch1", jsonl, _OLD_ARCHIVE)])

    r = _run(reg, "--delete", stdin="y\n")

    assert "deleted 1 sessions" in r.stdout and "purged 1 archived" in r.stdout
    assert not Path(t["file"]).exists() and not jsonl.exists()


def test_archive_project_scope_limits_purge(tmp_path):
    _write_config(tmp_path, 30)
    a = _archive_files(tmp_path, "repo-a", "arch-a")
    b = _archive_files(tmp_path, "repo-b", "arch-b")
    reg = _write_reg_full(
        tmp_path,
        [],
        [
            _archived_rec("arch-a", a, _OLD_ARCHIVE, project="repo-a"),
            _archived_rec("arch-b", b, _OLD_ARCHIVE, project="repo-b"),
        ],
    )

    r = _run(reg, "--project", "repo-a", "--delete", stdin="y\n")

    assert r.returncode == 0 and "purged 1 archived" in r.stdout
    assert not a.exists()  # scoped project purged
    assert b.exists()  # other project untouched
    saved = json.loads(reg.read_text())
    assert [d["sessionId"] for d in saved["deleted"]] == ["arch-a"]  # purged out of the archive
    assert [a["sessionId"] for a in saved["archived"]] == ["arch-b"]  # other project untouched


# --- resurrected stub (transcript recreated after archiving) ---


def test_prune_lists_a_resurrected_archived_transcript(tmp_path, monkeypatch, capsys):
    projects_dir = tmp_path / "projects" / "-w-x"
    projects_dir.mkdir(parents=True)
    stub = projects_dir / "s1.jsonl"
    stub.write_text('{"type":"user","message":{"content":"hi"}}\n')
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(tmp_path / "projects"))

    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [],
                "deleted": [],
                "archived": [{"sessionId": "s1", "archivedAt": "2026-08-28T09:00:00+00:00"}],
            }
        )
    )

    assert prune.cmd_prune(["--registry", str(reg)]) == 0
    assert "s1" in capsys.readouterr().out
    assert stub.exists()  # preview only


def test_prune_delete_removes_a_resurrected_archived_transcript(tmp_path, monkeypatch, capsys):
    projects_dir = tmp_path / "projects" / "-w-x"
    projects_dir.mkdir(parents=True)
    stub = projects_dir / "s1.jsonl"
    stub.write_text('{"type":"user","message":{"content":"hi"}}\n')
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    # a genuine, unrelated thread that is NOT prune-eligible — its continued presence in
    # `threads` after the run shows stub deletion really leaves `threads` alone, rather
    # than the assertion being trivially true on an empty list. Dated relative to now:
    # a literal decays past DEFAULT_OLDER_THAN_DAYS and silently inverts the fixture.
    recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    live_thread = _thread(tmp_path, "live", size_bytes=100, mtime=recent)

    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [live_thread],
                "deleted": [],
                "archived": [{"sessionId": "s1", "archivedAt": "2026-08-28T09:00:00+00:00"}],
            }
        )
    )

    assert prune.cmd_prune(["--registry", str(reg), "--delete"]) == 0
    out = capsys.readouterr().out
    assert "removed 1 resurrected" in out
    assert not stub.exists()  # the stray recreated file is gone
    data = json.loads(reg.read_text())
    assert [t["sessionId"] for t in data["threads"]] == ["live"]  # unrelated thread untouched
    # the real archived copy is a separate concern — the archive record is untouched
    assert [a["sessionId"] for a in data["archived"]] == ["s1"]


def test_prune_delete_reports_a_stub_it_cannot_unlink(tmp_path, monkeypatch, capsys):
    projects_dir = tmp_path / "projects" / "-w-x"
    projects_dir.mkdir(parents=True)
    stub = projects_dir / "s1.jsonl"
    stub.write_text('{"type":"user","message":{"content":"hi"}}\n')
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    real_unlink = Path.unlink

    def denied(self, *a, **kw):
        if self.name == "s1.jsonl":
            raise OSError(13, "Permission denied")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", denied)

    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [],
                "deleted": [],
                "archived": [{"sessionId": "s1", "archivedAt": "2026-08-28T09:00:00+00:00"}],
            }
        )
    )

    assert prune.cmd_prune(["--registry", str(reg), "--delete"]) == 0
    captured = capsys.readouterr()
    assert "skipped s1" in captured.err
    assert "removed 0 resurrected" in captured.out
    assert stub.exists()


def test_prune_scopes_resurrected_stubs_by_project(tmp_path, monkeypatch, capsys):
    proj_a = tmp_path / "projects" / "-w-a"
    proj_b = tmp_path / "projects" / "-w-b"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)
    stub_a = proj_a / "sa.jsonl"
    stub_b = proj_b / "sb.jsonl"
    stub_a.write_text(
        '{"cwd":"/w/a","gitBranch":"main"}\n{"type":"user","message":{"content":"hi"}}\n'
    )
    stub_b.write_text(
        '{"cwd":"/w/b","gitBranch":"main"}\n{"type":"user","message":{"content":"hi"}}\n'
    )
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(tmp_path / "projects"))

    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [],
                "deleted": [],
                "archived": [
                    {"sessionId": "sa", "archivedAt": "2026-08-28T09:00:00+00:00"},
                    {"sessionId": "sb", "archivedAt": "2026-08-28T09:00:00+00:00"},
                ],
            }
        )
    )

    # preview scoped to project "a" only reports sa
    assert prune.cmd_prune(["--registry", str(reg), "--project", "/w/a"]) == 0
    out = capsys.readouterr().out
    assert "sa" in out
    assert "sb" not in out

    # --delete scoped to project "a" must not touch project "b"'s stub
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert prune.cmd_prune(["--registry", str(reg), "--project", "/w/a", "--delete"]) == 0
    assert not stub_a.exists()
    assert stub_b.exists()  # other project's resurrected stub untouched


def test_stub_deletion_clears_the_picker_entry_first(tmp_path, monkeypatch, capsys):
    """Same order as `prune.delete_sessions`: unlinking first strands a picker entry."""
    projects_dir = tmp_path / "projects" / "-w-x"
    projects_dir.mkdir(parents=True)
    stub = projects_dir / "s1.jsonl"
    stub.write_text('{"type":"user","message":{"content":"hi"}}\n')
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    calls = []

    def record(thread):
        calls.append((thread["sessionId"], thread["source"], Path(thread["file"]).exists()))
        return True

    monkeypatch.setattr("ringleader.scanner.hide_from_picker", record)

    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [],
                "deleted": [],
                "archived": [{"sessionId": "s1", "archivedAt": "2026-08-28T09:00:00+00:00"}],
            }
        )
    )

    assert prune.cmd_prune(["--registry", str(reg), "--delete"]) == 0
    assert calls == [("s1", "claude-code", True)]  # called while the transcript is still there
    assert not stub.exists()


def test_stub_deletion_skips_a_transcript_whose_picker_entry_survives(
    tmp_path, monkeypatch, capsys
):
    projects_dir = tmp_path / "projects" / "-w-x"
    projects_dir.mkdir(parents=True)
    stub = projects_dir / "s1.jsonl"
    stub.write_text('{"type":"user","message":{"content":"hi"}}\n')
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    def refuse(_thread):
        raise RuntimeError("picker index is read-only")

    monkeypatch.setattr("ringleader.scanner.hide_from_picker", refuse)

    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [],
                "deleted": [],
                "archived": [{"sessionId": "s1", "archivedAt": "2026-08-28T09:00:00+00:00"}],
            }
        )
    )

    assert prune.cmd_prune(["--registry", str(reg), "--delete"]) == 0
    captured = capsys.readouterr()
    assert "picker index is read-only" in captured.err
    assert "removed 0 resurrected" in captured.out
    assert stub.exists()


def test_delete_writes_the_registry_once_per_batch(tmp_path, monkeypatch, capsys):
    from ringleader import prune as prune_api
    from ringleader import registry

    threads = [_thread(tmp_path, f"s{i:02d}", size_bytes=10) for i in range(30)]
    reg = _write_registry(tmp_path, threads)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    write = registry.atomic_write_text
    writes = []

    def counted(target, text, **options):
        writes.append(target)
        write(target, text, **options)

    monkeypatch.setattr(registry, "atomic_write_text", counted)

    assert prune.cmd_prune(["--registry", str(reg), "--delete"]) == 0

    assert "deleted 30 sessions, skipped 0" in capsys.readouterr().out
    assert len(writes) == -(-30 // prune_api.DELETE_BATCH)
    assert json.loads(reg.read_text())["threads"] == []


def _handoffs(tmp_path):
    d = tmp_path / "xdg" / "ringleader" / "handoffs"
    d.mkdir(parents=True, exist_ok=True)
    old = d / "old-session-20260101T000000000000Z.md"
    old.write_text("# old", encoding="utf-8")
    stale = datetime.now(UTC) - timedelta(days=40)
    os.utime(old, (stale.timestamp(), stale.timestamp()))
    fresh = d / "fresh-session-20260901T000000000000Z.md"
    fresh.write_text("# fresh", encoding="utf-8")
    return old, fresh


def test_handoff_exports_past_retention_are_previewed_and_kept(tmp_path):
    _write_config(tmp_path, 30)
    old, fresh = _handoffs(tmp_path)
    reg = _write_reg_full(tmp_path, [], [])

    r = _run(reg)

    assert r.returncode == 0
    assert f"handoff {old.name}" in r.stdout
    assert "1 handoff export(s) past retention" in r.stdout
    assert (old.exists(), fresh.exists()) == (True, True)


def test_delete_removes_handoff_exports_past_retention(tmp_path):
    _write_config(tmp_path, 30)
    old, fresh = _handoffs(tmp_path)
    reg = _write_reg_full(tmp_path, [], [])

    r = _run(reg, "--delete", stdin="y\n")

    assert "removed 0 resurrected, 1 handoff export(s)" in r.stdout
    assert (old.exists(), fresh.exists()) == (False, True)


def test_handoff_exports_stay_when_retention_is_off(tmp_path):
    old, _ = _handoffs(tmp_path)
    reg = _write_reg_full(tmp_path, [], [])

    r = _run(reg, "--delete", stdin="y\n")

    assert "nothing to prune" in r.stdout
    assert old.exists()
