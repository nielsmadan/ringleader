import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from ringleader import lifecycle
from ringleader.commands import keep, sweep, unarchive
from ringleader.sources.base import IndexUpdate

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
        lifecycle.restore(reg, ts)


def test_restore_missing_archived_file_is_refused(tmp_path):
    reg = {"threads": [], "archived": []}
    ts = {
        "sessionId": "old",
        "reason": "archived",
        "archivePath": str(tmp_path / "gone.jsonl"),
        "originalPath": str(tmp_path / "back.jsonl"),
    }
    with pytest.raises(RuntimeError, match="gone"):
        lifecycle.restore(reg, ts)


def test_restored_thread_is_not_swept_again(tmp_path, monkeypatch):
    """A session archived by the finish path comes back unmarked, so the next sweep
    leaves it alone — otherwise unarchiving re-arms the archive it just undid."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    proj = tmp_path / "proj"
    proj.mkdir()
    transcript = proj / "s1.jsonl"
    transcript.write_text('{"type":"user"}\n')
    old = time.time() - 600
    os.utime(transcript, (old, old))
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [
                    {
                        "sessionId": "s1",
                        "source": "claude-code",
                        "file": str(transcript),
                        "project": str(proj),
                        "decision": "harvested",
                        "finishOnExit": True,
                        "finishedAt": "2026-08-28T00:00:00+00:00",
                    }
                ],
                "archived": [],
                "deleted": [],
            }
        )
    )

    assert sweep.cmd_sweep(["--registry", str(reg_path)]) == 0
    assert not transcript.exists()
    stored = json.loads(reg_path.read_text())["archived"][0]["thread"]
    assert "finishOnExit" not in stored
    assert "finishedAt" not in stored

    assert unarchive.cmd_unarchive(["s1", "--registry", str(reg_path)]) == 0
    restored = json.loads(reg_path.read_text())["threads"][0]
    assert restored.get("finishOnExit") is None

    assert sweep.cmd_sweep(["--registry", str(reg_path)]) == 0
    after = json.loads(reg_path.read_text())
    assert [t["sessionId"] for t in after["threads"]] == ["s1"]
    assert after["archived"] == []
    assert transcript.exists()


def test_restore_strips_a_finish_mark_left_by_an_older_build(tmp_path):
    """Records archived before the mark was cleared at archive time still carry it."""
    reg, _original = _setup(tmp_path)
    data = json.loads(reg.read_text())
    data["archived"][0]["thread"]["finishOnExit"] = True
    data["archived"][0]["thread"]["finishedAt"] = "2026-08-28T00:00:00+00:00"
    reg.write_text(json.dumps(data))

    assert unarchive.cmd_unarchive(["s1", "--registry", str(reg)]) == 0
    t = json.loads(reg.read_text())["threads"][0]
    assert t.get("finishOnExit") is None
    assert t.get("finishedAt") is None


@pytest.mark.parametrize("command", [keep.cmd_keep, unarchive.cmd_unarchive])
@pytest.mark.parametrize("outcome", list(IndexUpdate))
def test_restore_reports_only_missing_picker_entries(
    tmp_path, monkeypatch, capsys, command, outcome
):
    reg, original = _setup(tmp_path)
    monkeypatch.setattr(lifecycle.scanner, "show_in_picker", lambda *_: outcome)
    args = ["s1", "--registry", str(reg)]
    if command is keep.cmd_keep:
        args.append("--yes")

    assert command(args) == 0

    output = capsys.readouterr()
    assert original.exists()
    if outcome is IndexUpdate.NO_ENTRY:
        assert "picker entry could not be updated" in output.err
    else:
        assert output.err == ""


def test_restore_refuses_when_a_companion_destination_exists_and_moves_nothing(tmp_path):
    reg, original = _setup(tmp_path)
    archived = tmp_path / "arc" / "proj" / "s1.jsonl"
    (archived.parent / "s1").mkdir()
    (archived.parent / "s1" / "tool.txt").write_text("archived")
    (original.parent / "s1").mkdir()
    r = _run("session", "unarchive", "s1", "--registry", str(reg))
    assert r.returncode == 1
    assert "already exists" in r.stderr
    assert (archived.is_file(), (archived.parent / "s1" / "tool.txt").is_file()) == (True, True)
