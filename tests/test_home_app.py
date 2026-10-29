import json
import os
import subprocess
import sys
import textwrap
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ringleader import refresh, registry, revalidate
from ringleader.decisions import ArchiveOp, KeepOp, Outcome, ReloadOp
from ringleader.harvests import Harvest
from ringleader.home.app import InboxApp, ScanFailedMessage
from ringleader.home.harvest_view import harvest_groups
from ringleader.home.intents import AgentHandoffIntent, ResumeIntent
from ringleader.home.screens.home import HomeScreen
from ringleader.home.screens.projects import ProjectsScreen
from ringleader.home.screens.roundup import RoundupScreen
from ringleader.picker.app import Choice, PickerScreen
from ringleader.resume import launchable_agents
from ringleader.tui.modals import ConfirmModal, SnoozeModal
from ringleader.tui.snapshot import screen_text
from scripts.tui_fixture import build_fixture
from scripts.tui_shot import settle_inbox
from textual.widgets import Label, OptionList, Static

from tui_helpers import until

NOW_DT = datetime(2026, 10, 15, 12, tzinfo=UTC)
NOW = NOW_DT.isoformat()
REPO_ROOT = Path(__file__).resolve().parent.parent


def _iso(days):
    return (NOW_DT - timedelta(days=days)).isoformat()


def _thread(sid, *, days=1.0, project="/w/a", status="unclassified", **extra):
    return {
        "sessionId": sid,
        "source": "claude-code",
        "file": f"/nowhere/{sid}.jsonl",
        "project": project,
        "title": f"title {sid}",
        "mtime": _iso(days),
        "status": status,
        "decision": None,
        **extra,
    }


def _write(path, *threads, last_roundup=None):
    reg = {**registry.empty_registry(), "threads": list(threads), "lastRoundupAt": last_roundup}
    path.write_text(json.dumps(reg))
    return path


def _reload(path, now_iso):
    return registry.load_registry(path), None


class Gate:
    def __init__(self, result=None):
        self.release = threading.Event()
        self.calls = 0
        self.result = result

    def __call__(self, path, now_iso):
        self.calls += 1
        self.release.wait(10)
        return (self.result or registry.load_registry(path)), None


class FakeExecute:
    def __init__(self):
        self.gate = threading.Event()
        self.gate.set()
        self.started = []
        self.log = []

    def __call__(self, op, ctx):
        self.started.append(op)
        if not isinstance(op, ReloadOp):
            self.gate.wait(10)
        self.log.append(("finished", op))
        if isinstance(op, ReloadOp):
            return Outcome(ok=True, message="", wrote=False)
        return Outcome(ok=True, message=f"done {type(op).__name__}", wrote=True)


def _app(path, *, refresher=_reload, execute=None, cls=InboxApp, **kwargs):
    return cls(
        path,
        refresher=refresher,
        presence=frozenset,
        now=lambda: NOW_DT,
        models=lambda task: None,
        execute=execute or FakeExecute(),
        cwd=path.parent,
        strict=True,
        **kwargs,
    )


def _toasts(app):
    return [(n.message, n.severity) for n in app._notifications]


def _seen(thread):
    return revalidate.seen(thread)


def _exited(app):
    return app._exit or not app.is_running


async def test_home_renders_from_disk_while_the_scan_runs(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("a", days=2), _thread("b", days=3))
    gate = Gate()
    app = _app(path, refresher=gate)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: "triage 2" in screen_text(app))
            text = screen_text(app)
            assert "Next: Run the roundup (never run)" in text
            assert "scanning…" in text
            assert app.scans.running is True
        finally:
            gate.release.set()


async def test_a_scan_that_fails_outright_says_so_in_the_subtitle(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("a", days=2))
    gate = Gate()
    app = _app(path, refresher=gate)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: "triage 1" in screen_text(app))
            app.post_message(ScanFailedMessage("boom"))
            await until(pilot, lambda: app.sub_title == "scan failed")
        finally:
            gate.release.set()


async def test_a_landed_scan_updates_the_counts(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("a", days=2))
    scanned = {
        **registry.empty_registry(),
        "threads": [_thread("a", days=2), _thread("b", days=3), _thread("c", days=4)],
    }
    gate = Gate(result=scanned)
    app = _app(path, refresher=gate)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: "triage 1" in screen_text(app))
        gate.release.set()
        await until(pilot, lambda: "triage 3" in screen_text(app))
        assert "scanned just now" in screen_text(app)


