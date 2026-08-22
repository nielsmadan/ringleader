import json
from datetime import UTC, datetime, timedelta

import pytest
from ringleader import harvests
from ringleader.config import Config
from ringleader.inbox.app import InboxApp, _today_str
from ringleader.models.base import ModelError
from textual.widgets import Label

pytestmark = pytest.mark.asyncio


def _fresh_iso() -> str:
    return datetime.now(UTC).isoformat()


def _stale_iso(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


class FakeModel:
    name = "fake"

    def __init__(self, out=None, boom=False):
        self._out = (
            out if out is not None else {"summary": "did the thing", "gotchas": ["watch it"]}
        )
        self._boom = boom

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        if self._boom:
            raise ModelError("boom")
        return self._out


def _write_transcript(path):
    path.write_text('{"type":"user","message":{"content":"hi"}}\n', encoding="utf-8")


def _seed(tmp_path, n=2, last_roundup_at="fresh"):
    reg_path = tmp_path / "registry.json"
    threads = []
    for i in range(n):
        f = tmp_path / f"s{i}.jsonl"
        _write_transcript(f)
        threads.append(
            {
                "sessionId": f"s{i}",
                "source": "claude-code",
                "file": str(f),
                "project": f"/repo{i}",
                "gitRoot": f"/repo{i}",
                "gitProjectLabel": f"repo{i}",
                "gitRemote": "git@github.com:me/repo.git",
                "gitBranch": "main",
                "title": f"session-{i}",
                "whatItIs": f"did thing {i}",
                "lastPrompt": "do the thing",
                "mtime": "2026-01-01T00:00:00+00:00",
                "sizeBytes": 100,
                "messageCount": 3,
                "status": "unfinished",
                "decision": None,
                "reviewedAtMtime": None,
                "snoozedAt": None,
                "snoozedUntil": None,
                "selectedForDay": None,
                "harvestPath": None,
            }
        )
    reg = {"version": 2, "threads": threads, "archived": [], "deleted": []}
    if last_roundup_at == "fresh":
        reg["lastRoundupAt"] = _fresh_iso()
    elif last_roundup_at is not None:
        reg["lastRoundupAt"] = last_roundup_at
    reg_path.write_text(json.dumps(reg), encoding="utf-8")
    return reg_path


def _setup(tmp_path, monkeypatch, n=2, last_roundup_at="fresh"):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reg_path = _seed(tmp_path, n=n, last_roundup_at=last_roundup_at)
    monkeypatch.setattr("ringleader.export.render_conversation", lambda p: "## USER\n\nhi\n")
    return reg_path


def _seed_tabs(tmp_path, monkeypatch):
    """One inbox candidate (s0), one selected-for-today (s1), one harvested-and-live
    with a matching harvest file already on disk (s2)."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr("ringleader.export.render_conversation", lambda p: "## USER\n\nhi\n")
    today = _today_str()

    def _thread(i, **overrides):
        f = tmp_path / f"s{i}.jsonl"
        _write_transcript(f)
        base = {
            "sessionId": f"s{i}",
            "source": "claude-code",
            "file": str(f),
            "project": f"/repo{i}",
            "gitRoot": f"/repo{i}",
            "gitProjectLabel": f"repo{i}",
            "gitRemote": "git@github.com:me/repo.git",
            "gitBranch": "main",
            "title": f"session-{i}",
            "whatItIs": f"did thing {i}",
            "lastPrompt": "do the thing",
            "mtime": "2026-01-01T00:00:00+00:00",
            "sizeBytes": 100,
            "messageCount": 3,
            "status": "unfinished",
            "decision": None,
            "reviewedAtMtime": None,
            "snoozedAt": None,
            "snoozedUntil": None,
            "selectedForDay": None,
            "harvestPath": None,
        }
        base.update(overrides)
        return base

    threads = [
        _thread(0),
        _thread(1, selectedForDay=today),
        _thread(2, decision="harvested", harvestPath=["dummy"]),
    ]
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": threads,
                "archived": [],
                "deleted": [],
                "lastRoundupAt": _fresh_iso(),
            }
        ),
        encoding="utf-8",
    )

    harvests.write_harvest(
        session_id="s2",
        name="session-2",
        folder_path="/repo2",
        repo_url="git@github.com:me/repo.git",
        harvested_at="2026-01-01T00:00:00+00:00",
        content="## Summary\n\nharvested digest preview\n",
    )
    return reg_path


async def test_mount_lists_live_undecided_threads(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test():
        assert len(app._inbox) == 2
        assert [t["sessionId"] for t in app._inbox] == ["s0", "s1"]


async def test_j_k_move_cursor(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        list_view = app._list_view()
        assert list_view.index == 0
        await pilot.press("j")
        assert list_view.index == 1
        await pilot.press("k")
        assert list_view.index == 0


async def test_delete_is_staged_and_undo_restores(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press("d")
        assert app._staged_deletes == {"s0"}
        assert len(app._inbox) == 1
        await pilot.press("u")
        assert app._staged_deletes == set()
        assert len(app._inbox) == 2
        assert [t["sessionId"] for t in app._inbox] == ["s0", "s1"]


async def test_snooze_invalid_duration_does_not_close(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        input_widget = app.screen.query_one("#snooze-input")
        input_widget.value = "banana"
        await pilot.press("enter")
        await pilot.pause()
        # still on the modal, and nothing removed
        assert len(app._inbox) == 2
        assert len(app.screen_stack) == 2


async def test_snooze_valid_duration_snoozes_and_removes(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        input_widget = app.screen.query_one("#snooze-input")
        input_widget.value = "3d"
        await pilot.press("enter")
        await pilot.pause()
        assert len(app._inbox) == 1
        assert [t["sessionId"] for t in app._inbox] == ["s1"]

    saved = json.loads(reg_path.read_text())
    t0 = next(t for t in saved["threads"] if t["sessionId"] == "s0")
    assert t0["snoozedUntil"]


async def test_enter_selects_for_today(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert len(app._inbox) == 1
        assert [t["sessionId"] for t in app._inbox] == ["s1"]

    saved = json.loads(reg_path.read_text())
    t0 = next(t for t in saved["threads"] if t["sessionId"] == "s0")
    assert t0["selectedForDay"] is not None


async def test_harvest_nonempty_digest_writes_harvest(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr("ringleader.inbox.app.active_model", lambda cfg: FakeModel())
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press("h")
        for _ in range(5):
            await pilot.pause()
        assert len(app._inbox) == 1

    saved = json.loads(reg_path.read_text())
    t0 = next(t for t in saved["threads"] if t["sessionId"] == "s0")
    assert t0["decision"] == "harvested"
    got = harvests.load_harvests()
    assert len(got) == 1
    assert "did the thing" in got[0].content


async def test_harvest_empty_digest_archives(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "ringleader.inbox.app.active_model",
        lambda cfg: FakeModel(out={"summary": "", "gotchas": []}),
    )
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press("h")
        for _ in range(5):
            await pilot.pause()
        assert len(app._inbox) == 1

    saved = json.loads(reg_path.read_text())
    assert [t["sessionId"] for t in saved["threads"]] == ["s1"]
    assert any(a["sessionId"] == "s0" for a in saved["archived"])


async def test_harvest_blank_summary_and_only_empty_list_items_archives(tmp_path, monkeypatch):
    # summary is blank and the only list item present is itself falsy ("") — _render_md
    # would filter it out via `[i for i in ... if i]`, so this must count as empty too,
    # not as a non-empty digest worth writing.
    reg_path = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "ringleader.inbox.app.active_model",
        lambda cfg: FakeModel(out={"summary": "", "gotchas": [""]}),
    )
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press("h")
        for _ in range(5):
            await pilot.pause()
        assert len(app._inbox) == 1

    saved = json.loads(reg_path.read_text())
    assert [t["sessionId"] for t in saved["threads"]] == ["s1"]
    assert any(a["sessionId"] == "s0" for a in saved["archived"])
    assert harvests.load_harvests() == []


async def test_harvest_no_model_leaves_row(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr("ringleader.inbox.app.active_model", lambda cfg: None)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press("h")
        await pilot.pause()
        assert len(app._inbox) == 2


async def test_exit_applies_staged_deletes(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch)
    app = InboxApp(registry_path=str(reg_path))
    session0_file = tmp_path / "s0.jsonl"
    async with app.run_test() as pilot:
        await pilot.press("d")
        assert app._staged_deletes == {"s0"}
        await pilot.press("q")
        await pilot.pause()

    assert not session0_file.exists()
    saved = json.loads(reg_path.read_text())
    assert [t["sessionId"] for t in saved["threads"]] == ["s1"]


async def test_detail_view_opens_and_h_pops_back(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press(";")
        await pilot.pause()
        assert len(app.screen_stack) == 2
        await pilot.press("h")
        await pilot.pause()
        assert len(app.screen_stack) == 1


async def test_tab_cycles_inbox_selected_harvesting(tmp_path, monkeypatch):
    reg_path = _seed_tabs(tmp_path, monkeypatch)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        assert app._active_tab == "inbox"
        assert [t["sessionId"] for t in app._inbox] == ["s0"]

        await pilot.press("tab")
        await pilot.pause()
        assert app._active_tab == "selected"
        assert [t["sessionId"] for t in app._selected] == ["s1"]
        assert "selected" in app.sub_title

        await pilot.press("tab")
        await pilot.pause()
        assert app._active_tab == "harvesting"
        assert [t["sessionId"] for t in app._harvesting] == ["s2"]
        assert "harvesting" in app.sub_title

        await pilot.press("tab")
        await pilot.pause()
        assert app._active_tab == "inbox"

        await pilot.press("shift+tab")
        await pilot.pause()
        assert app._active_tab == "harvesting"

        await pilot.press("shift+tab")
        await pilot.pause()
        assert app._active_tab == "selected"


async def test_harvesting_tab_shows_digest_preview(tmp_path, monkeypatch):
    reg_path = _seed_tabs(tmp_path, monkeypatch)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press("tab")
        await pilot.press("tab")
        await pilot.pause()
        list_view = app._list_view()
        label = list_view.children[0].query_one("Label").content
        assert "harvested digest preview" in str(label)


async def test_selected_tab_u_unselects_back_to_inbox(tmp_path, monkeypatch):
    reg_path = _seed_tabs(tmp_path, monkeypatch)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press("tab")
        await pilot.pause()
        assert app._active_tab == "selected"
        await pilot.press("u")
        await pilot.pause()
        assert app._selected == []

    saved = json.loads(reg_path.read_text())
    t1 = next(t for t in saved["threads"] if t["sessionId"] == "s1")
    assert t1["selectedForDay"] is None


async def test_selected_tab_enter_unselects_back_to_inbox(tmp_path, monkeypatch):
    reg_path = _seed_tabs(tmp_path, monkeypatch)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press("tab")
        await pilot.pause()
        assert app._active_tab == "selected"
        await pilot.press("enter")
        await pilot.pause()
        assert app._selected == []

    saved = json.loads(reg_path.read_text())
    t1 = next(t for t in saved["threads"] if t["sessionId"] == "s1")
    assert t1["selectedForDay"] is None


async def test_harvesting_tab_a_archives(tmp_path, monkeypatch):
    reg_path = _seed_tabs(tmp_path, monkeypatch)
    app = InboxApp(registry_path=str(reg_path))
    session2_file = tmp_path / "s2.jsonl"
    async with app.run_test() as pilot:
        await pilot.press("tab")
        await pilot.press("tab")
        await pilot.pause()
        assert app._active_tab == "harvesting"
        await pilot.press("a")
        await pilot.pause()
        assert app._harvesting == []

    assert not session2_file.exists()
    saved = json.loads(reg_path.read_text())
    assert [t["sessionId"] for t in saved["threads"]] == ["s0", "s1"]
    assert any(a["sessionId"] == "s2" for a in saved["archived"])


async def test_inbox_destructive_keys_do_not_fire_on_selected_tab(tmp_path, monkeypatch):
    reg_path = _seed_tabs(tmp_path, monkeypatch)
    monkeypatch.setattr("ringleader.inbox.app.active_model", lambda cfg: FakeModel())
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press("tab")
        await pilot.pause()
        assert app._active_tab == "selected"

        await pilot.press("d")
        await pilot.press("s")
        await pilot.press("h")
        await pilot.press(";")
        await pilot.press("a")
        for _ in range(3):
            await pilot.pause()

        assert app._staged_deletes == set()
        assert len(app.screen_stack) == 1
        assert [t["sessionId"] for t in app._selected] == ["s1"]

    saved = json.loads(reg_path.read_text())
    t1 = next(t for t in saved["threads"] if t["sessionId"] == "s1")
    assert t1["selectedForDay"] is not None
    assert t1["decision"] is None


async def test_inbox_destructive_keys_do_not_fire_on_harvesting_tab(tmp_path, monkeypatch):
    reg_path = _seed_tabs(tmp_path, monkeypatch)
    monkeypatch.setattr("ringleader.inbox.app.active_model", lambda cfg: FakeModel())
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press("tab")
        await pilot.press("tab")
        await pilot.pause()
        assert app._active_tab == "harvesting"

        await pilot.press("d")
        await pilot.press("s")
        await pilot.press("h")
        await pilot.press(";")
        await pilot.press("u")
        for _ in range(3):
            await pilot.pause()

        assert app._staged_deletes == set()
        assert len(app.screen_stack) == 1
        assert [t["sessionId"] for t in app._harvesting] == ["s2"]

    saved = json.loads(reg_path.read_text())
    assert [t["sessionId"] for t in saved["threads"]] == ["s0", "s1", "s2"]


# -- pre-flight roundup check -------------------------------------------------


async def test_preflight_absent_last_roundup_shows_confirm_prompt(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch, last_roundup_at=None)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.screen_stack) == 2
        msg = app.screen.query_one("#roundup-confirm-message", Label)
        assert "stale" in str(msg.content)
        assert "[y/n]" in str(msg.content)


async def test_preflight_stale_last_roundup_shows_confirm_prompt(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch, last_roundup_at=_stale_iso(hours=2))
    monkeypatch.setattr(
        "ringleader.inbox.app.config.load_config",
        lambda: Config(roundup_stale_hours=1),
    )
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.screen_stack) == 2


async def test_preflight_fresh_last_roundup_skips_prompt(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch, last_roundup_at=_stale_iso(hours=1))
    monkeypatch.setattr(
        "ringleader.inbox.app.config.load_config",
        lambda: Config(roundup_stale_hours=8),
    )
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert len(app._inbox) == 2


async def test_preflight_y_runs_roundup_and_notifies_then_shows_inbox(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch, last_roundup_at=None)
    calls: dict[str, list] = {"roundup": [], "notify": []}
    monkeypatch.setattr(
        "ringleader.inbox.app.roundup.cmd_roundup",
        lambda argv: calls["roundup"].append(argv) or 0,
    )
    monkeypatch.setattr(
        "ringleader.inbox.app.notify.notify",
        lambda title, message: calls["notify"].append((title, message)),
    )
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.screen_stack) == 2
        await pilot.press("y")
        for _ in range(10):
            await pilot.pause()
        assert len(app.screen_stack) == 1
        assert calls["roundup"] == [["--registry", str(reg_path), "--yes"]]
        assert calls["notify"] == [("ringleader", "roundup complete")]
        assert len(app._inbox) == 2


async def test_preflight_n_skips_roundup_and_shows_inbox(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch, last_roundup_at=None)
    calls: dict[str, list] = {"roundup": [], "notify": []}
    monkeypatch.setattr(
        "ringleader.inbox.app.roundup.cmd_roundup",
        lambda argv: calls["roundup"].append(argv) or 0,
    )
    monkeypatch.setattr(
        "ringleader.inbox.app.notify.notify",
        lambda title, message: calls["notify"].append((title, message)),
    )
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.screen_stack) == 2
        await pilot.press("n")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert calls["roundup"] == []
        assert calls["notify"] == []
        assert len(app._inbox) == 2


async def test_preflight_y_roundup_error_does_not_crash_and_shows_inbox(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch, last_roundup_at=None)

    def _boom(argv):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr("ringleader.inbox.app.roundup.cmd_roundup", _boom)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.screen_stack) == 2
        await pilot.press("y")
        for _ in range(10):
            await pilot.pause()
        assert app.is_running
        assert len(app.screen_stack) == 1
        assert len(app._inbox) == 2


# -- harvest error path -------------------------------------------------


async def test_harvest_render_conversation_error_leaves_row(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr("ringleader.inbox.app.active_model", lambda cfg: FakeModel())

    def _boom(path):
        raise OSError("transcript unreadable")

    monkeypatch.setattr("ringleader.export.render_conversation", _boom)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press("h")
        for _ in range(5):
            await pilot.pause()
        assert app.is_running
        assert len(app._inbox) == 2
        assert [t["sessionId"] for t in app._inbox] == ["s0", "s1"]

    saved = json.loads(reg_path.read_text())
    assert [t["sessionId"] for t in saved["threads"]] == ["s0", "s1"]


async def test_harvest_archive_collision_leaves_row(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "ringleader.inbox.app.active_model",
        lambda cfg: FakeModel(out={"summary": "", "gotchas": []}),
    )

    def _boom(reg, thread, archive_root_path):
        raise RuntimeError("archive name collision")

    monkeypatch.setattr("ringleader.inbox.app.archive_cmd.archive_thread", _boom)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press("h")
        for _ in range(5):
            await pilot.pause()
        assert app.is_running
        assert len(app._inbox) == 2

    saved = json.loads(reg_path.read_text())
    assert [t["sessionId"] for t in saved["threads"]] == ["s0", "s1"]


# -- tab leak through modals ---------------------------------------------


async def test_tab_does_not_switch_while_snooze_modal_open(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        assert len(app.screen_stack) == 2
        await pilot.press("tab")
        await pilot.pause()
        assert app._active_tab == "inbox"
        assert len(app.screen_stack) == 2


async def test_tab_does_not_switch_while_detail_modal_open(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.press(";")
        await pilot.pause()
        assert len(app.screen_stack) == 2
        await pilot.press("tab")
        await pilot.pause()
        assert app._active_tab == "inbox"
        assert len(app.screen_stack) == 2


# -- inbox zero ------------------------------------------------------------


async def test_empty_inbox_shows_placeholder(tmp_path, monkeypatch):
    reg_path = _setup(tmp_path, monkeypatch, n=0)
    app = InboxApp(registry_path=str(reg_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._inbox == []
        label = app.query_one("#empty-state", Label)
        assert label.display is True
        assert "inbox zero" in str(label.content)
