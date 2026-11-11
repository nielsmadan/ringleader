import os
import threading
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ringleader import decisions, registry, stateio
from ringleader.decisions import KeepOp, RoundupPhasesOp
from ringleader.devtools.tui_fixture import build_fixture
from ringleader.home.app import InboxApp
from ringleader.home.base import RoundupRun
from ringleader.home.screens.home import HomeScreen
from ringleader.home.screens.roundup import (
    RoundupScreen,
    confirm_text,
    purged_text,
    stage_lines,
    warning_title,
)
from ringleader.presence import PresenceUnavailable
from ringleader.roundup import PhasesResult, RoundupPlan, RoundupProgress
from ringleader.sources.base import stat_fingerprint
from ringleader.tui.modals import ChoiceModal, ConfirmModal
from ringleader.tui.snapshot import screen_text
from textual.widgets import Label

from tui_helpers import drawn_text, until

NOW_DT = datetime(2026, 10, 15, 12, tzinfo=UTC)
NOW = NOW_DT.isoformat()
CANDIDATE = "tiny"
ANSWER = {"status": "finished", "whatItIs": "a parser fix", "suggestedName": "parser-fix"}


def _iso(days):
    return (NOW_DT - timedelta(days=days)).isoformat()


def _thread(sid, *, days, messages=20):
    path = Path(os.environ["SESSION_TRACKER_PROJECTS_DIR"]) / "-w-x" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{{"type":"user","message":{{"content":"fix the parser in {sid}"}}}}\n')
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


def _write(path, *, old):
    threads = [
        _thread(CANDIDATE, days=30, messages=3),
        _thread("recent-1", days=1),
        _thread("recent-2", days=2),
        *(_thread(f"old-{k:02d}", days=20 + k) for k in range(old)),
    ]
    registry.save_registry(path, {**registry.empty_registry(), "threads": threads})
    return path


class CountingReload:
    def __init__(self):
        self.calls = 0

    def __call__(self, path, now_iso):
        self.calls += 1
        return registry.load_registry(path), None


class FakeModel:
    name = "claude"
    model = "fake"

    def __init__(self, gate=None):
        self.gate = gate
        self.prompts = []
        self._lock = threading.Lock()

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        with self._lock:
            self.prompts.append(prompt)
        if self.gate is not None:
            self.gate.wait(10)
        return ANSWER


def _app(path, *, model=None, presence=frozenset, refresher=None, **kwargs):
    return InboxApp(
        path,
        refresher=refresher or CountingReload(),
        presence=presence,
        now=lambda: NOW_DT,
        models=lambda task: model if task == "enrich" else None,
        cwd=path.parent,
        strict=True,
        **kwargs,
    )


def _transcript(sid):
    return Path(os.environ["SESSION_TRACKER_PROJECTS_DIR"]) / "-w-x" / f"{sid}.jsonl"


def _stored(path):
    reg = registry.load_registry(path)
    return {t["sessionId"]: t for t in reg["threads"]}, reg


def _toasts(app):
    return [n.message for n in app._notifications]


async def _open_plan(app, pilot):
    await until(pilot, lambda: app.idle)
    await pilot.press("4")
    await until(pilot, lambda: isinstance(app.screen, RoundupScreen))
    await until(pilot, lambda: app.screen.plan is not None)


async def _choose(app, pilot, key):
    await pilot.press("enter")
    await until(pilot, lambda: isinstance(app.screen, ChoiceModal))
    await pilot.press(key)
    await until(pilot, lambda: isinstance(app.screen, RoundupScreen))


async def test_the_plan_reuses_the_fresh_scan_and_excludes_the_prune_candidate(tmp_path):
    path = _write(tmp_path / "registry.json", old=27)
    refresher = CountingReload()
    app = _app(path, model=FakeModel(), refresher=refresher)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_plan(app, pilot)
        plan = app.screen.plan
        assert refresher.calls == 1
        assert [item.identity for item in plan.prune_items] == [("claude-code", CANDIDATE)]
        assert len(plan.enrich_all_ids) == 29
        assert ("claude-code", CANDIDATE) not in plan.enrich_all_ids
        assert "delete 1 tiny session older than 7 days (prune)" in screen_text(app)