async def test_a_scan_older_than_the_adopted_snapshot_is_dropped_for_a_reload(tmp_path):
    thread = _thread("a", days=2)
    path = _write(tmp_path / "registry.json", thread)
    stale_scan = {**registry.empty_registry(), "threads": [], "marker": "scan"}
    gate = Gate(result=stale_scan)
    execute = FakeExecute()
    app = _app(path, refresher=gate, execute=execute)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: gate.calls == 1)
            assert app.submit([ArchiveOp(_seen(thread))], label="archive") is True
            await until(pilot, lambda: app.state.snapshot.generation == 1)
        finally:
            gate.release.set()
        await until(pilot, lambda: app.idle)
        assert [type(op) for op in execute.started] == [ArchiveOp, ReloadOp]
        assert app.state.snapshot.generation == 1
        assert app.state.snapshot.reg.get("marker") is None


async def test_three_opens_the_hosted_picker_and_choosing_exits_with_a_resume_intent(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("a", days=2))
    gate = Gate()
    gate.release.set()
    app = _app(path, refresher=gate)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        await pilot.press("3")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        await until(pilot, lambda: app.screen.query_one("#rows", OptionList).option_count == 1)
        assert "esc back" in screen_text(app)
        await pilot.press("enter")
        await until(pilot, lambda: "resume with which agent?" in screen_text(app))
        await pilot.press("enter")
        await until(pilot, lambda: _exited(app))
    assert gate.calls == 1
    assert isinstance(app.return_value, ResumeIntent)
    assert (app.return_value.choice.thread["sessionId"], app.return_value.choice.target) == (
        "a",
        "claude-code",
    )


@pytest.mark.parametrize("key", ["escape", "ctrl+c"])
async def test_escape_returns_home_from_the_picker(tmp_path, key):
    path = _write(tmp_path / "registry.json", _thread("a", days=2))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        await pilot.press("3")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        await pilot.press(key)
        await until(pilot, lambda: isinstance(app.screen, HomeScreen))
        assert app.is_running


async def test_one_pops_back_to_home(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("a", days=2))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        await pilot.press("3")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        await pilot.press("escape", "4")
        await until(pilot, lambda: isinstance(app.screen, RoundupScreen))
        await pilot.press("3")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        app.screen.query_one("#rows", OptionList).focus()
        await pilot.press("1")
        await until(pilot, lambda: isinstance(app.screen, HomeScreen))
        assert app.screen_stack[1:] == [app.screen]


@pytest.mark.parametrize("key", ["q", "ctrl+c"])
async def test_q_when_idle_exits_with_nothing(tmp_path, key):
    path = _write(tmp_path / "registry.json", _thread("a", days=2))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        await pilot.press(key)
        await until(pilot, lambda: _exited(app))
    assert app.return_value is None


@pytest.mark.parametrize("key", ["q", "ctrl+c"])
async def test_q_while_a_change_is_written_waits_for_it_then_exits(tmp_path, key):
    thread = _thread("a", days=2)
    path = _write(tmp_path / "registry.json", thread)
    execute = FakeExecute()
    execute.gate.clear()
    app = _app(path, execute=execute)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: not app.scans.running)
            app.submit([ArchiveOp(_seen(thread))], label="archive")
            await until(pilot, lambda: len(execute.started) == 1)
            await pilot.press(key)
            await until(pilot, lambda: "finishing 1 change" in screen_text(app))
            assert app.is_running
            assert not app._exit
        finally:
            execute.gate.set()
        await until(pilot, lambda: app.idle)
        await until(pilot, lambda: _exited(app))
    assert app.return_value is None
    assert execute.log == [("finished", ArchiveOp(_seen(thread)))]


async def test_a_second_q_offers_to_discard_what_has_not_started(tmp_path):
    first, second = _thread("a", days=2), _thread("b", days=3)
    path = _write(tmp_path / "registry.json", first, second)
    execute = FakeExecute()
    execute.gate.clear()
    app = _app(path, execute=execute)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: not app.scans.running)
            app.submit([ArchiveOp(_seen(first))], label="archive")
            app.submit([KeepOp(_seen(second))], label="keep")
            await until(pilot, lambda: len(execute.started) == 1)
            await pilot.press("q")
            await until(pilot, lambda: "finishing 2 changes" in screen_text(app))
            await pilot.press("q")
            await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
            assert "Discard 1 queued change that have not started?" in screen_text(app)
            await pilot.press("y")
            await until(pilot, lambda: app.writer.unfinished == 1)
        finally:
            execute.gate.set()
        await until(pilot, lambda: _exited(app))
    assert execute.started == [ArchiveOp(_seen(first))]


