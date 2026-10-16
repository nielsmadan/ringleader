import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ringleader import decisions, harvests, registry
from ringleader.decisions import ReloadOp
from ringleader.home.app import NOTHING_TO_HARVEST, InboxApp
from ringleader.home.intents import ResumeIntent
from ringleader.home.screens.detail import DetailScreen
from ringleader.home.screens.home import HomeScreen
from ringleader.home.screens.projects import ProjectsScreen
from ringleader.home.screens.triage import TriageScreen
from ringleader.presence import PresenceUnavailable
from ringleader.sources.base import stat_fingerprint
from ringleader.tui.modals import ConfirmModal, SnoozeModal
from ringleader.tui.snapshot import screen_text
from scripts.tui_fixture import build_fixture
from textual.widgets import Label, OptionList

from tui_helpers import until

NOW_DT = datetime(2026, 10, 15, 12, tzinfo=UTC)
NOW = NOW_DT.isoformat()


def _iso(days):
    return (NOW_DT - timedelta(days=days)).isoformat()


def _transcript(sid, project_slug="-w-x"):
    path = Path(os.environ["SESSION_TRACKER_PROJECTS_DIR"]) / project_slug / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{{"type":"user","message":{{"content":"fix the parser in {sid}"}}}}\n')
    when = time.time() - 600
    os.utime(path, (when, when))
    return path


def _thread(sid, *, days=1.0, project="/w/x", source="claude-code", **extra):
    if source == "claude-code":
        transcript = _transcript(sid, project.replace("/", "-"))
        file_fields = {
            "file": str(transcript),
            "fileFingerprint": stat_fingerprint(transcript.stat()),
            "sizeBytes": transcript.stat().st_size,
        }
    else:
        file_fields = {"file": None}
    return {
        "sessionId": sid,
        "source": source,
        "project": project,
        "title": f"title {sid}",
        "mtime": _iso(days),
        "messageCount": 3,
        "status": "unclassified",
        "decision": None,
        **file_fields,
        **extra,
    }


def _append(thread):
    with open(thread["file"], "a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant"}\n')


def _write(path, *threads):
    reg = {**registry.empty_registry(), "threads": list(threads), "lastRoundupAt": _iso(0.1)}
    registry.save_registry(path, reg)
    return path


def _reload(path, now_iso):
    return registry.load_registry(path), None


class GatedExecute:
    def __init__(self, *, gated=False):
        self.gate = threading.Event()
        if not gated:
            self.gate.set()
        self.started = []
        self.log = []

    def __call__(self, op, ctx):
        if not isinstance(op, ReloadOp):
            self.started.append(op)
            self.gate.wait(10)
        outcome = decisions.execute(op, ctx)
        if not isinstance(op, ReloadOp):
            self.log.append(("finished", type(op).__name__))
        return outcome


class FakeModel:
    name = "claude"
    model = "fake"

    def __init__(self, answer, gate=None):
        self.answer = answer
        self.gate = gate
        self.calls = 0

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(10)
        return self.answer


ANSWER = {"summary": "fixed the parser", "gotchas": ["watch the cache"], "decisions": []}
EMPTY = {"summary": " ", "gotchas": [], "decisions": [], "whatChanged": []}


def _app(path, *, execute=None, presence=frozenset, model=None, cls=InboxApp, **kwargs):
    return cls(
        path,
        refresher=_reload,
        presence=presence,
        now=lambda: NOW_DT,
        models=lambda task: model if task == "harvest" else None,
        execute=execute or GatedExecute(),
        cwd=path.parent,
        strict=True,
        **kwargs,
    )


def _toasts(app):
    return [n.message for n in app._notifications]


def _rows(app):
    return app.screen.query_one("#rows", OptionList)


def _ids(app):
    rows = _rows(app)
    return [rows.get_option_at_index(i).id for i in range(rows.option_count)]


def _stored(path):
    reg = registry.load_registry(path)
    return {t["sessionId"]: t for t in reg["threads"]}, reg


def _exited(app):
    return app._exit or not app.is_running


