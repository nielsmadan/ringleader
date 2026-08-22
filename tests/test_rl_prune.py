import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

_OLD_ARCHIVE = "2026-01-01T00:00:00+00:00"  # far past any retention against real `now`


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    # cmd_prune reads config via load_config(); pin XDG so no test touches the real store.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))


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
):
    """Write a real transcript file + sidecar dir, return the registry thread."""
    f = tmp / f"{sid}.jsonl"
    f.write_text("x" * size_bytes, encoding="utf-8")
    side = tmp / sid
    side.mkdir()
    (side / "note.txt").write_text("sidecar", encoding="utf-8")
    return {
        "sessionId": sid,
        "file": str(f),
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
    assert not (tmp_path / "a").exists()  # sidecar dir gone
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
    assert not (tmp_path / "a").exists()  # sidecar still cleaned up
    assert json.loads(reg.read_text())["deleted"][0]["sessionId"] == "a"


def test_codex_thread_tiny_by_message_count_despite_large_bytes(tmp_path):
    """Codex transcripts are event-heavy (full request/response payloads), so a
    genuinely tiny session (few real messages) can sit well above the byte
    ceiling. Tininess must key on messageCount, not sizeBytes."""
    t = _thread(tmp_path, "codex-a", size_bytes=200 * 1024, msgs=2)
    t["source"] = "codex"
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