async def test_exit_with_waits_for_the_writer_before_returning_the_intent(tmp_path):
    thread = _thread("a", days=2)
    path = _write(tmp_path / "registry.json", thread)
    execute = FakeExecute()
    execute.gate.clear()

    class Recording(InboxApp):
        def exit(self, result=None, return_code=0, message=None):
            execute.log.append(("exit", result))
            super().exit(result, return_code, message)

    app = _app(path, execute=execute, cls=Recording)
    intent = ResumeIntent(Choice(thread=thread, target="claude-code"))
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: not app.scans.running)
            app.submit([ArchiveOp(_seen(thread))], label="archive")
            await until(pilot, lambda: len(execute.started) == 1)
            app.exit_with(intent)
            await pilot.pause()
            assert app.is_running
        finally:
            execute.gate.set()
        await until(pilot, lambda: _exited(app))
    assert execute.log == [("finished", ArchiveOp(_seen(thread))), ("exit", intent)]
    assert app.return_value is intent


async def test_read_only_scans_never_write_and_submits_are_refused(tmp_path, monkeypatch):
    fixture = build_fixture(tmp_path / "world", threads=20, now=NOW_DT, transcripts=True)
    for key, value in fixture.env.items():
        monkeypatch.setenv(key, value)
    path = fixture.registry_path
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    app = InboxApp(
        path,
        presence=frozenset,
        now=lambda: NOW_DT + timedelta(hours=1),
        models=lambda task: None,
        read_only=True,
        cwd=tmp_path,
        strict=True,
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle and app.sub_title.startswith("scanned"), timeout=20)
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        thread = app.view_threads()[0]
        assert app.submit([KeepOp(_seen(thread))], label="keep") is False
        await until(pilot, lambda: ("read-only: keep is disabled", "warning") in _toasts(app))


async def test_a_stderr_print_from_a_thread_becomes_a_warning_toast(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("a", days=2))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        printer = threading.Thread(
            target=lambda: print("skipped codex: unreadable", file=sys.stderr), daemon=True
        )
        printer.start()
        printer.join()
        await until(pilot, lambda: ("skipped codex: unreadable", "warning") in _toasts(app))


async def test_golden_home(tmp_path, golden):
    fixture = build_fixture(tmp_path, threads=40, now=NOW_DT)
    reg = registry.load_registry(fixture.registry_path)
    labelled = [
        {**t, "gitProjectKey": Path(t["project"]).name, "gitProjectLabel": Path(t["project"]).name}
        for t in reg["threads"]
    ]
    registry.save_registry(fixture.registry_path, {**reg, "threads": labelled})
    app = _app(fixture.registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle_inbox(app, pilot)
        golden("inbox/home", screen_text(app), replace={str(tmp_path): "<root>"})


async def test_golden_home_caught_up(tmp_path, golden):
    path = _write(tmp_path / "registry.json", last_roundup=_iso(0.1))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle_inbox(app, pilot)
        text = screen_text(app)
        golden("inbox/home-caught-up", text)
    assert "All caught up — nothing waiting." in text


def test_quitting_while_the_scan_is_blocked_returns_promptly(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("a", days=2))
    script = textwrap.dedent(
        f"""
        import threading
        from ringleader.home.app import InboxApp, ScanFailedMessage

        blocked = threading.Event()

        def refresher(path, now_iso):
            blocked.wait()
            raise AssertionError("unreachable")

        async def pilot(pilot):
            await pilot.pause(0.2)
            await pilot.press("q")

        app = InboxApp(
            {str(path)!r},
            refresher=refresher,
            presence=frozenset,
            models=lambda task: None,
            strict=True,
        )
        result = app.run(headless=True, auto_pilot=pilot)
        print("exited", result, app.scans.running)
        """
    )
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    assert (done.returncode, done.stdout.strip().splitlines()[-1]) == (0, "exited None True")


async def test_the_hosted_picker_does_not_offer_a_session_with_a_queued_decision(tmp_path):
    thread = _thread("a", days=2)
    path = _write(tmp_path / "registry.json", thread, _thread("b", days=3))
    execute = FakeExecute()
    execute.gate.clear()
    app = _app(path, execute=execute)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: not app.scans.running)
            assert app.submit([ArchiveOp(_seen(thread))], label="archive") is True
            await pilot.press("3")
            await until(pilot, lambda: isinstance(app.screen, PickerScreen))
            rows = app.screen.query_one("#rows", OptionList)
            await until(pilot, lambda: rows.option_count > 0)
            assert rows.option_count == 1
        finally:
            execute.gate.set()
        await until(pilot, lambda: app.idle)


