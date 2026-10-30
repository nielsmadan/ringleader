import json
import os
import subprocess
import sys
import textwrap
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from ringleader import refresh, resume, scanner
from ringleader.picker.app import (
    PREVIEW_CHARS,
    AgentChoice,
    AgentModal,
    ConfirmModal,
    PickerApp,
    PickerScreen,
    launchable_agents,
    row_text,
)
from ringleader.presence import PresenceUnavailable
from ringleader.resume import Scope
from ringleader.sources.base import SessionLaunchingSource
from ringleader.sources.claude_code import ClaudeCodeSource
from ringleader.sources.codex import CodexSource
from textual.app import App
from textual.geometry import Region
from textual.widgets import Input, Label, ListView, OptionList

from tui_helpers import until

CWD = "/work/repo"
NOW = "2026-05-05T00:00:00+00:00"
SCANNED = "2026-05-05T12:00:00+00:00"


@pytest.mark.parametrize("blocked", ["workspace", "refresh", "presence"])
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
        from ringleader.refresh import Refreshed

        path, blocked, action = sys.argv[1:]
        started = threading.Event()
        gate = threading.Event()

        def refresh(path, now):
            if blocked == "refresh":
                started.set()
                gate.wait()
            return json.loads(Path(path).read_text()), None

        def workspace(path, now, cwd, recursive):
            if blocked == "workspace":
                started.set()
                gate.wait()
            return Refreshed(json.loads(Path(path).read_text()), None, frozenset())

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

        app = PickerApp(path, "/work/repo", refresher=refresh,
                        workspace_refresher=workspace, presence=presence)
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


def _screen(app):
    return next(screen for screen in app.screen_stack if isinstance(screen, PickerScreen))


async def _settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _filtered(app, pilot):
    screen = _screen(app)
    filter_input = screen.query_one("#filter", Input)
    await until(pilot, lambda: screen._filter_timer is None and screen._query == filter_input.value)


def _ids(app):
    return [t["sessionId"] for t in _screen(app)._rows]


def _rendered(widget):
    strips = widget.render_lines(Region(0, 0, widget.size.width, widget.size.height))
    return " ".join(" ".join(strip.text for strip in strips).split())


def _row_labels(app):
    rows = _screen(app).query_one("#rows", OptionList)
    return [rows.render_line(index).text.strip() for index in range(rows.option_count)]


def _highlighted_id(app):
    return _screen(app)._rows[_screen(app).query_one("#rows", OptionList).highlighted]["sessionId"]


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
        rows = _screen(app).query_one("#rows", OptionList)
        assert rows.highlighted == 0
        await pilot.press("ctrl+j")
        assert rows.highlighted == 1
        await pilot.press("ctrl+k")
        assert rows.highlighted == 0


