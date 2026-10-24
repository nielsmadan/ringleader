import io
import json
import os
import time

from ringleader import lifecycle
from ringleader.commands import sweep


def _world(tmp_path, *, mark, age_seconds):
    t = tmp_path / "t.jsonl"
    t.write_text('{"type":"user"}\n')
    when = time.time() - age_seconds
    os.utime(t, (when, when))
    thread = {
        "sessionId": "s1",
        "file": str(t),
        "project": str(tmp_path),
        "source": "claude-code",
        "decision": "harvested",
    }
    if mark:
        thread["finishOnExit"] = True
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 2, "threads": [thread], "archived": [], "deleted": []}))
    return reg, t


def test_due_selects_a_marked_and_quiet_thread(tmp_path):
    reg, _ = _world(tmp_path, mark=True, age_seconds=600)
    loaded = json.loads(reg.read_text())
    assert [t["sessionId"] for t in lifecycle.due(loaded, time.time(), grace_seconds=120)] == ["s1"]


def test_due_skips_a_marked_but_freshly_written_thread(tmp_path):
    reg, _ = _world(tmp_path, mark=True, age_seconds=5)
    loaded = json.loads(reg.read_text())
    assert lifecycle.due(loaded, time.time(), grace_seconds=120) == []


def test_due_skips_an_unmarked_thread(tmp_path):
    reg, _ = _world(tmp_path, mark=False, age_seconds=600)
    loaded = json.loads(reg.read_text())
    assert lifecycle.due(loaded, time.time(), grace_seconds=120) == []


def test_sweep_archives_a_due_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg, transcript = _world(tmp_path, mark=True, age_seconds=600)

    assert sweep.cmd_sweep(["--registry", str(reg)]) == 0

    out = json.loads(reg.read_text())
    assert out["threads"] == []
    assert [a["sessionId"] for a in out["archived"]] == ["s1"]
    assert not transcript.exists()


def test_sweep_is_a_noop_when_nothing_is_marked(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg, transcript = _world(tmp_path, mark=False, age_seconds=600)
    assert sweep.cmd_sweep(["--registry", str(reg)]) == 0
    assert transcript.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_session_form_ignores_the_grace_period(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg, transcript = _world(tmp_path, mark=True, age_seconds=0)

    assert sweep.cmd_sweep(["--session", "s1", "--registry", str(reg)]) == 0
    assert not transcript.exists()


def test_session_form_refuses_an_unmarked_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg, transcript = _world(tmp_path, mark=False, age_seconds=0)

    assert sweep.cmd_sweep(["--session", "s1", "--registry", str(reg)]) == 0
    assert transcript.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_session_form_is_silent_for_an_unknown_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg, transcript = _world(tmp_path, mark=True, age_seconds=600)

    assert sweep.cmd_sweep(["--session", "no-such-session", "--registry", str(reg)]) == 0
    assert transcript.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

    out = json.loads(reg.read_text())
    assert [t["sessionId"] for t in out["threads"]] == ["s1"]


def test_from_hook_archives_the_session_named_by_a_top_level_session_id(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg, transcript = _world(tmp_path, mark=True, age_seconds=0)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))

    assert sweep.cmd_sweep(["--from-hook", "--registry", str(reg)]) == 0
    assert not transcript.exists()


def test_from_hook_archives_the_session_named_by_a_camelcase_session_id(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg, transcript = _world(tmp_path, mark=True, age_seconds=0)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"sessionId": "s1"})))

    assert sweep.cmd_sweep(["--from-hook", "--registry", str(reg)]) == 0
    assert not transcript.exists()


def test_from_hook_ignores_a_nested_payload_session_id(tmp_path, monkeypatch, capsys):
    # A nested payload.session_id is deliberately not a recognised key: per
    # ringleader/sources/codex.py, that field names the *root* thread of a subagent
    # rollout, not the exiting session — trusting it could archive the wrong session.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg, transcript = _world(tmp_path, mark=True, age_seconds=0)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"payload": {"session_id": "s1"}})))

    assert sweep.cmd_sweep(["--from-hook", "--registry", str(reg)]) == 0
    assert transcript.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_from_hook_is_silent_on_unparseable_stdin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg, transcript = _world(tmp_path, mark=True, age_seconds=0)
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))

    assert sweep.cmd_sweep(["--from-hook", "--registry", str(reg)]) == 0
    assert transcript.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_from_hook_is_silent_on_empty_stdin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg, transcript = _world(tmp_path, mark=True, age_seconds=0)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    assert sweep.cmd_sweep(["--from-hook", "--registry", str(reg)]) == 0
    assert transcript.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_from_hook_is_silent_when_no_recognised_key_is_present(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg, transcript = _world(tmp_path, mark=True, age_seconds=0)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"reason": "quit"})))

    assert sweep.cmd_sweep(["--from-hook", "--registry", str(reg)]) == 0
    assert transcript.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_from_hook_is_silent_for_an_unknown_session_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg, transcript = _world(tmp_path, mark=True, age_seconds=0)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "no-such-session"})))

    assert sweep.cmd_sweep(["--from-hook", "--registry", str(reg)]) == 0
    assert transcript.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_from_hook_is_silent_for_an_unmarked_session_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg, transcript = _world(tmp_path, mark=False, age_seconds=0)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))

    assert sweep.cmd_sweep(["--from-hook", "--registry", str(reg)]) == 0
    assert transcript.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_sweep_persists_a_partial_success_when_one_thread_collides(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from ringleader.paths import archive_root

    when = time.time() - 600
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"type":"user"}\n')
    os.utime(bad, (when, when))
    good = tmp_path / "good.jsonl"
    good.write_text('{"type":"user"}\n')
    os.utime(good, (when, when))

    threads = [
        {
            "sessionId": "bad",
            "file": str(bad),
            "project": str(tmp_path),
            "source": "claude-code",
            "decision": "harvested",
            "finishOnExit": True,
        },
        {
            "sessionId": "good",
            "file": str(good),
            "project": str(tmp_path),
            "source": "claude-code",
            "decision": "harvested",
            "finishOnExit": True,
        },
    ]
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 2, "threads": threads, "archived": [], "deleted": []}))

    # provoke a real move collision for "bad" by pre-occupying its archive destination
    dest_dir = archive_root() / tmp_path.name
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "bad.jsonl").write_text("already here\n")

    assert sweep.cmd_sweep(["--registry", str(reg)]) == 1

    captured = capsys.readouterr()
    assert "bad" in captured.err

    out = json.loads(reg.read_text())
    assert [t["sessionId"] for t in out["threads"]] == ["bad"]
    assert [a["sessionId"] for a in out["archived"]] == ["good"]
    assert bad.exists()
    assert not good.exists()


def test_sweep_reports_an_oserror_instead_of_propagating_it(tmp_path, monkeypatch, capsys):
    """An OSError from the move must not escape and abort the rest of a roundup."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg, transcript = _world(tmp_path, mark=True, age_seconds=600)

    def boom(*_a, **_kw):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr("ringleader.lifecycle.shutil.move", boom)

    assert sweep.cmd_sweep(["--registry", str(reg)]) == 1
    captured = capsys.readouterr()
    assert "s1" in captured.err
    assert "Permission denied" in captured.err
    assert transcript.exists()