async def test_the_highlight_follows_its_row_when_a_scan_reorders_in_flight(tmp_path):
    older = _thread("b", days=3)
    path = _write(tmp_path / "registry.json", _thread("a", days=2), older)
    scanned = {
        **registry.empty_registry(),
        "threads": [_thread("z", days=0.1), _thread("a", days=2), older],
    }
    gate = Gate(result=scanned)
    app = _app(path, refresher=gate)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: "triage 2" in screen_text(app))
            in_flight = app.screen.query_one("#in-flight", OptionList)
            in_flight.focus()
            in_flight.highlighted = 1
            await until(pilot, lambda: in_flight.highlighted == 1)
            assert in_flight.highlighted_option.id == registry.qualified_id(older)
        finally:
            gate.release.set()
        await until(pilot, lambda: in_flight.option_count == 3)
        assert in_flight.highlighted_option.id == registry.qualified_id(older)


async def test_a_second_exit_choice_while_leaving_is_refused_with_a_toast(tmp_path):
    thread = _thread("a", days=2)
    path = _write(tmp_path / "registry.json", thread)
    execute = FakeExecute()
    execute.gate.clear()
    app = _app(path, execute=execute)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: app.idle)
            app.submit([ArchiveOp(_seen(thread))], label="archive")
            await pilot.press("q")
            await until(pilot, lambda: "finishing" in screen_text(app))
            app.exit_with(ResumeIntent(Choice(thread, "claude-code")))
            await until(
                pilot,
                lambda: ("leaving — that choice was not applied", "warning") in _toasts(app),
            )
        finally:
            execute.gate.set()
        await until(pilot, lambda: _exited(app))
    assert app.return_value is None


def _harvest(sid, folder):
    return Harvest(
        path=Path(f"/h/{sid}.md"),
        harvest_id=sid,
        digest="d",
        session_id=sid,
        name=sid,
        folder_path=folder,
        repo_url=None,
        clone_url=None,
        harvested_at=None,
        content="body",
    )


async def test_publishing_one_group_hands_off_in_its_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    folder = tmp_path / "proj"
    folder.mkdir()
    path = _write(tmp_path / "registry.json", _thread("a", days=2))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        items = (_harvest("h1", str(folder)),)
        app._set(harvests=items)
        app.start_publish_handoff(harvest_groups(items)[0])
        await until(pilot, lambda: "with which agent?" in screen_text(app))
        await pilot.press("enter")
        await until(pilot, lambda: _exited(app))
    intent = app.return_value
    assert isinstance(intent, AgentHandoffIntent)
    assert (intent.agent in launchable_agents(), intent.cwd, intent.purpose, intent.label) == (
        True,
        str(folder),
        "publish",
        "proj",
    )


async def test_publishing_everything_from_the_home_directory_asks_first(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    path = _write(tmp_path / "registry.json", _thread("a", days=2))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        app._set(harvests=(_harvest("h1", None),))
        app.start_publish_handoff(None)
        await until(pilot, lambda: "with which agent?" in screen_text(app))
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        assert "may touch any repository" in str(app.screen.query_one("#confirm-message").content)
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, HomeScreen))
        assert app.is_running
        app.start_publish_handoff(None)
        await until(pilot, lambda: "with which agent?" in screen_text(app))
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("y")
        await until(pilot, lambda: _exited(app))
    intent = app.return_value
    assert isinstance(intent, AgentHandoffIntent)
    assert (intent.cwd, intent.label) == (str(home), "all harvests")


async def test_enter_on_a_next_step_and_ctrl_r_while_scanning(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("a", days=2), last_roundup=_iso(0.1))
    gate = Gate()
    app = _app(path, refresher=gate)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: app.scans.running)
            await pilot.press("ctrl+r")
            await until(pilot, lambda: ("rescan queued", "information") in _toasts(app))
            assert "scanning…" in screen_text(app)
        finally:
            gate.release.set()
        await until(pilot, lambda: app.idle)
        assert "presence unknown" not in screen_text(app)