async def test_arrow_keys_move_the_highlight(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        rows = _screen(app).query_one("#rows", OptionList)
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
        await _filtered(app, pilot)
        filter_input = _screen(app).query_one("#filter", Input)
        rows = _screen(app).query_one("#rows", OptionList)
        assert filter_input.value == "a"
        assert len(_screen(app)._rows) > 1
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
        assert _screen(app)._scope is Scope.ALL
        assert _ids(app) == ["s3", "s0", "s1", "s2"]
        await pilot.press("a", "l")
        filter_input = _screen(app).query_one("#filter", Input)
        assert filter_input.value == "al"
        assert filter_input.cursor_position == 2
        await pilot.press("ctrl+a")
        assert filter_input.cursor_position == 2
        assert filter_input.value == "al"


async def test_ctrl_t_switches_to_recursive_scope_and_picks_up_a_subdirectory(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("ctrl+t")
        assert _screen(app)._scope is Scope.RECURSIVE
        assert _ids(app) == ["s0", "s1", "s2"]


async def test_typing_filters_the_rows_and_the_characters_land_in_the_input(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("b", "r", "a")
        await _filtered(app, pilot)
        assert _screen(app).query_one("#filter", Input).value == "bra"
        assert _ids(app) == ["s1"]


async def test_an_opening_query_filters_the_very_first_render(tmp_path):
    app = _app(tmp_path, query="bra")
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert _screen(app).query_one("#filter", Input).value == "bra"
        assert _ids(app) == ["s1"]


async def test_typing_then_enter_chooses_the_latest_match(tmp_path):
    app = _app(tmp_path, target="codex")
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("b", "r", "a", "enter")
    assert app.return_value.thread["sessionId"] == "s1"


async def test_enter_before_the_filter_settles_chooses_from_the_typed_query(tmp_path):
    app = _app(tmp_path, target="codex", filter_delay=60)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("b", "r", "a")
        assert _highlighted_id(app) == "s0"
        await pilot.press("enter")
    assert app.return_value.thread["sessionId"] == "s1"


async def test_a_burst_of_keystrokes_filters_once_on_the_final_query(tmp_path, monkeypatch):
    queries = []
    rank = resume.rank

    def recording(threads, query, *args):
        queries.append(query)
        return rank(threads, query, *args)

    monkeypatch.setattr(resume, "rank", recording)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        queries.clear()
        await pilot.press("b", "r", "a")
        await _filtered(app, pilot)
        assert queries == ["bra"]
        assert _ids(app) == ["s1"]


async def test_a_keystroke_that_keeps_the_same_results_leaves_the_rows_alone(tmp_path, monkeypatch):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        rows = _screen(app).query_one("#rows", OptionList)
        cleared = []
        clear_options = rows.clear_options

        def counting():
            cleared.append(_screen(app)._query)
            return clear_options()

        monkeypatch.setattr(rows, "clear_options", counting)
        await pilot.press("a")
        await _filtered(app, pilot)
        await pilot.press("l")
        await _filtered(app, pilot)
        assert _ids(app) == ["s0"]
        assert cleared == ["al"]


async def test_a_row_built_for_one_query_is_reused_by_the_next(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        rows = _screen(app).query_one("#rows", OptionList)
        before = rows.get_option_at_index(0)
        await pilot.press("a", "l")
        await _filtered(app, pilot)
        assert _ids(app) == ["s0"]
        assert rows.get_option_at_index(0) is before


async def test_each_thread_is_cleaned_for_matching_once_across_keystrokes(tmp_path, monkeypatch):
    built = []
    haystack = resume.haystack_of

    def counting(thread):
        built.append(thread["sessionId"])
        return haystack(thread)

    monkeypatch.setattr(resume, "haystack_of", counting)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        for key in "alph":
            await pilot.press(key)
            await _filtered(app, pilot)
        assert _ids(app) == ["s0"]
        assert sorted(built) == ["s0", "s1"]


async def test_a_render_resolves_each_agents_storage_layout_once(tmp_path, monkeypatch):
    made, passes = [], []
    source_by_name, slug_sources = scanner.source_by_name, resume.slug_sources
    fixed = datetime(2026, 5, 5, tzinfo=UTC)

    def counting_sources(name):
        made.append(name)
        return source_by_name(name)

    def counting_passes():
        passes.append(True)
        return slug_sources()

    monkeypatch.setattr(scanner, "source_by_name", counting_sources)
    monkeypatch.setattr(resume, "slug_sources", counting_passes)
    threads = [_thread(f"s{index}", title=f"session {index}") for index in range(50)]
    app = _app(tmp_path, threads, scope=Scope.ALL, now=lambda: fixed)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert len(_ids(app)) == 50
    assert len(passes) == 1
    assert sorted(made) == sorted(slug_sources())


async def test_row_ages_advance_with_the_clock_while_the_row_set_holds(tmp_path):
    clock = [datetime(2026, 5, 5, 0, 0, tzinfo=UTC)]
    app = _app(tmp_path, now=lambda: clock[0])
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        rows = screen.query_one("#rows", OptionList)
        before = str(rows.get_option_at_index(0).prompt)
        clock[0] += timedelta(days=30)
        await screen._render_rows()
        after = str(rows.get_option_at_index(0).prompt)
    assert before != after


async def test_a_rescan_returning_the_same_threads_keeps_the_row_cache(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        cache = screen._cache
        screen._adopt(_scanned(screen.registry_path), None, None)
        await pilot.pause()
        assert screen._cache is cache


async def test_a_rescan_returning_different_threads_replaces_the_row_cache(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        cache = screen._cache
        later = [_thread("s9", title="zulu")]
        screen._adopt(_scanned(screen.registry_path, later), None, None)
        await pilot.pause()
        assert screen._cache is not cache
        assert _ids(app) == ["s9"]


async def test_every_result_in_a_large_list_is_reachable(tmp_path):
    threads = [_thread(f"s{index}", title=f"session {index}") for index in range(2000)]
    app = _app(tmp_path, threads, query="session", target="claude-code")
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        rows = _screen(app).query_one("#rows", OptionList)
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
        await _filtered(app, pilot)
        assert _screen(app).query_one("#empty", Label).display
        await pilot.press("backspace", "backspace", "backspace")
        await _filtered(app, pilot)
        assert _ids(app) == ["s0", "s1"]
        assert _highlighted_id(app) == "s0"
        assert _screen(app).query_one("#rows", OptionList).display


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
        assert _screen(app).query_one("#rows", OptionList).option_count == 2
        assert "showing cached results" in _rendered(
            _screen(app).query_one("#refresh-status", Label)
        )
        gate.set()
        await _settle(app, pilot)
        assert _ids(app) == ["s0", "s1"]


async def test_the_scanned_registry_replaces_the_one_read_from_disk(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert _screen(app)._reg["lastScan"] == SCANNED
        assert _rendered(_screen(app).query_one("#refresh-status", Label)) == "Sessions refreshed"


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
        await _filtered(app, pilot)
        await pilot.press("ctrl+j")
        held = _highlighted_id(app)
        gate.set()
        await _settle(app, pilot)
        assert _screen(app).query_one("#filter", Input).value == "a"
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
        assert _screen(app)._current()["source"] == "codex"
        gate.set()
        await _settle(app, pilot)
        assert _screen(app)._current()["source"] == "codex"


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
        assert _rendered(_screen(app).query_one("#refresh-status", Label)) == (
            "Refresh failed — keeping displayed results; ctrl+r or F5 to retry"
        )
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


async def test_failed_presence_refresh_clears_previous_running_badges(tmp_path):
    available = True

    def presence():
        if not available:
            raise PresenceUnavailable("presence offline")
        return frozenset({("claude", "s0")})

    app = _app(tmp_path, presence=presence)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert "running" in _row_labels(app)[0]
        available = False
        await pilot.press("ctrl+r")
        await _settle(app, pilot)
        assert _ids(app) == ["s0", "s1"]
        assert all("running" not in label for label in _row_labels(app))
        available = True
        await pilot.press("f5")
        await _settle(app, pilot)
        assert "running" in _row_labels(app)[0]


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
        assert modal._choices == [
            AgentChoice("codex"),
            AgentChoice("codex", "fork"),
            AgentChoice("claude-code"),
            AgentChoice("droid"),
            AgentChoice("opencode"),
            AgentChoice("pi"),
        ]
        agents = modal.query_one("#agents", ListView)
        assert agents.index == 0
        first = _rendered(agents.children[0].query_one(Label))
        assert "codex" in first
        assert "(resume existing)" in first
        assert "(fork into new session)" in _rendered(agents.children[1].query_one(Label))


def test_the_handoff_agents_are_the_sources_that_declare_they_can_launch_one():
    declared = {
        source.name
        for source in (source_type() for source_type in scanner.SOURCE_TYPES)
        if scanner.supports(source, "launches_sessions", SessionLaunchingSource)
    }
    assert set(launchable_agents()) == declared
    assert {c.target for c in AgentModal(_thread("s0", source="codex"))._choices} == declared


def test_the_agent_modal_leaves_out_a_source_rl_cannot_launch():
    """The thread's own agent is the highlighted default, so offering one the command
    would refuse sends the user back with a flag for an agent it had just listed."""
    assert AgentModal(_thread("s0", source="mystery"))._choices == [
        AgentChoice(a) for a in launchable_agents()
    ]


def test_the_agent_modal_checks_both_halves_of_the_launch_capability(monkeypatch):
    """`scanner.supports` exists because a source can declare a capability it does not
    implement, or implement one it never declared. A real source name that fails either
    half must not become the highlighted default."""
    real = scanner.supports

    def only_declared(source, capability, protocol):
        return capability in source.capabilities

    monkeypatch.setattr(scanner, "supports", lambda s, c, p: real(s, c, p) and s.name != "codex")
    assert "codex" not in [c.target for c in AgentModal(_thread("s0", source="codex"))._choices]

    monkeypatch.setattr(scanner, "supports", only_declared)
    assert "codex" in [c.target for c in AgentModal(_thread("s0", source="codex"))._choices]


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
        await pilot.press("ctrl+j", "ctrl+j")
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
        await pilot.press("ctrl+j", "ctrl+j")
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
    """With `--to` set no modal is pushed, so both dispatches would otherwise run all the
    way to `exit`."""

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
        _screen(app).action_choose()
        _screen(app).action_choose()
        await pilot.pause()
        await app.workers.wait_for_complete()
    assert len(app.exits) == 1
    assert app.return_value.target == "codex"


@pytest.mark.parametrize("key", ["escape", "ctrl+c"])
async def test_escape_in_the_agent_modal_returns_to_the_list(tmp_path, key):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, AgentModal)
        await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, PickerScreen)
        assert app.is_running
        assert _screen(app).query_one("#rows", OptionList).highlighted == 0


async def test_the_scope_keys_stay_inert_while_the_agent_modal_is_open(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, AgentModal)
        await pilot.press("ctrl+a")
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert _screen(app)._scope is Scope.CWD
        assert _ids(app) == ["s0", "s1"]
        assert isinstance(app.screen, AgentModal)


@pytest.mark.parametrize("key", ["escape", "ctrl+c"])
async def test_escape_on_the_list_exits_without_a_choice(tmp_path, key):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press(key)
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


@pytest.mark.parametrize("key", ["n", "escape", "ctrl+c"])
async def test_declining_the_running_confirmation_returns_to_the_list(tmp_path, key):
    app = _app(tmp_path, presence=lambda: frozenset({("claude", "s0")}))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await _open_confirm(app, pilot)
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, PickerScreen)
        assert app.is_running
        assert _screen(app).query_one("#rows", OptionList).highlighted == 0
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


def test_a_row_carries_at_most_a_terminal_width_of_preview():
    thread = _thread("s0", title="alpha") | {"lastPrompt": "x" * 5000}

    text = row_text(thread, "2026-10-17T00:00:00+00:00").plain

    assert text.endswith("x" * PREVIEW_CHARS)
    assert len(text) < PREVIEW_CHARS + 100


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
        empty = _screen(app).query_one("#empty", Label)
        assert empty.display is True
        assert "ctrl+a" in _rendered(empty)


async def test_the_empty_state_offers_to_clear_the_filter_that_emptied_it(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("z", "q", "x")
        await _filtered(app, pilot)
        hint = _rendered(_screen(app).query_one("#empty", Label))
        assert "clear the filter" in hint
        assert "ctrl+a for everywhere" in hint


async def test_the_unfiltered_everywhere_scope_says_there_is_simply_nothing_yet(tmp_path):
    app = _app(tmp_path, [], scope=Scope.ALL)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        hint = _rendered(_screen(app).query_one("#empty", Label))
        assert hint == "nothing here — no sessions on this machine yet"


@pytest.mark.parametrize("source", ["claude-code", "codex", "droid", "opencode", "pi"])
@pytest.mark.parametrize("running", [False, True])
async def test_fork_choice_creates_a_new_session_without_reopening_confirmation(
    tmp_path, source, running
):
    live_agent = "claude" if source == "claude-code" else source
    app = _app(
        tmp_path,
        [_thread("s0", source=source)],
        presence=lambda: frozenset({(live_agent, "s0")}) if running else frozenset(),
    )
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
    assert app.return_value.thread["sessionId"] == "s0"
    assert app.return_value.target == source
    assert app.return_value.action == "fork"
    assert app.return_value.confirmed is False


class _Host(App[None]):
    def __init__(self, picker):
        super().__init__()
        self.picker = picker
        self.results = []

    def on_mount(self):
        self.push_screen(self.picker, callback=self.results.append)


def _hosted(tmp_path, **kwargs):
    reg_path = _seed(tmp_path, _default_threads())
    return _Host(
        PickerScreen(
            reg_path,
            CWD,
            refresher=lambda path, now_iso: (_scanned(path), None),
            presence=frozenset,
            **kwargs,
        )
    )


async def test_a_hosted_picker_dismisses_the_choice_back_to_its_host(tmp_path):
    app = _hosted(tmp_path, target="codex")
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: len(app.results) == 1)
        assert app.screen is app.default_screen
        assert app.is_running
    assert app.results[0].thread["sessionId"] == "s0"
    assert app.results[0].target == "codex"


async def test_escape_on_a_hosted_picker_returns_to_its_host(tmp_path):
    app = _hosted(tmp_path)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("escape")
        await until(pilot, lambda: app.results == [None])
        assert app.screen is app.default_screen
        assert app.is_running


def test_row_separates_start_from_last_activity():
    thread = _thread("s0", title="alpha", mtime="2026-10-23")
    thread["startedAt"] = "2026-10-16T00:00:00+00:00"
    text = row_text(thread, "2026-10-24T00:00:00+00:00").plain
    assert text[10:22].strip() == "1d ago"
    assert text[22:34].strip() == "8d ago"
    assert text[34:].startswith("alpha")


def test_row_shows_unknown_start_for_old_registry():
    text = row_text(_thread("s0"), NOW).plain
    assert text[22:34].strip() == "?"


async def test_default_refresh_reports_sources_that_still_use_cached_rows(tmp_path, monkeypatch):
    path = _seed(tmp_path, _default_threads())
    monkeypatch.setattr(
        refresh,
        "refresh_registry",
        lambda path, now, **kwargs: refresh.Refreshed(
            _scanned(path), None, frozenset({"opencode"})
        ),
    )
    app = PickerApp(path, CWD, presence=frozenset)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert _rendered(_screen(app).query_one("#refresh-status", Label)) == (
            "Partially refreshed — cached sources: opencode"
        )
        assert _ids(app) == ["s0", "s1"]


@pytest.mark.parametrize("persist", [False, True])
async def test_default_refresh_honors_registry_persistence(tmp_path, monkeypatch, persist):
    path = _seed(tmp_path, [])
    before = path.read_bytes()
    projects = tmp_path / "projects"
    workspace = projects / "-work-repo"
    workspace.mkdir(parents=True)
    source = ClaudeCodeSource(projects_dir=projects)
    monkeypatch.setattr(scanner, "active_sources", lambda **kwargs: [source])
    app = PickerApp(path, CWD, persist=persist, presence=frozenset)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        for key, sid in [("ctrl+r", "fork-one"), ("f5", "fork-two")]:
            (workspace / f"{sid}.jsonl").write_text(
                json.dumps({"sessionId": sid, "cwd": CWD, "type": "user"}) + "\n"
            )
            await pilot.press(key)
            await _settle(app, pilot)
            assert sid in _ids(app)
        if persist:
            saved = json.loads(path.read_bytes())
            assert {t["sessionId"] for t in saved["threads"]} == {"fork-one", "fork-two"}
        else:
            assert path.read_bytes() == before


async def test_dry_run_retry_retains_full_results_and_reuses_parsed_sessions(tmp_path, monkeypatch):
    path = _seed(tmp_path, [])
    before = path.read_bytes()
    projects = tmp_path / "projects"
    for sid, cwd, directory in [("local", CWD, "-work-repo"), ("remote", "/other", "-other")]:
        workspace = projects / directory
        workspace.mkdir(parents=True)
        (workspace / f"{sid}.jsonl").write_text(
            json.dumps({"sessionId": sid, "cwd": cwd, "type": "user"}) + "\n"
        )
    monkeypatch.setattr(
        scanner, "active_sources", lambda **kwargs: [ClaudeCodeSource(projects_dir=projects)]
    )
    parsed = []
    original_parse = ClaudeCodeSource.parse

    def parse(source, ref):
        parsed.append(ref.file)
        return original_parse(source, ref)

    monkeypatch.setattr(ClaudeCodeSource, "parse", parse)
    original_full = refresh.refresh_registry
    fail = False

    def full(*args, **kwargs):
        if fail:
            raise OSError("full refresh unavailable")
        return original_full(*args, **kwargs)

    monkeypatch.setattr(refresh, "refresh_registry", full)
    app = PickerApp(path, CWD, scope=Scope.ALL, persist=False, presence=frozenset)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert set(_ids(app)) == {"local", "remote"}
        screen = _screen(app)
        screen._list_view().highlighted = _ids(app).index("remote")
        fail = True
        await pilot.press("ctrl+r")
        await _settle(app, pilot)
        assert set(_ids(app)) == {"local", "remote"}
        assert screen._highlighted_id() == ("claude-code", "remote")
        assert _refresh_status(app).startswith("Refresh failed")
        assert len(parsed) == 2
        assert path.read_bytes() == before


async def test_default_picker_reuses_discovery_across_stages_and_refreshes(tmp_path, monkeypatch):
    from ringleader.sources import codex

    path = _seed(tmp_path, [])
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    def rollout(sid, cwd, source="cli"):
        file = sessions / f"{sid}.jsonl"
        file.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"session_id": sid, "cwd": cwd, "source": source},
                }
            )
            + "\n"
        )
        os.utime(file, (1, 1))
        return file

    rollout("local", CWD)
    outside = rollout("outside", "/other")
    background = rollout("background", CWD, "exec")
    refresh.refresh_registry(
        path, NOW, sources=[CodexSource(sessions_dir=sessions)], git_resolver=lambda cwd: None
    )
    monkeypatch.setattr(
        scanner, "active_sources", lambda **kwargs: [CodexSource(sessions_dir=sessions)]
    )
    reads = []
    original_header = codex._header

    def header(file):
        reads.append(file)
        return original_header(file)

    monkeypatch.setattr(codex, "_header", header)
    app = PickerApp(path, CWD, presence=frozenset)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert _ids(app) == ["local"]
        assert reads == [background]
        fork = rollout("fork", CWD)
        await pilot.press("ctrl+r")
        await _settle(app, pilot)
        assert set(_ids(app)) == {"local", "fork"}
        assert reads == [background, fork]
        rollout("outside", CWD)
        await pilot.press("f5")
        await _settle(app, pilot)
        assert set(_ids(app)) == {"local", "fork", "outside"}
        assert reads == [background, fork, outside]