async def _open_triage(app, pilot):
    await until(pilot, lambda: app.idle)
    await pilot.press("2")
    await until(pilot, lambda: isinstance(app.screen, ProjectsScreen))
    await pilot.press("enter")
    await until(pilot, lambda: isinstance(app.screen, TriageScreen))


async def _snooze(pilot, app):
    await pilot.press("s")
    await until(pilot, lambda: isinstance(app.screen, SnoozeModal))
    await pilot.press("3", "d", "enter")


def _archived(path, sid):
    threads, reg = _stored(path)
    return sid not in threads and any(a["sessionId"] == sid for a in reg["archived"])


async def _press(pilot, app, key):
    if key == "s":
        await _snooze(pilot, app)
    else:
        await pilot.press(key)


@pytest.mark.parametrize(
    ("key", "check"),
    [
        ("a", lambda path: _archived(path, "s1")),
        ("g", lambda path: _stored(path)[0]["s1"]["reviewedAtMtime"] == _iso(5)),
        ("K", lambda path: _stored(path)[0]["s1"]["decision"] == "keep"),
        ("s", lambda path: _stored(path)[0]["s1"]["snoozedUntil"].startswith("2026-10-18")),
        ("d", lambda path: any(d["sessionId"] == "s1" for d in _stored(path)[1]["deleted"])),
    ],
)
async def test_each_key_hides_its_row_before_the_writer_runs(tmp_path, key, check):
    path = _write(tmp_path / "registry.json", _thread("s1", days=5), _thread("s2", days=4))
    execute = GatedExecute(gated=True)
    app = _app(path, execute=execute, delete_grace=0)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _open_triage(app, pilot)
            assert _ids(app) == ["claude-code:s1", "claude-code:s2"]
            await _press(pilot, app, key)
            await until(pilot, lambda: _ids(app) == ["claude-code:s2"])
            assert execute.log == []
        finally:
            execute.gate.set()
        await until(pilot, lambda: app.idle)
        assert check(path)
        assert _ids(app) == ["claude-code:s2"]


async def test_three_rapid_keep_goings_survive_a_scan_landing_in_between(tmp_path):
    threads = [_thread(f"s{i}", days=5 - i) for i in range(3)]
    path = _write(tmp_path / "registry.json", *threads, _thread("s9", days=0.5))
    execute = GatedExecute(gated=True)
    app = _app(path, execute=execute)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _open_triage(app, pilot)
            await pilot.press("g", "g", "g")
            await until(pilot, lambda: _ids(app) == ["claude-code:s9"])
            adopted = app._state.snapshot.taken_at
            app.rescan()
            await until(pilot, lambda: app._state.snapshot.taken_at != adopted)
            assert _ids(app) == ["claude-code:s9"]
        finally:
            execute.gate.set()
        await until(pilot, lambda: app.idle)
        assert _ids(app) == ["claude-code:s9"]
        stored, _ = _stored(path)
        assert [stored[f"s{i}"]["reviewedAtMtime"] for i in range(3)] == [
            t["mtime"] for t in threads
        ]


async def test_a_transcript_appended_after_render_brings_the_row_back(tmp_path):
    thread = _thread("s1", days=5)
    path = _write(tmp_path / "registry.json", thread)
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        _append(thread)
        await pilot.press("a")
        await until(pilot, lambda: any("changed since you looked" in m for m in _toasts(app)))
        await until(pilot, lambda: app.idle)
        assert _ids(app) == ["claude-code:s1"]
        assert Path(thread["file"]).exists()


async def test_presence_unknown_refuses_the_archive_and_the_row_comes_back(tmp_path):
    thread = _thread("s1", days=5)
    path = _write(tmp_path / "registry.json", thread)

    def unknown():
        raise PresenceUnavailable("not installed")

    app = _app(path, presence=unknown)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("a")
        await until(pilot, lambda: any("can't tell whether" in m for m in _toasts(app)))
        await until(pilot, lambda: app.idle)
        assert _ids(app) == ["claude-code:s1"]
        assert "s1" in _stored(path)[0]
        assert Path(thread["file"]).exists()