async def test_enter_offers_the_four_enrich_choices(tmp_path):
    path = _write(tmp_path / "registry.json", old=27)
    app = _app(path, model=FakeModel())
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_plan(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ChoiceModal))
        text = screen_text(app)
        assert "Run the roundup? deletes 1, purges 0, archives 0." in text
        assert "Enrich 29 sessions? about 1 min" in text
        assert "Run, enrich all 29 — about 1 min" in text
        assert "Run, enrich the 2 from the last 7 days — about 1 min" in text
        assert "Run, skip enrich" in text
        assert "Cancel" in text


async def test_recent_enriches_the_two_recent_deletes_the_candidate_and_stamps(tmp_path):
    path = _write(tmp_path / "registry.json", old=27)
    model = FakeModel()
    app = _app(path, model=model)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_plan(app, pilot)
        await _choose(app, pilot, "2")
        await until(pilot, lambda: app.roundup is not None and app.roundup.stage == "done")
        await until(pilot, lambda: app.idle)
        threads, reg = _stored(path)
        classified = sorted(sid for sid, t in threads.items() if t["status"] == "finished")
        assert classified == ["recent-1", "recent-2"]
        assert len(model.prompts) == 2
        assert not _transcript(CANDIDATE).exists()
        assert reg["lastRoundupAt"] == NOW
        assert "Roundup finished." in screen_text(app)
        await until(pilot, lambda: "roundup finished" in _toasts(app))


async def test_cancel_in_the_choice_deletes_nothing(tmp_path):
    path = _write(tmp_path / "registry.json", old=27)
    app = _app(path, model=FakeModel())
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_plan(app, pilot)
        await _choose(app, pilot, "4")
        await pilot.pause()
        assert app.roundup is None
        assert _transcript(CANDIDATE).exists()
        assert "enter run · escape back" in screen_text(app)


async def test_enter_alone_in_the_choice_cancels_and_deletes_nothing(tmp_path):
    path = _write(tmp_path / "registry.json", old=27)
    model = FakeModel()
    app = _app(path, model=model)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_plan(app, pilot)
        await _choose(app, pilot, "enter")
        await pilot.pause()
        assert app.roundup is None
        assert _transcript(CANDIDATE).exists()
        assert model.prompts == []
        assert "enter run · escape back" in screen_text(app)


async def test_few_pending_asks_yes_or_no_and_no_deletes_nothing(tmp_path):
    path = _write(tmp_path / "registry.json", old=3)
    app = _app(path, model=FakeModel())
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_plan(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        message = app.screen.query_one("#confirm-message", Label).content
        assert str(message) == "Run the roundup? deletes 1, purges 0, archives 0, enriches 5. [y/n]"
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, RoundupScreen))
        assert app.roundup is None
        assert _transcript(CANDIDATE).exists()


async def test_c_mid_enrich_cancels_without_stamping(tmp_path):
    path = _write(tmp_path / "registry.json", old=27)
    gate = threading.Event()
    model = FakeModel(gate)
    app = _app(path, model=model)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _open_plan(app, pilot)
            await _choose(app, pilot, "1")
            await until(pilot, lambda: app.roundup.stage == "enrich" and model.prompts)
            await pilot.press("c")
            await until(pilot, lambda: "stopping after the current session…" in screen_text(app))
        finally:
            gate.set()
        await until(pilot, lambda: app.roundup.stage == "cancelled")
        await until(pilot, lambda: app.idle)
        assert "Roundup cancelled — not stamped" in screen_text(app)
        assert _stored(path)[1].get("lastRoundupAt") is None
        assert not _transcript(CANDIDATE).exists()