async def test_refresh_errors_display_bracketed_text_literally(tmp_path):
    from ringleader.tui.snapshot import screen_text

    def fail(path, now):
        raise OSError("unreadable [/tmp/registry.json]")

    app = _app(tmp_path, refresher=fail)
    async with app.run_test(size=(100, 30), notifications=True) as pilot:
        await _settle(app, pilot)
        await until(pilot, lambda: "unreadable [/tmp/registry.json]" in screen_text(app))


@pytest.mark.parametrize("started_at", [None, 42, {}, "invalid"])
def test_row_shows_unknown_for_invalid_start(started_at):
    text = row_text({**_thread("s0"), "startedAt": started_at}, NOW).plain
    assert text[22:34].strip() == "?"


def _refresh_status(app):
    return _rendered(_screen(app).query_one("#refresh-status", Label))


@pytest.mark.parametrize("scope", [Scope.CWD, Scope.RECURSIVE, Scope.ALL])
async def test_workspace_results_arrive_before_the_full_scan(tmp_path, scope):
    gate = threading.Event()
    calls = []
    local = [*_default_threads(), _thread("fork", title="alpha fork")]

    def workspace(path, now, cwd, recursive):
        calls.append((cwd, recursive))
        return refresh.Refreshed(_scanned(path, local), None, frozenset())

    app = _app(
        tmp_path,
        scope=scope,
        workspace_refresher=workspace,
        refresher=_gated_refresher(gate, local),
    )
    async with app.run_test() as pilot:
        try:
            await until(pilot, lambda: "fork" in _ids(app))
            await pilot.pause()
            assert _refresh_status(app) == "Workspace results ready — refreshing all sessions…"
            assert calls == [(CWD, scope is Scope.RECURSIVE)]
            assert _screen(app)._refreshing
        finally:
            gate.set()
        await _settle(app, pilot)
        assert _refresh_status(app) == "Sessions refreshed"


