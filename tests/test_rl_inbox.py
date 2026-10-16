import sys

import pytest
from ringleader import commands
from ringleader.commands import inbox as inbox_cmd
from ringleader.home.app import InboxApp
from ringleader.home.intents import ResumeIntent
from ringleader.home.writer import Writer
from ringleader.models import proc
from ringleader.picker.app import Choice


def _patch(monkeypatch, *, run):
    log = []
    monkeypatch.setattr(InboxApp, "run", lambda self, *a, **k: run(log))
    monkeypatch.setattr(
        Writer, "join_current", lambda self, timeout=None: log.append("join_current") or True
    )
    monkeypatch.setattr(proc, "kill_all_children", lambda: log.append("kill_all_children") or 0)
    return log


def _intent():
    return ResumeIntent(Choice(thread={"sessionId": "s0"}, target="claude-code"))


def test_resume_intent_calls_the_resume_command_with_the_registry(monkeypatch, tmp_path):
    log = _patch(monkeypatch, run=lambda log: _intent())
    calls = []

    def fake_resume(argv, **kwargs):
        calls.append((argv, kwargs["picker"]("a", "b", "c", "d", None)))
        return 7

    monkeypatch.setattr(commands.resume, "cmd_resume", fake_resume)

    assert inbox_cmd.cmd_inbox(["--registry", str(tmp_path / "r.json")]) == 7
    assert calls == [(["--registry", str(tmp_path / "r.json")], _intent().choice)]
    assert log == ["join_current", "kill_all_children"]


def test_read_only_resumes_as_a_dry_run(monkeypatch, tmp_path):
    _patch(monkeypatch, run=lambda log: _intent())
    calls = []
    monkeypatch.setattr(
        commands.resume, "cmd_resume", lambda argv, **kwargs: calls.append(argv) or 0
    )

    assert inbox_cmd.cmd_inbox(["--registry", str(tmp_path / "r.json"), "--read-only"]) == 0
    assert calls == [["--registry", str(tmp_path / "r.json"), "--dry-run"]]


def test_quitting_without_an_intent_exits_zero_and_launches_nothing(monkeypatch, tmp_path):
    _patch(monkeypatch, run=lambda log: None)
    monkeypatch.setattr(
        commands.resume,
        "cmd_resume",
        lambda *a, **k: pytest.fail("nothing should launch"),
    )

    assert inbox_cmd.cmd_inbox(["--registry", str(tmp_path / "r.json")]) == 0


def test_cleanup_runs_when_the_app_raises(monkeypatch, tmp_path):
    def boom(log):
        raise RuntimeError("boom")

    log = _patch(monkeypatch, run=boom)

    with pytest.raises(RuntimeError, match="boom"):
        inbox_cmd.cmd_inbox(["--registry", str(tmp_path / "r.json")])
    assert log == ["join_current", "kill_all_children"]


def test_a_crashed_app_exits_with_its_return_code(monkeypatch, tmp_path):
    log = _patch(monkeypatch, run=lambda log: log.append("run"))
    monkeypatch.setattr(InboxApp, "return_code", property(lambda self: 1))

    assert inbox_cmd.cmd_inbox(["--registry", str(tmp_path / "r.json")]) == 1
    assert log == ["run", "join_current", "kill_all_children"]


def test_abort_cancels_every_background_job(tmp_path):
    from ringleader.tui.background import CancelToken

    app = InboxApp(str(tmp_path / "r.json"), read_only=True)
    enrich, harvest = CancelToken(), CancelToken()
    app.roundups._enrich_job = enrich
    app.jobs._harvests[1] = harvest

    app.abort_background_work()

    assert (enrich.is_cancelled(), harvest.is_cancelled()) == (True, True)


def test_missing_textual_is_reported(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "textual", None)

    assert inbox_cmd.cmd_inbox([]) == 2
    assert "rl inbox needs Textual" in capsys.readouterr().out
