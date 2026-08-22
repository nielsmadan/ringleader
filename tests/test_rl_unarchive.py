import json
import subprocess
import sys
from pathlib import Path

import pytest
from ringleader.commands import unarchive

REPO = Path(__file__).resolve().parent.parent


def _run(*args):
    return subprocess.run(
        [sys.executable, "-m", "ringleader", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
    )


def _setup(tmp_path):
    """An archived session: transcript sitting in the archive, tombstone in the registry."""
    proj, arc = tmp_path / "proj", tmp_path / "arc" / "proj"
    proj.mkdir()
    arc.mkdir(parents=True)
    (arc / "s1.jsonl").write_text("{}\n")
    original = proj / "s1.jsonl"
    thread = {
        "sessionId": "s1",
        "source": "claude-code",
        "file": str(original),
        "project": str(proj),
        "title": "my-thread",
        "whatItIs": "important work",
        "status": "unfinished",
        "mtime": "2026-08-01T00:00:00+00:00",
        "decision": None,
    }
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [],
                "archived": [
                    {
                        "sessionId": "s1",
                        "reason": "archived",
                        "archivePath": str(arc / "s1.jsonl"),
                        "originalPath": str(original),
                        "thread": thread,
                    }
                ],
            }
        )
    )
    return reg, original


def test_unarchive_restores_file_and_thread(tmp_path):
    reg, original = _setup(tmp_path)
    r = _run("session", "unarchive", "s1", "--registry", str(reg))
    assert r.returncode == 0, r.stderr
    assert original.exists()

    data = json.loads(reg.read_text())
    assert data["archived"] == []
    t = data["threads"][0]
    # the classification survives the round-trip: a restored session is not stripped
    assert t["sessionId"] == "s1"
    assert t["whatItIs"] == "important work"
    assert t["file"] == str(original)


def test_unarchive_unknown_session_fails(tmp_path):
    reg, _ = _setup(tmp_path)
    r = _run("session", "unarchive", "nope", "--registry", str(reg))
    assert r.returncode == 1
    assert "no archived session" in r.stderr


def test_unarchive_refuses_to_overwrite_an_existing_file(tmp_path):
    reg, original = _setup(tmp_path)
    original.write_text("already here\n")
    r = _run("session", "unarchive", "s1", "--registry", str(reg))
    assert r.returncode == 1
    assert "already exists" in r.stderr
    assert original.read_text() == "already here\n"  # untouched


def test_keep_on_archived_restores_then_keeps(tmp_path):
    reg, original = _setup(tmp_path)
    r = _run("session", "keep", "s1", "--yes", "--registry", str(reg))
    assert r.returncode == 0, r.stderr
    assert original.exists()
    t = json.loads(reg.read_text())["threads"][0]
    assert t["decision"] == "keep"
    assert t["keptAt"]


def test_restore_without_original_path_is_refused(tmp_path):
    """Tombstones written before originalPath was recorded can't be auto-restored."""
    reg = {"threads": [], "archived": []}
    ts = {"sessionId": "old", "reason": "archived", "archivePath": str(tmp_path / "a.jsonl")}
    with pytest.raises(RuntimeError, match="original path"):
        unarchive.restore(reg, ts)


def test_restore_missing_archived_file_is_refused(tmp_path):
    reg = {"threads": [], "archived": []}
    ts = {
        "sessionId": "old",
        "reason": "archived",
        "archivePath": str(tmp_path / "gone.jsonl"),
        "originalPath": str(tmp_path / "back.jsonl"),
    }
    with pytest.raises(RuntimeError, match="gone"):
        unarchive.restore(reg, ts)