@pytest.mark.parametrize("key", ["ctrl+r", "f5"])
async def test_manual_refresh_finds_a_new_session_and_preserves_query_and_selection(tmp_path, key):
    later = _default_threads()
    app = _app(tmp_path, refresher=lambda path, now: (_scanned(path, list(later)), None))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("a")
        await _filtered(app, pilot)
        await pilot.press("down")
        selected = _highlighted_id(app)
        later.append(_thread("fork", title="alpha fork"))
        await pilot.press(key)
        await until(pilot, lambda: "fork" in _ids(app))
        await _settle(app, pilot)
        assert _screen(app).query_one("#filter", Input).value == "a"
        assert _screen(app)._scope is Scope.CWD
        assert _highlighted_id(app) == selected


async def test_refresh_requests_coalesce_without_overlapping_scans(tmp_path):
    gate = threading.Event()
    calls = []

    def full(path, now):
        calls.append(len(calls) + 1)
        gate.wait(5)
        return _scanned(path), None

    app = _app(tmp_path, refresher=full)
    async with app.run_test() as pilot:
        try:
            await until(pilot, lambda: calls == [1])
            await pilot.press("ctrl+r", "f5", "ctrl+r")
            assert calls == [1]
            assert "refresh queued" in _refresh_status(app)
        finally:
            gate.set()
        await _settle(app, pilot)
        assert calls == [1, 2]
        assert _refresh_status(app) == "Sessions refreshed"


