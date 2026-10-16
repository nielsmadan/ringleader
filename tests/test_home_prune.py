import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ringleader import decisions, registry
from ringleader.decisions import KeepOp
from ringleader.home.app import InboxApp
from ringleader.home.screens.prune import PruneScreen
from ringleader.sources.base import stat_fingerprint
from ringleader.tui.modals import ConfirmModal
from ringleader.tui.snapshot import screen_text
from scripts.tui_fixture import build_fixture
from textual.widgets import OptionList

from tui_helpers import until

NOW_DT = datetime(2026, 10, 15, 12, tzinfo=UTC)


def _iso(days):
    return (NOW_DT - timedelta(days=days)).isoformat()


def _transcript(sid):
    return Path(os.environ["SESSION_TRACKER_PROJECTS_DIR"]) / "-w-x" / f"{sid}.jsonl"


def _thread(sid, *, days, messages=3):
    path = _transcript(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{{"type":"user","message":{{"content":"hello from {sid}"}}}}\n')
    when = NOW_DT.timestamp() - 600
    os.utime(path, (when, when))
    return {
        "sessionId": sid,
        "source": "claude-code",
        "project": "/w/x",
        "title": f"title {sid}",
        "mtime": _iso(days),
        "messageCount": messages,
        "status": "unclassified",
        "decision": None,
        "file": str(path),
        "fileFingerprint": stat_fingerprint(path.stat()),
        "sizeBytes": path.stat().st_size,
    }


def _write(path):
    threads = [
        _thread("young", days=30),
        _thread("oldest", days=60),
        _thread("fresh", days=2),
        _thread("long", days=40, messages=30),
    ]
    reg = {**registry.empty_registry(), "threads": threads, "lastRoundupAt": _iso(0.1)}
    registry.save_registry(path, reg)
    return path


def _reload(path, now_iso):
    return registry.load_registry(path), None


def _app(path, **kwargs):
    return InboxApp(
        path,
        refresher=_reload,
        presence=frozenset,
        now=lambda: NOW_DT,
        models=lambda task: None,
        cwd=path.parent,
        strict=True,
        **kwargs,
    )


def _ids(app):
    rows = app.screen.query_one("#prune-rows", OptionList)
    return [rows.get_option_at_index(i).id for i in range(rows.option_count)]


def _toasts(app):
    return [n.message for n in app._notifications]


async def _open(app, pilot):
    await until(pilot, lambda: app.idle)
    await pilot.press("5")
    await until(pilot, lambda: isinstance(app.screen, PruneScreen))


async def _delete_all(app, pilot, answer):
    await pilot.press("D")
    await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
    await pilot.press(answer)
    await until(pilot, lambda: isinstance(app.screen, PruneScreen))


async def test_candidates_are_listed_oldest_first(tmp_path):
    app = _app(_write(tmp_path / "registry.json"))
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot)
        assert _ids(app) == ["claude-code:oldest", "claude-code:young"]
        text = screen_text(app)
        assert (
            "2 tiny sessions older than 7 days (fewer than 12 messages) — D to delete all" in text
        )
        assert "title oldest" in text


async def test_delete_all_yes_deletes_the_transcripts_and_the_rows_go(tmp_path):
    app = _app(_write(tmp_path / "registry.json"))
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot)
        await _delete_all(app, pilot, "y")
        await until(pilot, lambda: app.screen.result is not None)
        await until(pilot, lambda: app.idle)
        assert _ids(app) == []
        assert not _transcript("oldest").exists()
        assert not _transcript("young").exists()
        assert _transcript("long").exists()
        text = screen_text(app)
        assert "deleted 2, skipped 0" in text
        assert "Nothing to prune." in text


async def test_delete_all_no_deletes_nothing(tmp_path):
    app = _app(_write(tmp_path / "registry.json"))
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot)
        await _delete_all(app, pilot, "n")
        await until(pilot, lambda: app.idle)
        assert _transcript("oldest").exists()
        assert _transcript("young").exists()
        assert _ids(app) == ["claude-code:oldest", "claude-code:young"]


async def test_a_transcript_changed_after_listing_is_skipped_with_its_reason(tmp_path):
    app = _app(_write(tmp_path / "registry.json"))
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot)
        with _transcript("young").open("a", encoding="utf-8") as handle:
            handle.write('{"type":"assistant"}\n')
        await _delete_all(app, pilot, "y")
        await until(pilot, lambda: app.screen.result is not None)
        await until(pilot, lambda: app.idle)
        assert _transcript("young").exists()
        assert not _transcript("oldest").exists()
        assert _ids(app) == ["claude-code:young"]
        text = screen_text(app)
        assert "deleted 1, skipped 1" in text
        assert "title young: session changed since selection" in text


async def test_read_only_refuses_delete_all(tmp_path):
    app = _app(_write(tmp_path / "registry.json"), read_only=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot)
        await pilot.press("D")
        await until(pilot, lambda: "read-only: prune is disabled" in _toasts(app))
        assert isinstance(app.screen, PruneScreen)
        assert _transcript("oldest").exists()


async def test_golden_prune(tmp_path, golden):
    fixture = build_fixture(tmp_path, threads=40, now=NOW_DT)
    app = _app(fixture.registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot)
        golden("inbox/prune", screen_text(app), replace={str(tmp_path): "<root>"})


async def test_c_on_a_queued_prune_drops_it_and_says_so(tmp_path):
    path = _write(tmp_path / "registry.json")
    gate = threading.Event()

    def execute(op, ctx):
        if isinstance(op, KeepOp):
            gate.wait(10)
        return decisions.execute(op, ctx)

    app = _app(path, execute=execute)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _open(app, pilot)
            app.decide("keep", [registry.load_registry(path)["threads"][2]])
            await until(pilot, lambda: app._running_ticket is not None)
            await _delete_all(app, pilot, "y")
            await pilot.press("c")
            await until(
                pilot,
                lambda: "prune cancelled before it started — nothing was deleted" in _toasts(app),
            )
        finally:
            gate.set()
        await until(pilot, lambda: app.idle)
        assert _transcript("oldest").exists()
        assert _transcript("young").exists()
        assert _ids(app) == ["claude-code:oldest", "claude-code:young"]