def _counting(monkeypatch, cls, name):
    calls = []
    original = getattr(cls, name)

    def counted(self, *args, **kwargs):
        calls.append(name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(cls, name, counted)
    return calls


async def test_a_rerender_resyncs_the_screen_only_after_the_state_changed(tmp_path, monkeypatch):
    path = _write(tmp_path / "registry.json", _thread("s1"))
    synced = _counting(monkeypatch, ProjectsScreen, "render_data")
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        await pilot.press("2")
        await until(pilot, lambda: isinstance(app.screen, ProjectsScreen))
        app._rerender()
        await until(pilot, lambda: app.idle)
        before = len(synced)
        app._rerender()
        await until(pilot, lambda: app.idle)
        unchanged = len(synced)
        app._set()
        app._rerender()
        await until(pilot, lambda: app.idle)
        assert (unchanged, len(synced)) == (before, before + 1)


async def test_home_state_is_reused_until_the_view_or_the_minute_changes(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("s1"), _thread("s2"))
    clock = [NOW_DT]
    app = InboxApp(
        path,
        refresher=_reload,
        presence=frozenset,
        now=lambda: clock[0],
        models=lambda task: None,
        execute=FakeExecute(),
        cwd=tmp_path,
        strict=True,
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        first = app.home_state()
        clock[0] = NOW_DT + timedelta(seconds=30)
        within_the_minute = app.home_state()
        clock[0] = NOW_DT + timedelta(minutes=1)
        next_minute = app.home_state()
        app.decide("keep", [app.view_threads()[0]])
        decided = app.home_state()
        assert (within_the_minute is first, next_minute is first, decided is next_minute) == (
            True,
            False,
            False,
        )
        assert decided.triage_total == next_minute.triage_total - 1


async def test_a_resize_rewraps_the_counts_without_rebuilding_home(tmp_path, monkeypatch):
    path = _write(tmp_path / "registry.json", _thread("s1"), _thread("s2", status="blocked"))
    rebuilt = _counting(monkeypatch, HomeScreen, "render_data")
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        before = len(rebuilt)
        await pilot.resize_terminal(30, 30)
        await until(pilot, lambda: "\n" in str(app.screen.query_one("#counts", Static).content))
        assert len(rebuilt) == before


async def test_hosted_picker_tracks_a_pending_scan_until_it_finishes(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("a"))
    gate = Gate()
    app = _app(path, refresher=gate)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await pilot.press("3")
            await until(pilot, lambda: isinstance(app.screen, PickerScreen))
            assert str(app.screen.query_one("#refresh-status", Label).render()) == (
                "Refreshing sessions… showing cached results"
            )
            gate.release.set()
            await until(
                pilot,
                lambda: (
                    str(app.screen.query_one("#refresh-status", Label).render())
                    == "Sessions refreshed"
                ),
            )
        finally:
            gate.release.set()


async def test_hosted_picker_reports_a_failed_inbox_scan(tmp_path):
    def fail(path, now):
        raise OSError("unreadable store")

    path = _write(tmp_path / "registry.json", _thread("a"))
    app = _app(path, refresher=fail)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        await pilot.press("3")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        await until(
            pilot,
            lambda: (
                str(app.screen.query_one("#refresh-status", Label).render())
                == "Refresh failed — showing cached results"
            ),
        )
        assert app.screen.query_one("#rows", OptionList).option_count == 1


async def test_hosted_picker_reports_unavailable_sources(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("a"))
    app = _app(path)
    app.scans._full_refresher = lambda path, now: refresh.Refreshed(
        registry.load_registry(path), None, frozenset({"opencode"})
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        await pilot.press("3")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        await until(
            pilot,
            lambda: (
                str(app.screen.query_one("#refresh-status", Label).render())
                == "Partially refreshed — cached sources: opencode"
            ),
        )


async def test_ctrl_c_cancels_a_dialog_with_focused_input(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("a", days=2))
    app = _app(path)
    results = []
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        modal = SnoozeModal(lambda: NOW_DT)
        app.push_screen(modal, callback=results.append)
        await until(pilot, lambda: app.screen is modal)
        await pilot.press("3", "d", "ctrl+c")
        await until(pilot, lambda: isinstance(app.screen, HomeScreen))
        assert results == [None]
        assert app.is_running