async def test_an_opencode_row_is_refused_synchronously(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("o1", days=5, source="opencode"))
    execute = GatedExecute()
    app = _app(path, execute=execute)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("a")
        await until(
            pilot, lambda: any("no filesystem transcript lifecycle" in m for m in _toasts(app))
        )
        assert _ids(app) == ["opencode:o1"]
        assert execute.started == []


async def test_a_bulk_archive_skips_a_refused_session_and_applies_the_rest(tmp_path):
    path = _write(
        tmp_path / "registry.json",
        _thread("s1", days=5),
        _thread("o1", days=4, source="opencode"),
        _thread("s2", days=3),
    )
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("asterisk", "a")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("y")
        await until(pilot, lambda: _ids(app) == ["opencode:o1"])
        await until(pilot, lambda: app.idle)
        assert [_archived(path, sid) for sid in ("s1", "s2")] == [True, True]
        assert any("no filesystem transcript lifecycle" in m for m in _toasts(app))
        assert app.decided_count == 2


async def test_a_refused_decision_keeps_the_marks(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("s1", days=5), _thread("s2", days=4))
    app = _app(path, read_only=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("asterisk", "K")
        await until(pilot, lambda: any("read-only" in m for m in _toasts(app)))
        assert app.screen.marks == {"claude-code:s1", "claude-code:s2"}


async def test_a_refused_archive_does_not_count_as_decided(tmp_path):
    thread = _thread("s1", days=5)
    path = _write(tmp_path / "registry.json", thread)
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        _append(thread)
        await pilot.press("a")
        await until(pilot, lambda: any("changed since you looked" in m for m in _toasts(app)))
        await until(pilot, lambda: app.idle)
        assert app.decided_count == 0
        assert "0 decided this session" in screen_text(app)


async def test_a_decision_key_after_the_highlighted_row_vanished_asks_again(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("s1", days=5), _thread("s2", days=4))
    execute = GatedExecute()
    app = _app(path, execute=execute)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        reg = registry.load_registry(path)
        registry.save_registry(
            path, {**reg, "threads": [t for t in reg["threads"] if t["sessionId"] != "s1"]}
        )
        app.rescan()
        await until(pilot, lambda: _ids(app) == ["claude-code:s2"])
        await pilot.press("a")
        await until(
            pilot, lambda: "the list changed under you — press the key again" in _toasts(app)
        )
        assert execute.started == []
        await pilot.press("a")
        await until(pilot, lambda: _ids(app) == [])
        await until(pilot, lambda: _archived(path, "s2"))


async def test_undo_within_the_grace_brings_a_delete_back(tmp_path):
    thread = _thread("s1", days=5)
    path = _write(tmp_path / "registry.json", thread)
    execute = GatedExecute()
    app = _app(path, execute=execute, delete_grace=30)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("d", "u")
        await until(pilot, lambda: _ids(app) == ["claude-code:s1"])
        assert "deleting title s1 in 30 s — u to undo" in _toasts(app)
        assert "undone: delete title s1" in _toasts(app)
        await until(pilot, lambda: app.idle)
        assert execute.started == []
        assert Path(thread["file"]).exists()


async def test_undo_mid_batch_reports_what_was_already_applied(tmp_path):
    path = _write(
        tmp_path / "registry.json",
        _thread("s1", days=5),
        _thread("s2", days=4),
        _thread("s3", days=3),
    )
    execute = GatedExecute(gated=True)
    app = _app(path, execute=execute)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _open_triage(app, pilot)
            await pilot.press("asterisk", "a")
            await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
            await pilot.press("y")
            await until(pilot, lambda: len(execute.started) == 1)
            await pilot.press("u")
            await until(pilot, lambda: any(m.startswith("undone:") for m in _toasts(app)))
        finally:
            execute.gate.set()
        await until(pilot, lambda: app.idle)
        assert "undone: 2 of archive 3 sessions (1 already applied)" in _toasts(app)
        assert [_archived(path, sid) for sid in ("s1", "s2", "s3")] == [True, False, False]


async def test_a_delete_runs_once_the_grace_runs_out(tmp_path):
    thread = _thread("s1", days=5)
    path = _write(tmp_path / "registry.json", thread)
    app = _app(path, delete_grace=0.3)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("d")
        await until(pilot, lambda: _ids(app) == [])
        await until(pilot, lambda: not Path(thread["file"]).exists())
        await until(pilot, lambda: app.idle)
        assert "s1" not in _stored(path)[0]


async def test_harvest_writes_the_harvest_and_archives(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("s1", days=5))
    model = FakeModel(ANSWER)
    app = _app(path, model=model)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("h")
        await until(pilot, lambda: _archived(path, "s1"))
        await until(pilot, lambda: app.idle)
    [saved] = harvests.load_harvests()
    assert "fixed the parser" in saved.content
    assert model.calls == 1


async def test_harvest_still_completes_after_leaving_the_screen(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("s1", days=5))
    gate = threading.Event()
    app = _app(path, model=FakeModel(ANSWER, gate=gate))
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _open_triage(app, pilot)
            await pilot.press("h")
            await until(pilot, lambda: "harvesting title s1…" in screen_text(app))
            await pilot.press("escape")
            await until(pilot, lambda: isinstance(app.screen, ProjectsScreen))
        finally:
            gate.set()
        await until(pilot, lambda: _archived(path, "s1"))
        await until(pilot, lambda: app.idle)
    assert len(harvests.load_harvests()) == 1


async def test_quitting_during_a_harvest_still_saves_it_then_exits(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("s1", days=5))
    gate = threading.Event()
    app = _app(path, model=FakeModel(ANSWER, gate=gate))
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _open_triage(app, pilot)
            await pilot.press("h")
            await until(pilot, lambda: "harvesting title s1…" in screen_text(app))
            await pilot.press("q")
            await until(pilot, lambda: "finishing 1 change" in screen_text(app))
            assert app.is_running
        finally:
            gate.set()
        await until(pilot, lambda: _exited(app))
    assert _archived(path, "s1")
    assert len(harvests.load_harvests()) == 1


async def test_discarding_during_a_harvest_drops_the_late_answer(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("s1", days=5))
    gate = threading.Event()
    app = _app(path, model=FakeModel(ANSWER, gate=gate))
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _open_triage(app, pilot)
            await pilot.press("h")
            await until(pilot, lambda: "harvesting title s1…" in screen_text(app))
            await pilot.press("q", "q")
            await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
            await pilot.press("y")
        finally:
            gate.set()
        await until(pilot, lambda: _exited(app))
    assert harvests.load_harvests() == []
    assert not _archived(path, "s1")


async def test_an_empty_harvest_answer_toasts_and_the_row_returns(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("s1", days=5))
    execute = GatedExecute()
    app = _app(path, execute=execute, model=FakeModel(EMPTY))
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("h")
        await until(pilot, lambda: NOTHING_TO_HARVEST in _toasts(app))
        await until(pilot, lambda: app.idle)
        assert _ids(app) == ["claude-code:s1"]
        assert execute.started == []


async def test_harvest_without_a_model_is_refused(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("s1", days=5))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("h")
        await until(pilot, lambda: any("no harvest model configured" in m for m in _toasts(app)))
        assert _ids(app) == ["claude-code:s1"]


@pytest.mark.parametrize(("answer", "archived"), [("y", True), ("n", False)])
async def test_marking_two_rows_then_archive_confirms_with_the_count(tmp_path, answer, archived):
    path = _write(
        tmp_path / "registry.json",
        _thread("s1", days=5),
        _thread("s2", days=4),
        _thread("s3", days=3),
    )
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("space", "down", "space", "a")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        assert "Archive 2 sessions? [y/n]" in screen_text(app)
        await pilot.press(answer)
        await until(pilot, lambda: isinstance(app.screen, TriageScreen))
        await until(pilot, lambda: app.idle)
        assert [_archived(path, sid) for sid in ("s1", "s2", "s3")] == [archived, archived, False]
        assert app.screen.marks == (
            frozenset() if archived else frozenset({"claude-code:s1", "claude-code:s2"})
        )


def _confirm_message(app):
    return str(app.screen.query_one("#confirm-message", Label).content)


def _deleted(path, sid):
    return any(d["sessionId"] == sid for d in _stored(path)[1]["deleted"])


@pytest.mark.parametrize(("answer", "deleted"), [("y", True), ("n", False)])
async def test_marking_two_rows_then_delete_confirms_with_the_count(tmp_path, answer, deleted):
    threads = [_thread("s1", days=5), _thread("s2", days=4), _thread("s3", days=3)]
    path = _write(tmp_path / "registry.json", *threads)
    app = _app(path, delete_grace=0)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("space", "down", "space", "d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        assert _confirm_message(app) == "Delete 2 sessions permanently? Nothing is harvested. [y/n]"
        await pilot.press(answer)
        await until(pilot, lambda: isinstance(app.screen, TriageScreen))
        await until(pilot, lambda: app.idle)
        assert [_deleted(path, sid) for sid in ("s1", "s2", "s3")] == [deleted, deleted, False]
        assert [Path(t["file"]).exists() for t in threads] == [not deleted, not deleted, True]


async def test_confirm_each_delete_asks_before_a_single_delete(tmp_path, monkeypatch):
    monkeypatch.setattr("ringleader.home.screens.triage.CONFIRM_EACH_DELETE", True)
    thread = _thread("s1", days=5)
    path = _write(tmp_path / "registry.json", thread)
    app = _app(path, delete_grace=0)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        assert _confirm_message(app) == "Delete title s1 permanently? Nothing is harvested. [y/n]"
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, TriageScreen))
        await until(pilot, lambda: app.idle)
        assert Path(thread["file"]).exists()
        assert _ids(app) == ["claude-code:s1"]