async def test_leaving_and_returning_shows_the_same_run_continuing(tmp_path):
    path = _write(tmp_path / "registry.json", old=27)
    gate = threading.Event()
    app = _app(path, model=FakeModel(gate))
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _open_plan(app, pilot)
            await _choose(app, pilot, "1")
            await until(pilot, lambda: app.roundup.stage == "enrich")
            run = app.roundup
            await pilot.press("escape")
            await until(pilot, lambda: isinstance(app.screen, HomeScreen))
            await until(pilot, lambda: "roundup running" in screen_text(app))
            await pilot.press("4")
            await until(pilot, lambda: isinstance(app.screen, RoundupScreen))
            await until(pilot, lambda: "Roundup running" in screen_text(app))
            assert app.roundup is run
            assert "▸ enrich  enrich 29 sessions (all)" in screen_text(app)
        finally:
            gate.set()
        await until(pilot, lambda: app.roundup.stage == "done", timeout=10)
        assert _stored(path)[1]["lastRoundupAt"] == NOW


async def test_unknown_presence_skips_the_prune_item_and_keeps_its_transcript(tmp_path):
    path = _write(tmp_path / "registry.json", old=3)

    def unknown():
        raise PresenceUnavailable("not installed")

    app = _app(path, model=FakeModel(), presence=unknown)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_plan(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("y")
        await until(pilot, lambda: app.roundup is not None and app.roundup.stage == "done")
        skipped = app.roundup.phases.skipped
        assert [(seen.identity[1], reason) for seen, reason in skipped] == [
            (
                CANDIDATE,
                (
                    f"can't tell whether title {CANDIDATE} is running — install presence with "
                    "rl data presence install"
                ),
            )
        ]
        assert _transcript(CANDIDATE).exists()
        assert f"  title {CANDIDATE}: can't tell whether title {CANDIDATE} is running" in (
            screen_text(app)
        )


async def test_read_only_refuses_enter(tmp_path):
    path = _write(tmp_path / "registry.json", old=3)
    app = _app(path, model=FakeModel(), read_only=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_plan(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: "read-only: roundup is disabled" in _toasts(app))
        assert isinstance(app.screen, RoundupScreen)
        assert app.roundup is None


async def test_read_only_plan_names_the_model_it_would_use(tmp_path):
    path = _write(tmp_path / "registry.json", old=3)
    app = _app(path, model=FakeModel(), read_only=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_plan(app, pilot)
        await until(pilot, lambda: "enrich: read-only" in screen_text(app))
        assert "enrich: read-only — skipped (would use claude (fake))" in screen_text(app)


async def test_read_only_plan_without_a_model_says_none_is_configured(tmp_path):
    path = _write(tmp_path / "registry.json", old=3)
    app = _app(path, read_only=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_plan(app, pilot)
        await until(pilot, lambda: "enrich: no model configured" in screen_text(app))


async def test_golden_roundup_plan(tmp_path, golden):
    fixture = build_fixture(tmp_path, threads=40, now=NOW_DT)
    app = _app(fixture.registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_plan(app, pilot)
        golden(
            "inbox/roundup-plan", await drawn_text(pilot, app), replace={str(tmp_path): "<root>"}
        )


async def test_golden_roundup_warning(tmp_path, golden):
    path = _write(tmp_path / "registry.json", old=27)
    app = _app(path, model=FakeModel())
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_plan(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ChoiceModal))
        golden(
            "inbox/roundup-warning", await drawn_text(pilot, app), replace={str(tmp_path): "<root>"}
        )


def _exited(app):
    return app._exit or not app.is_running


def _held_phases(gate, *, progress=None, after=None):
    def execute(op, ctx):
        if isinstance(op, RoundupPhasesOp):
            if progress is not None:
                ctx.progress(progress)
            gate.wait(10)
            outcome = decisions.execute(op, ctx)
            if after is not None:
                after()
            return outcome
        return decisions.execute(op, ctx)

    return execute


def _held_keep(gate):
    def execute(op, ctx):
        if isinstance(op, KeepOp):
            gate.wait(10)
        return decisions.execute(op, ctx)

    return execute


async def _start_few(app, pilot, *, settled=True):
    if settled:
        await _open_plan(app, pilot)
    else:
        await pilot.press("4")
        await until(pilot, lambda: isinstance(app.screen, RoundupScreen))
        await until(pilot, lambda: app.screen.plan is not None)
    await pilot.press("enter")
    await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
    await pilot.press("y")
    await until(pilot, lambda: isinstance(app.screen, RoundupScreen))


async def test_phase_progress_reaches_the_screen(tmp_path):
    path = _write(tmp_path / "registry.json", old=3)
    gate = threading.Event()
    app = _app(path, execute=_held_phases(gate, progress=RoundupProgress("prune", 3, 14)))
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _start_few(app, pilot)
            await until(pilot, lambda: app.roundup.progress is not None)
            await pilot.pause()
            assert "▸ prune" in screen_text(app)
            assert "delete 1 tiny session — 3 / 14" in screen_text(app)
        finally:
            gate.set()
        await until(pilot, lambda: app.roundup.stage == "done")


async def test_c_during_phases_stops_without_stamping(tmp_path):
    path = _write(tmp_path / "registry.json", old=3)
    gate = threading.Event()
    app = _app(path, execute=_held_phases(gate, progress=RoundupProgress("sweep", 0, 0)))
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _start_few(app, pilot)
            await until(pilot, lambda: "▸ sweep" in screen_text(app))
            await pilot.press("c")
            await until(pilot, lambda: "stopping after the current session…" in screen_text(app))
        finally:
            gate.set()
        await until(pilot, lambda: app.roundup.stage == "cancelled")
        await until(pilot, lambda: app.idle)
        assert _transcript(CANDIDATE).exists()
        assert _stored(path)[1].get("lastRoundupAt") is None


async def test_c_on_a_queued_roundup_drops_it_before_anything_is_deleted(tmp_path):
    path = _write(tmp_path / "registry.json", old=3)
    gate = threading.Event()
    app = _app(path, execute=_held_keep(gate))
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: app.idle)
            app.decide("keep", [registry.load_registry(path)["threads"][1]])
            await until(pilot, lambda: app.writer.waiting == 0 and not app.writer.idle)
            await _start_few(app, pilot, settled=False)
            assert app.roundup.stage == "phases"
            await pilot.press("c")
            await until(pilot, lambda: app.roundup.stage == "cancelled")
        finally:
            gate.set()
        await until(pilot, lambda: app.idle)
        assert _transcript(CANDIDATE).exists()
        assert _stored(path)[1].get("lastRoundupAt") is None
        assert "Roundup cancelled — not stamped" in screen_text(app)


async def test_q_during_phases_finishes_them_then_exits_without_stamping(tmp_path):
    path = _write(tmp_path / "registry.json", old=3)
    gate = threading.Event()
    app = _app(path, execute=_held_phases(gate, progress=RoundupProgress("sweep", 0, 0)))
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _start_few(app, pilot)
            await until(pilot, lambda: "▸ sweep" in screen_text(app))
            await pilot.press("q")
            await until(pilot, lambda: "finishing 1 change" in screen_text(app))
            assert app.is_running
        finally:
            gate.set()
        await until(pilot, lambda: _exited(app))
    assert app.roundup.stage == "cancelled"
    assert _stored(path)[1].get("lastRoundupAt") is None


async def test_q_during_enrich_exits_once_the_enrich_has_drained_without_stamping(tmp_path):
    path = _write(tmp_path / "registry.json", old=27)
    gate = threading.Event()
    model = FakeModel(gate)
    app = _app(path, model=model)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _open_plan(app, pilot)
            await _choose(app, pilot, "1")
            await until(pilot, lambda: app.roundup.stage == "enrich" and model.prompts)
            await pilot.press("q")
            await until(pilot, lambda: "finishing 1 change" in screen_text(app))
            assert app.is_running
        finally:
            gate.set()
        await until(pilot, lambda: _exited(app), timeout=10)
    assert app.writer.idle
    assert app.roundup.stage == "cancelled"
    assert _stored(path)[1].get("lastRoundupAt") is None


async def test_q_q_y_during_enrich_discards_nothing_but_still_exits(tmp_path):
    path = _write(tmp_path / "registry.json", old=27)
    gate = threading.Event()
    model = FakeModel(gate)
    app = _app(path, model=model)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _open_plan(app, pilot)
            await _choose(app, pilot, "1")
            await until(pilot, lambda: app.roundup.stage == "enrich" and model.prompts)
            await pilot.press("q", "q")
            await until(pilot, lambda: "the change being written now" in " ".join(_toasts(app)))
        finally:
            gate.set()
        await until(pilot, lambda: _exited(app), timeout=10)
    assert _stored(path)[1].get("lastRoundupAt") is None


async def test_another_roundup_taking_the_lock_after_phases_fails_the_run_without_stamping(
    tmp_path,
):
    path = _write(tmp_path / "registry.json", old=3)
    gate = threading.Event()
    gate.set()
    holder = ExitStack()

    def take_lock():
        assert holder.enter_context(stateio.roundup_lock()) is True

    app = _app(path, model=FakeModel(), execute=_held_phases(gate, after=take_lock))
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _start_few(app, pilot)
            await until(pilot, lambda: app.roundup.stage == "failed")
        finally:
            holder.close()
        await until(pilot, lambda: app.idle)
        assert "Roundup failed: another roundup is running" in screen_text(app)
        assert _stored(path)[1].get("lastRoundupAt") is None


async def test_the_tui_holds_the_roundup_lock_while_it_enriches(tmp_path):
    path = _write(tmp_path / "registry.json", old=27)
    gate = threading.Event()
    model = FakeModel(gate)
    app = _app(path, model=model)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _open_plan(app, pilot)
            await _choose(app, pilot, "1")
            await until(pilot, lambda: app.roundup.stage == "enrich" and model.prompts)
            with stateio.roundup_lock() as acquired:
                assert acquired is False
        finally:
            gate.set()
        await until(pilot, lambda: app.roundup.stage == "done", timeout=10)
        with stateio.roundup_lock() as acquired:
            assert acquired is True


async def test_a_session_changed_during_enrich_is_reported_as_stale_not_enriched(tmp_path):
    path = _write(tmp_path / "registry.json", old=3)
    gate = threading.Event()
    model = FakeModel(gate)
    app = _app(path, model=model)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await _start_few(app, pilot)
            await until(pilot, lambda: app.roundup.stage == "enrich" and model.prompts)
            registry.mutate_registry(
                path,
                lambda reg: reg["threads"][1].update(mtime=_iso(0.5)),
            )
        finally:
            gate.set()
        await until(pilot, lambda: app.roundup.stage == "done", timeout=10)
        await until(pilot, lambda: app.idle)
        assert app.roundup.enrich.stale == 1
        assert "enriched 4 of 5 (0 failed, 1 changed meanwhile)" in screen_text(app)


def test_purged_text_counts_handoff_exports():
    phases = PhasesResult(
        swept=0,
        deleted=0,
        purged=2,
        skipped=(),
        errors=0,
        cancelled=False,
        warnings=(),
        handoffs_purged=4,
    )

    assert purged_text(phases) == "purged 2 (+4 handoff exports)"


def _handoff_plan(handoffs):
    return RoundupPlan(
        "r",
        NOW,
        None,
        (),
        (),
        (("claude-code", "a"), ("claude-code", "b")),
        (),
        (),
        None,
        None,
        1,
        handoff_paths=tuple(f"/h/{k}.md" for k in range(handoffs)),
    )


def test_the_roundup_confirmation_counts_the_handoff_exports_it_deletes():
    plan = _handoff_plan(3)

    assert "purges 2 and 3 handoff exports," in confirm_text(plan, "none")
    assert "purges 2 and 3 handoff exports," in warning_title(plan)


def test_the_planned_purge_stage_names_the_handoff_exports():
    run = RoundupRun(_handoff_plan(1), "none", "phases", None, None, None)

    assert "purge   purge 2 archived sessions, and 1 handoff export" in stage_lines(run)[2]


def test_the_roundup_confirmation_without_handoffs_counts_only_sessions():
    assert "purges 2, archives" in confirm_text(_handoff_plan(0), "none")
