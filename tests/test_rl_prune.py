import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


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
                "tombstones": [],
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
    assert data["tombstones"][0]["sessionId"] == "a"
    assert data["tombstones"][0]["reason"] == "prune"


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
    assert json.loads(reg.read_text())["tombstones"][0]["sessionId"] == "a"


def test_keep_marked_session_never_listed(tmp_path):
    t = _thread(tmp_path, "a", size_bytes=100, decision="keep")
    reg = _write_registry(tmp_path, [t])
    r = _run(reg)
    assert "nothing to prune" in r.stdout
    assert Path(t["file"]).exists()