async def test_star_marks_every_row_and_again_clears(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("s1", days=5), _thread("s2", days=4))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("asterisk")
        assert app.screen.marks == {"claude-code:s1", "claude-code:s2"}
        assert screen_text(app).count("● ") == 2
        await pilot.press("A")
        assert app.screen.marks == frozenset()


async def test_bulk_harvest_is_refused(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("s1", days=5), _thread("s2", days=4))
    app = _app(path, model=FakeModel(ANSWER))
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("asterisk", "h")
        await until(pilot, lambda: "harvest works on one session at a time" in _toasts(app))
        assert _ids(app) == ["claude-code:s1", "claude-code:s2"]


async def test_a_bulk_archive_returns_the_changed_session_with_its_reason(tmp_path):
    first, changed = _thread("s1", days=5), _thread("s2", days=4)
    path = _write(tmp_path / "registry.json", first, changed, _thread("s3", days=3))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("space", "down", "space")
        _append(changed)
        await pilot.press("a")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("y")
        await until(pilot, lambda: "title s2: changed since you looked" in _toasts(app))
        await until(pilot, lambda: app.idle)
        assert _archived(path, "s1")
        assert _ids(app) == ["claude-code:s2", "claude-code:s3"]


async def test_enter_opens_the_detail_and_the_conversation_appears(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("s1", days=5))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, DetailScreen))
        await until(pilot, lambda: "fix the parser in s1" in screen_text(app))
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, TriageScreen))