async def test_full_refresh_failure_keeps_workspace_results_and_can_be_retried(tmp_path):
    calls = []
    local = [*_default_threads(), _thread("fork")]

    def full(path, now):
        calls.append(now)
        if len(calls) == 1:
            raise OSError("offline")
        return _scanned(path, [*local, _thread("newer")]), None

    app = _app(
        tmp_path,
        refresher=full,
        workspace_refresher=lambda path, now, cwd, recursive: refresh.Refreshed(
            _scanned(path, local), None, frozenset()
        ),
    )
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert "fork" in _ids(app)
        assert _refresh_status(app) == (
            "Refresh failed — keeping displayed results; ctrl+r or F5 to retry"
        )
        await pilot.press("ctrl+r")
        await _settle(app, pilot)
        assert "newer" in _ids(app)
        assert _refresh_status(app) == "Sessions refreshed"


async def test_workspace_failure_still_runs_the_full_refresh(tmp_path):
    def workspace(*args):
        raise OSError("workspace unavailable")

    app = _app(tmp_path, workspace_refresher=workspace)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert _ids(app) == ["s0", "s1"]
        assert _refresh_status(app) == "Sessions refreshed"
        assert [n.message for n in app._notifications] == ["workspace unavailable"]


async def test_session_results_do_not_wait_for_presence(tmp_path):
    gate = threading.Event()

    def presence():
        gate.wait()
        return frozenset({("claude", "fork")})

    app = _app(
        tmp_path,
        presence=presence,
        refresher=lambda path, now: (_scanned(path, [_thread("fork")]), None),
    )
    async with app.run_test() as pilot:
        try:
            await until(pilot, lambda: _ids(app) == ["fork"])
            assert _refresh_status(app) == "Sessions refreshed"
        finally:
            gate.set()
        await _settle(app, pilot)
        assert "running" in _row_labels(app)[0]


@pytest.mark.parametrize("key", ["ctrl+r", "f5"])
async def test_refresh_keys_stay_inert_in_agent_and_confirmation_modals(tmp_path, key):
    calls = []

    def full(path, now):
        calls.append(now)
        return _scanned(path), None

    app = _app(tmp_path, refresher=full, presence=lambda: frozenset({("claude", "s0")}))
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.press("enter")
        assert isinstance(app.screen, AgentModal)
        await pilot.press(key)
        assert len(calls) == 1
        await pilot.press("enter")
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press(key)
        assert len(calls) == 1
        await pilot.press("escape")
        assert isinstance(app.screen, PickerScreen)
