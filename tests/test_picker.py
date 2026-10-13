import json
import subprocess
import sys
import textwrap
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest
from ringleader import scanner
from ringleader.picker.app import (
    AgentModal,
    ConfirmModal,
    PickerApp,
    launchable_agents,
    row_text,
)
from ringleader.presence import PresenceUnavailable
from ringleader.resume import Scope
from ringleader.sources.base import SessionLaunchingSource
from textual.geometry import Region
from textual.widgets import Input, Label, ListView, OptionList

CWD = "/work/repo"
NOW = "2026-05-05T00:00:00+00:00"
SCANNED = "2026-05-05T12:00:00+00:00"


@pytest.mark.parametrize("blocked", ["refresh", "presence"])
@pytest.mark.parametrize("action", ["choose", "quit"])
def test_app_run_returns_while_background_work_is_blocked(tmp_path, blocked, action):
    reg_path = _seed(tmp_path, [_thread("s0", title="alpha")])
    script = textwrap.dedent("""
        import asyncio
        import json
        import sys
        import threading
        from pathlib import Path
        from ringleader.picker.app import PickerApp, AgentModal

        path, blocked, action = sys.argv[1:]
        started = threading.Event()
        gate = threading.Event()

        def refresh(path, now):
            if blocked == "refresh":
                started.set()
                gate.wait()
            return json.loads(Path(path).read_text()), None

        def presence():
            if blocked == "presence":
                started.set()
                gate.wait()
            return frozenset()

        async def choose(pilot):
            while not started.is_set():
                await asyncio.sleep(0.01)
            if action == "choose":
                await pilot.press("enter")
                assert isinstance(pilot.app.screen, AgentModal)
                await pilot.press("enter")
            else:
                await pilot.press("escape")

        app = PickerApp(path, "/work/repo", refresher=refresh, presence=presence)
        result = app.run(headless=True, auto_pilot=choose)
        if action == "choose":
            assert result.thread["sessionId"] == "s0"
            assert result.target == "claude-code"
        else:
            assert result is None
        print("returned")
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(reg_path), blocked, action],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.strip() == "returned"


def _thread(session_id, *, source="claude-code", project=CWD, title=None, mtime="2026-01-01"):
    return {
        "sessionId": session_id,
        "source": source,
        "file": f"/transcripts/{session_id}.jsonl",
        "project": project,
        "title": title or session_id,
        "aiTitle": None,
        "lastPrompt": f"last prompt for {session_id}",
        "summary": None,
        "mtime": f"{mtime}T00:00:00+00:00",
        "status": "unfinished",
        "decision": "keep",
    }


def _seed(tmp_path, threads):
    reg_path = tmp_path / "registry.json"
    reg = {"version": 2, "threads": threads, "archived": [], "deleted": []}
    reg_path.write_text(json.dumps(reg), encoding="utf-8")
    return reg_path


def _default_threads():
    return [
        _thread("s0", title="alpha", mtime="2026-03-03"),
        _thread("s1", source="codex", title="bravo", mtime="2026-02-02"),
        _thread("s2", project=f"{CWD}/sub", title="charlie", mtime="2026-01-01"),
        _thread("s3", source="pi", project="/elsewhere", title="delta", mtime="2026-04-04"),
    ]


def _scanned(path, threads=None):
    reg = json.loads(Path(path).read_text(encoding="utf-8"))
    if threads is not None:
        reg = {**reg, "threads": threads}
    return {**reg, "lastScan": SCANNED}


def _gated_refresher(gate, threads=None, note=None):
    """A scan the test releases by hand, so the window the picker opens in — rendered
    from disk, scan still running — can be inspected and acted on."""

    def refresher(path, now_iso):
        gate.wait(5)
        return _scanned(path, threads), note

    return refresher


def _app(tmp_path, threads=None, *, presence=None, refresher=None, **kwargs):
    reg_path = _seed(tmp_path, _default_threads() if threads is None else threads)

    return PickerApp(
        registry_path=str(reg_path),
        cwd=CWD,
        refresher=refresher or (lambda path, now_iso: (_scanned(path), None)),
        presence=presence or frozenset,
        **kwargs,
    )


async def _settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


def _ids(app):
    return [t["sessionId"] for t in app._rows]


def _rendered(widget):
    strips = widget.render_lines(Region(0, 0, widget.size.width, widget.size.height))
    return " ".join(" ".join(strip.text for strip in strips).split())


def _row_labels(app):
    rows = app.query_one("#rows", OptionList)
    return [rows.render_line(index).text.strip() for index in range(rows.option_count)]


def _highlighted_id(app):
    return app._rows[app.query_one("#rows", OptionList).highlighted]["sessionId"]


async def _open_confirm(app, pilot):
    await pilot.press("enter")
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    return _rendered(app.screen.query_one("#confirm-message", Label))


async def test_ctrl_j_and_ctrl_k_move_the_highlight(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        rows = app.query_one("#rows", OptionList)
        assert rows.highlighted == 0
        await pilot.press("ctrl+j")
        assert rows.highlighted == 1
        await pilot.press("ctrl+k")
        assert rows.highlighted == 0


async def test_arrow_keys_move_the_highlight(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        rows = app.query_one("#rows", OptionList)
        await pilot.press("down")
        assert rows.highlighted == 1
        await pilot.press("up")
        assert rows.highlighted == 0


async def test_ctrl_k_moves_the_highlight_and_leaves_the_typed_query_intact(tmp_path):
    """Input binds ctrl+k to delete_right_all, so without a priority binding the motion
    would silently eat the rest of the query."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("ctrl+a")
        await pilot.press("a")
        filter_input = app.query_one("#filter", Input)
        rows = app.query_one("#rows", OptionList)
        assert filter_input.value == "a"
        assert len(app._rows) > 1
        filter_input.cursor_position = 0
        await pilot.press("ctrl+j")
        assert rows.highlighted == 1
        await pilot.press("ctrl+k")
        assert filter_input.value == "a"
        assert rows.highlighted == 0


async def test_ctrl_a_switches_to_all_scope_without_moving_the_input_cursor(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert _ids(app) == ["s0", "s1"]
        await pilot.press("ctrl+a")
        assert app._scope is Scope.ALL
        assert _ids(app) == ["s3", "s0", "s1", "s2"]
        await pilot.press("a", "l")
        filter_input = app.query_one("#filter", Input)
        assert filter_input.value == "al"
        assert filter_input.cursor_position == 2
        await pilot.press("ctrl+a")
        assert filter_input.cursor_position == 2
        assert filter_input.value == "al"


async def test_ctrl_r_switches_to_recursive_scope_and_picks_up_a_subdirectory(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("ctrl+r")
        assert app._scope is Scope.RECURSIVE
        assert _ids(app) == ["s0", "s1", "s2"]


async def test_typing_filters_the_rows_and_the_characters_land_in_the_input(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("b", "r", "a")
        assert app.query_one("#filter", Input).value == "bra"
        assert _ids(app) == ["s1"]


async def test_an_opening_query_filters_the_very_first_render(tmp_path):
    app = _app(tmp_path, query="bra")
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert app.query_one("#filter", Input).value == "bra"
        assert _ids(app) == ["s1"]


async def test_typing_then_enter_chooses_the_latest_match(tmp_path):
    app = _app(tmp_path, target="codex")
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("b", "r", "a", "enter")
    assert app.return_value.thread["sessionId"] == "s1"


async def test_every_result_in_a_large_list_is_reachable(tmp_path):
    threads = [_thread(f"s{index}", title=f"session {index}") for index in range(2000)]
    app = _app(tmp_path, threads, query="session", target="claude-code")
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        rows = app.query_one("#rows", OptionList)
        assert rows.option_count == 2000
        rows.focus()
        await pilot.press("end")
        assert _highlighted_id(app) == "s1999"
        assert "session 1999" in _rendered(rows)
        await pilot.press("enter")
    assert app.return_value.thread["sessionId"] == "s1999"


async def test_clicking_a_result_selects_it_for_enter(tmp_path):
    app = _app(tmp_path, target="codex")
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.click("#rows", offset=(2, 1))
        assert _highlighted_id(app) == "s1"
        await pilot.press("enter")
    assert app.return_value.thread["sessionId"] == "s1"


async def test_cursor_movement_stops_at_the_first_and_last_result(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("up")
        assert _highlighted_id(app) == "s0"
        await pilot.press("down", "down")
        assert _highlighted_id(app) == "s1"


async def test_clearing_a_query_restores_results_after_no_matches(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("z", "q", "x")
        assert app.query_one("#empty", Label).display
        await pilot.press("backspace", "backspace", "backspace")
        assert _ids(app) == ["s0", "s1"]
        assert _highlighted_id(app) == "s0"
        assert app.query_one("#rows", OptionList).display


async def test_rows_are_ordered_most_recent_first_with_a_blank_query(tmp_path):
    app = _app(tmp_path, scope=Scope.ALL)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert _ids(app) == ["s3", "s0", "s1", "s2"]


# -- the instant open, and the scan that lands behind it ---------------------


async def test_the_rows_render_from_disk_before_the_background_scan_lands(tmp_path):
    gate = threading.Event()
    app = _app(tmp_path, refresher=_gated_refresher(gate))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert _ids(app) == ["s0", "s1"]
        assert app.query_one("#rows", OptionList).option_count == 2
        gate.set()
        await _settle(app, pilot)
        assert _ids(app) == ["s0", "s1"]


async def test_the_scanned_registry_replaces_the_one_read_from_disk(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert app._reg["lastScan"] == SCANNED


async def test_quitting_while_the_scan_is_still_running_exits_without_a_choice(tmp_path):
    gate = threading.Event()
    app = _app(tmp_path, refresher=_gated_refresher(gate))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        gate.set()
        await _settle(app, pilot)
    assert app.return_value is None


async def test_choosing_while_the_scan_is_still_running_exits_with_the_choice(tmp_path):
    gate = threading.Event()
    app = _app(tmp_path, refresher=_gated_refresher(gate))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        gate.set()
        await _settle(app, pilot)
    assert app.return_value.thread["sessionId"] == "s0"
    assert app.return_value.target == "claude-code"


async def test_a_scan_keeps_the_typed_query_and_the_highlighted_session(tmp_path):
    gate = threading.Event()
    later = [*_default_threads(), _thread("s9", title="alpaca", mtime="2026-06-06")]
    app = _app(tmp_path, refresher=_gated_refresher(gate, later))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("ctrl+j")
        held = _highlighted_id(app)
        gate.set()
        await _settle(app, pilot)
        assert app.query_one("#filter", Input).value == "a"
        assert "s9" in _ids(app)
        assert _highlighted_id(app) == held


async def test_a_scan_preserves_the_agent_when_session_ids_collide(tmp_path):
    gate = threading.Event()
    threads = [
        _thread("dup", title="claude side", mtime="2026-03-03"),
        _thread("dup", source="codex", title="codex side", mtime="2026-02-02"),
    ]
    app = _app(tmp_path, threads, refresher=_gated_refresher(gate, threads))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")
        assert app._current()["source"] == "codex"
        gate.set()
        await _settle(app, pilot)
        assert app._current()["source"] == "codex"


async def test_a_scan_that_drops_the_highlighted_session_falls_back_to_the_first_row(tmp_path):
    gate = threading.Event()
    later = [t for t in _default_threads() if t["sessionId"] != "s1"]
    app = _app(tmp_path, refresher=_gated_refresher(gate, later))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+j")
        assert _highlighted_id(app) == "s1"
        gate.set()
        await _settle(app, pilot)
        assert _ids(app) == ["s0"]
        assert _highlighted_id(app) == "s0"


async def test_a_recently_scanned_registry_is_still_scanned_again(tmp_path):
    """The session most worth resuming is the one just left, which a scan seconds old can
    already have missed. There is no manual-refresh key, so skipping the scan on a young
    `lastScan` would leave that session unreachable until something else rescanned."""
    scans = []

    def refresher(path, now_iso):
        scans.append(path)
        return _scanned(path), None

    reg_path = _seed(tmp_path, _default_threads())
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    reg_path.write_text(
        json.dumps({**reg, "lastScan": datetime.now(UTC).isoformat()}), encoding="utf-8"
    )
    app = PickerApp(registry_path=str(reg_path), cwd=CWD, refresher=refresher, presence=frozenset)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
    assert scans == [str(reg_path)]


async def test_the_scan_delta_note_is_announced(tmp_path):
    gate = threading.Event()
    gate.set()
    app = _app(tmp_path, refresher=_gated_refresher(gate, note="(scanning… 2 new)"))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert [n.message for n in app._notifications] == ["(scanning… 2 new)"]


async def test_a_scan_that_blows_up_warns_and_keeps_the_rows_read_from_disk(tmp_path):
    def boom(path, now_iso):
        raise OSError("the store is on fire")

    app = _app(tmp_path, refresher=boom)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert _ids(app) == ["s0", "s1"]
        assert [n.severity for n in app._notifications] == ["warning"]
        assert app.is_running


# -- presence ---------------------------------------------------------------


async def test_the_running_badge_tracks_what_presence_can_report(tmp_path):
    live = _app(tmp_path, presence=lambda: frozenset({("claude", "s0")}))
    async with live.run_test() as pilot:
        await _settle(live, pilot)
        labels = _row_labels(live)
        assert _ids(live) == ["s0", "s1"]
        assert len(labels) == 2
        assert "running" in labels[0]

    def boom():
        raise PresenceUnavailable("hooklinesinker is not installed")

    dark = _app(tmp_path, presence=boom)
    async with dark.run_test() as pilot:
        await _settle(dark, pilot)
        labels = _row_labels(dark)
        assert _ids(dark) == ["s0", "s1"]
        assert len(labels) == 2
        assert all("running" not in text for text in labels)


async def test_a_presence_probe_that_fails_outright_still_renders_every_row(tmp_path):
    def boom():
        raise OSError("hooklinesinker exploded")

    app = _app(tmp_path, presence=boom)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        labels = _row_labels(app)
        assert _ids(app) == ["s0", "s1"]
        assert all("running" not in text for text in labels)
        assert app.is_running


async def test_only_the_agent_presence_names_is_badged_when_two_sessions_share_an_id(tmp_path):
    threads = [
        _thread("dup", title="claude side", mtime="2026-03-03"),
        _thread("dup", source="codex", title="codex side", mtime="2026-02-02"),
    ]
    app = _app(tmp_path, threads, presence=lambda: frozenset({("codex", "dup")}))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        claude_row, codex_row = _row_labels(app)
        assert "claude side" in claude_row
        assert "codex side" in codex_row
        assert [text for text in (claude_row, codex_row) if "running" in text] == [codex_row]


# -- choosing ---------------------------------------------------------------


async def test_enter_opens_the_agent_modal_with_the_sessions_own_agent_first(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("ctrl+j")
        await pilot.press("enter")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, AgentModal)
        assert modal._agents == ["codex", "claude-code", "droid", "opencode", "pi"]
        agents = modal.query_one("#agents", ListView)
        assert agents.index == 0
        first = _rendered(agents.children[0].query_one(Label))
        assert "codex" in first
        assert "(original)" in first


def test_the_handoff_agents_are_the_sources_that_declare_they_can_launch_one():
    declared = {
        source.name
        for source in (source_type() for source_type in scanner.SOURCE_TYPES)
        if scanner.supports(source, "launches_sessions", SessionLaunchingSource)
    }
    assert set(launchable_agents()) == declared
    assert set(AgentModal(_thread("s0", source="codex"))._agents) == declared


def test_the_agent_modal_leaves_out_a_source_rl_cannot_launch():
    """The thread's own agent is the highlighted default, so offering one the command
    would refuse sends the user back with a flag for an agent it had just listed."""
    assert AgentModal(_thread("s0", source="mystery"))._agents == list(launchable_agents())


def test_the_agent_modal_checks_both_halves_of_the_launch_capability(monkeypatch):
    """`scanner.supports` exists because a source can declare a capability it does not
    implement, or implement one it never declared. A real source name that fails either
    half must not become the highlighted default."""
    real = scanner.supports

    def only_declared(source, capability, protocol):
        return capability in source.capabilities

    monkeypatch.setattr(scanner, "supports", lambda s, c, p: real(s, c, p) and s.name != "codex")
    assert "codex" not in AgentModal(_thread("s0", source="codex"))._agents

    monkeypatch.setattr(scanner, "supports", only_declared)
    assert "codex" in AgentModal(_thread("s0", source="codex"))._agents


async def test_the_choice_carries_the_thread_list_the_picker_converged_on(tmp_path):
    """Textual cancels the scan worker without joining its thread, so the caller cannot
    read that list back off disk — the picker has to hand it over."""
    gate = threading.Event()
    gate.set()
    later = [*_default_threads(), _thread("s9", title="alpaca", mtime="2026-06-06")]
    app = _app(tmp_path, refresher=_gated_refresher(gate, later))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert [t["sessionId"] for t in app.return_value.threads] == [t["sessionId"] for t in later]


async def test_a_choice_made_before_the_scan_lands_carries_no_thread_list(tmp_path):
    """An unscanned list is worse than nothing: it is the same snapshot the caller could
    read off disk, but being non-empty it suppresses that fallback — and the scan thread
    outlives the app, so by the time the caller looks, disk may already be better."""
    gate = threading.Event()
    app = _app(tmp_path, refresher=_gated_refresher(gate, _default_threads()))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    gate.set()
    assert app.return_value.threads == ()


async def test_choosing_the_sessions_own_agent_exits_without_a_handoff(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value.thread["sessionId"] == "s0"
    assert app.return_value.target == "claude-code"


async def test_choosing_another_agent_exits_as_a_handoff_to_that_agent(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("ctrl+j")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("ctrl+j")
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value.thread["sessionId"] == "s1"
    assert app.return_value.target == "claude-code"


async def test_handing_a_running_session_to_another_agent_needs_no_confirmation(tmp_path):
    app = _app(tmp_path, presence=lambda: frozenset({("claude", "s0")}))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert "running" in _row_labels(app)[0]
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("ctrl+j")
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value.thread["sessionId"] == "s0"
    assert app.return_value.target == "codex"
    assert app.return_value.confirmed is False


async def test_a_preset_target_resumes_without_asking_which_agent(tmp_path):
    app = _app(tmp_path, target="pi")
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value.thread["sessionId"] == "s0"
    assert app.return_value.target == "pi"


async def test_a_preset_target_that_is_the_sessions_own_agent_is_not_a_handoff(tmp_path):
    app = _app(tmp_path, target="claude-code")
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value.target == "claude-code"


async def test_a_preset_handoff_target_skips_the_running_confirmation(tmp_path):
    app = _app(tmp_path, target="codex", presence=lambda: frozenset({("claude", "s0")}))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert "running" in _row_labels(app)[0]
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value.target == "codex"
    assert app.return_value.confirmed is False


async def test_a_preset_same_agent_target_still_confirms_a_running_session(tmp_path):
    app = _app(tmp_path, target="claude-code", presence=lambda: frozenset({("claude", "s0")}))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        assert "alpha is running right now" in _rendered(
            app.screen.query_one("#confirm-message", Label)
        )
        await pilot.press("y")
        await pilot.pause()
    assert app.return_value.target == "claude-code"
    assert app.return_value.confirmed is True


async def test_two_choices_dispatched_at_once_exit_the_app_once(tmp_path):
    """With `--to` set no modal is pushed, so `check_action` never sees a second screen
    and both dispatches would otherwise run all the way to `exit`."""

    class Recording(PickerApp):
        exits: ClassVar[list] = []

        def exit(self, result=None, return_code=0, message=None):
            self.exits.append(result)
            super().exit(result, return_code, message)

    Recording.exits = []
    reg_path = _seed(tmp_path, _default_threads())
    app = Recording(
        str(reg_path),
        CWD,
        target="codex",
        refresher=lambda path, now_iso: (_scanned(path), None),
        presence=frozenset,
    )
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        app.action_choose()
        app.action_choose()
        await pilot.pause()
        await app.workers.wait_for_complete()
    assert len(app.exits) == 1
    assert app.return_value.target == "codex"


async def test_escape_in_the_agent_modal_returns_to_the_list(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, AgentModal)
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert app.is_running
        assert app.query_one("#rows", OptionList).highlighted == 0


async def test_the_scope_keys_stay_inert_while_the_agent_modal_is_open(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, AgentModal)
        await pilot.press("ctrl+a")
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app._scope is Scope.CWD
        assert _ids(app) == ["s0", "s1"]
        assert isinstance(app.screen, AgentModal)


async def test_escape_on_the_list_exits_without_a_choice(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_value is None
    assert app.is_running is False


# -- the running-session confirmation ---------------------------------------


async def test_a_running_session_is_badged_and_confirmed_before_it_is_resumed(tmp_path):
    app = _app(tmp_path, presence=lambda: frozenset({("claude", "s0")}))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert "running" in _row_labels(app)[0]
        message = await _open_confirm(app, pilot)
        assert isinstance(app.screen, ConfirmModal)
        assert "alpha is running right now" in message
        await pilot.press("y")
        await pilot.pause()
    assert app.return_value.thread["sessionId"] == "s0"
    assert app.return_value.confirmed is True


@pytest.mark.parametrize("key", ["n", "escape"])
async def test_declining_the_running_confirmation_returns_to_the_list(tmp_path, key):
    app = _app(tmp_path, presence=lambda: frozenset({("claude", "s0")}))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await _open_confirm(app, pilot)
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press(key)
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert app.is_running
        assert app.query_one("#rows", OptionList).highlighted == 0
    assert app.return_value is None


async def test_the_confirmation_names_a_bracketed_session_verbatim(tmp_path):
    app = _app(
        tmp_path,
        [_thread("s0", title="[cadet-city] work")],
        presence=lambda: frozenset({("claude", "s0")}),
    )
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        message = await _open_confirm(app, pilot)
        assert "[cadet-city] work" in message


async def test_the_confirmation_survives_a_session_named_like_a_closing_markup_tag(tmp_path):
    app = _app(
        tmp_path,
        [_thread("s0", title="[/] leftover")],
        presence=lambda: frozenset({("claude", "s0")}),
    )
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        message = await _open_confirm(app, pilot)
        assert "[/] leftover" in message
        await pilot.press("y")
        await pilot.pause()
    assert app.return_value.thread["sessionId"] == "s0"


# -- row rendering ----------------------------------------------------------


async def test_a_bracketed_session_name_reaches_the_row_verbatim(tmp_path):
    app = _app(tmp_path, [_thread("s0", title="[cadet-city] work")])
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert "[cadet-city] work" in _row_labels(app)[0]


async def test_a_row_previews_the_last_prompt(tmp_path):
    app = _app(tmp_path, [_thread("s0", title="alpha")])
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert "last prompt for s0" in _row_labels(app)[0]


async def test_a_row_previews_the_summary_when_there_is_no_last_prompt(tmp_path):
    thread = _thread("s0", title="alpha") | {"lastPrompt": None, "summary": "distilled summary"}
    app = _app(tmp_path, [thread])
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert "distilled summary" in _row_labels(app)[0]


async def test_a_preview_too_long_for_the_row_ends_in_an_ellipsis(tmp_path):
    thread = _thread("s0", title="alpha") | {"lastPrompt": "verbose " * 40}
    app = _app(tmp_path, [thread])
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        label = _row_labels(app)[0]
        assert "alpha" in label
        assert label.endswith("…")


async def test_a_wide_terminal_shows_the_whole_preview_the_row_has_space_for(tmp_path):
    preview = "verbose " * 15
    thread = _thread("s0", title="alpha") | {"lastPrompt": preview}
    app = _app(tmp_path, [thread])
    async with app.run_test(size=(200, 24)) as pilot:
        await _settle(app, pilot)
        assert preview.strip() in _row_labels(app)[0]


async def test_a_hand_edited_mtime_still_leaves_every_row_on_screen(tmp_path):
    threads = [_thread("s0", title="alpha"), _thread("s1", title="bravo") | {"mtime": "gibberish"}]
    app = _app(tmp_path, threads)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        labels = " ".join(_row_labels(app))
        assert sorted(_ids(app)) == ["s0", "s1"]
        assert "alpha" in labels
        assert "bravo" in labels


def test_row_text_renders_a_thread_whose_every_field_is_none():
    thread = {
        "sessionId": "s0",
        "source": None,
        "title": None,
        "aiTitle": None,
        "lastPrompt": None,
        "summary": None,
        "mtime": None,
    }
    text = str(row_text(thread, NOW))
    assert "claude" in text
    assert "never" in text
    assert "s0" in text


def test_row_text_renders_an_mtime_it_cannot_parse():
    thread = {"sessionId": "s0", "source": "codex", "title": "badtime", "mtime": "gibberish"}
    assert "?" in str(row_text(thread, NOW))
    assert "never" in str(row_text({**thread, "mtime": 12345}, NOW))


# -- the empty state --------------------------------------------------------


async def test_the_empty_state_names_the_key_that_widens_the_search(tmp_path):
    app = _app(tmp_path, [_thread("s9", project="/elsewhere")])
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        empty = app.query_one("#empty", Label)
        assert empty.display is True
        assert "ctrl+a" in _rendered(empty)


async def test_the_empty_state_offers_to_clear_the_filter_that_emptied_it(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("z", "q", "x")
        await pilot.pause()
        hint = _rendered(app.query_one("#empty", Label))
        assert "clear the filter" in hint
        assert "ctrl+a for everywhere" in hint


async def test_the_unfiltered_everywhere_scope_says_there_is_simply_nothing_yet(tmp_path):
    app = _app(tmp_path, [], scope=Scope.ALL)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        hint = _rendered(app.query_one("#empty", Label))
        assert hint == "nothing here — no sessions on this machine yet"