async def test_detail_wraps_a_long_conversation_line_across_screen_lines(tmp_path):
    thread = _thread("s1", days=5)
    words = " ".join(f"word{k:02d}" for k in range(40))
    Path(thread["file"]).write_text(
        f'{{"type":"user","message":{{"content":"{words}"}}}}\n', encoding="utf-8"
    )
    path = _write(tmp_path / "registry.json", thread)
    app = _app(path)
    async with app.run_test(size=(60, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, DetailScreen))
        await until(pilot, lambda: "word39" in screen_text(app))
        rows = [row for row in screen_text(app).splitlines() if "word" in row]
        assert len(rows) > 1
        assert max(len(row) for row in rows) <= 60
        assert " ".join(" ".join(rows).split()) == words


async def test_detail_says_when_the_conversation_is_unavailable(tmp_path):
    thread = _thread("s1", days=5)
    path = _write(tmp_path / "registry.json", thread)
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        Path(thread["file"]).unlink()
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, DetailScreen))
        await until(
            pilot, lambda: "conversation unavailable: transcript not found" in screen_text(app)
        )


async def test_resume_waits_for_a_pending_archive_before_exiting(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("s1", days=5), _thread("s2", days=4))
    execute = GatedExecute(gated=True)

    class Recording(InboxApp):
        def exit(self, result=None, return_code=0, message=None):
            execute.log.append(("exit", type(result).__name__))
            super().exit(result, return_code, message)

    app = _app(path, execute=execute, cls=Recording)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _open_triage(app, pilot)
            await pilot.press("a")
            await until(pilot, lambda: _ids(app) == ["claude-code:s2"])
            await pilot.press("r")
            await until(pilot, lambda: "resume with which agent?" in screen_text(app))
            await pilot.press("enter")
            await until(pilot, lambda: "finishing 1 change" in screen_text(app))
            assert app.is_running
        finally:
            execute.gate.set()
        await until(pilot, lambda: _exited(app))
    assert execute.log == [("finished", "ArchiveOp"), ("exit", "ResumeIntent")]
    assert isinstance(app.return_value, ResumeIntent)
    assert app.return_value.choice.thread["sessionId"] == "s2"


async def test_the_done_state_offers_the_next_project(tmp_path):
    path = _write(
        tmp_path / "registry.json",
        _thread("s1", days=9, project="/w/first"),
        _thread("s2", days=4, project="/w/second"),
    )
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("K")
        await until(pilot, lambda: "/w/first done." in screen_text(app))
        assert "Next: Triage /w/second (1 waiting) — enter" in screen_text(app)
        await pilot.press("enter")
        await until(
            pilot,
            lambda: isinstance(app.screen, TriageScreen) and _ids(app) == ["claude-code:s2"],
        )
        await pilot.press("K")
        await until(pilot, lambda: "All caught up — escape for Home" in screen_text(app))


async def test_read_only_refuses_archive_and_the_row_stays(tmp_path):
    thread = _thread("s1", days=5)
    path = _write(tmp_path / "registry.json", thread)
    app = _app(path, read_only=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("a")
        await until(pilot, lambda: "read-only: archive title s1 is disabled" in _toasts(app))
        assert _ids(app) == ["claude-code:s1"]
    assert Path(thread["file"]).exists()


async def test_recent_and_woke_steps_open_their_scopes(tmp_path):
    woke = _thread("w1", days=20, snoozedAt=_iso(25), snoozedUntil=_iso(1))
    path = _write(tmp_path / "registry.json", _thread("s1", days=2), woke)
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        woke_step, recent_step = app.home_steps(app.home_state())[:2]
        app.activate(recent_step)
        await until(pilot, lambda: "Last 7 days · 1 waiting" in screen_text(app))
        assert _ids(app) == ["claude-code:s1"]
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, HomeScreen))
        app.activate(woke_step)
        await until(pilot, lambda: "Woke from snooze · 1 waiting" in screen_text(app))
        assert _ids(app) == ["claude-code:w1"]


async def test_golden_triage(tmp_path, golden):
    fixture = build_fixture(tmp_path, threads=40, now=NOW_DT)
    app = _app(fixture.registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        golden("inbox/triage", screen_text(app), replace={str(tmp_path): "<root>"})
        await pilot.press("space", "down", "down", "space")
        golden("inbox/triage-marked", screen_text(app), replace={str(tmp_path): "<root>"})


async def test_golden_detail(tmp_path, golden, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    thread = _thread(
        "s1",
        days=5,
        whatItIs="fixing the parser",
        lastPrompt="fix the parser",
        gitBranch="main",
    )
    path = _write(tmp_path / "registry.json", thread)
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, DetailScreen))
        await until(pilot, lambda: "fix the parser in s1" in screen_text(app))
        await pilot.pause()
        golden("inbox/detail", screen_text(app), replace={str(tmp_path): "<root>"})


async def test_undo_after_an_archive_applied_points_at_unarchive(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("s1", days=5))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_triage(app, pilot)
        await pilot.press("a")
        await until(pilot, lambda: _archived(path, "s1"))
        await until(pilot, lambda: app.idle)
        await pilot.press("u")
        await until(
            pilot,
            lambda: (
                "nothing to undo — already applied (rl session unarchive s1 restores an archive)"
                in _toasts(app)
            ),
        )
        assert "1 decided this session" in screen_text(app)
