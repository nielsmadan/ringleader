from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, ClassVar, Protocol, cast, runtime_checkable

from rich.text import Text
from textual import events, work
from textual.app import App
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import Static

from .. import (
    decisions,
    overview,
    paths,
    refresh,
    registry,
    resume,
    revalidate,
    scanner,
)
from ..agents import source_of
from ..config import key_preset, load_config
from ..decisions import (
    ArchiveOp,
    ClassifyOp,
    Decision,
    DeleteOp,
    HarvestSaveOp,
    KeepGoingOp,
    KeepOp,
    Op,
    OpContext,
    PruneOp,
    ReloadOp,
    RoundupPhasesOp,
    SingleOp,
    SnoozeOp,
    StampRoundupOp,
    lifecycle_reason,
)
from ..enrichment import Classified
from ..models import Model, ModelTask, active_model, proc
from ..picker.app import Choice, PickerScreen, RefreshResult, ask_resume_choice, live_session_ids
from ..progress import CancelCheck
from ..resume import Scope
from ..revalidate import Identity, Seen
from ..roundup import EnrichScope, RoundupPlan, RoundupProgress
from ..tui.background import CancelToken, Done, Failed, Job
from ..tui.modals import AgentPickModal, ConfirmModal
from ..tui.types import Clock, Presence, Refresher
from . import overlay
from .base import AppState, InboxScreen, RoundupRun, ScreenTarget
from .harvest_view import HarvestGroup, publish_all_cwd
from .intents import ExitIntent, ResumeIntent, publish_handoff, triage_handoff
from .jobs import (
    HarvestBlocked,
    HarvestEmpty,
    HarvestMessage,
    HarvestReady,
    JobBook,
    JobMessage,
    harvest_job,
)
from .keymap import Keymap, keymap_for, with_bindings
from .palette import InboxCommands
from .policy import DELETE_GRACE_SECONDS
from .prints import PrintCoalescer
from .roundup_run import EnrichDoneMessage, EnrichProgressMessage, RoundupDriver
from .scan import ScanLanded, ScanService
from .screens.harvests import HarvestsScreen
from .screens.home import HomeScreen
from .screens.projects import ProjectsScreen
from .screens.prune import PruneScreen
from .screens.roundup import RoundupScreen
from .screens.triage import TriageScreen
from .screens.usage import UsageScreen
from .state import (
    RECENT_DAYS,
    HomeState,
    NextStep,
    ProjectRef,
    ProjectScope,
    RecentScope,
    RenderBasis,
    TriageScope,
    WokeScope,
    build_home_state,
    next_steps,
    plural,
    visible_threads,
)
from .writer import (
    Batch,
    Drained,
    Execute,
    Finished,
    OpProgress,
    Refused,
    Snapshot,
    Started,
    Writer,
    WriterEvent,
)

type ModelResolver = Callable[[ModelTask], Model | None]

SUBTITLE_REFRESH_SECONDS = 30.0


@runtime_checkable
class LiveStatus(Protocol):
    def render_status(self) -> None: ...


@dataclass(frozen=True)
class _ViewMemo:
    serial: int
    snapshot: Snapshot
    hidden: frozenset[Identity]
    reg: dict[str, Any]
    threads: list[dict[str, Any]]


@dataclass(frozen=True)
class _HomeMemo:
    key: tuple[int, datetime, int | None]
    state: HomeState


class WriterMessage(Message):
    def __init__(self, event: WriterEvent) -> None:
        super().__init__()
        self.event = event


class ScanMessage(Message):
    def __init__(self, landed: ScanLanded) -> None:
        super().__init__()
        self.landed = landed


class ScanFailedMessage(Message):
    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error


NOTHING_TO_HARVEST = "nothing worth harvesting — archive it with a if it is done"
NO_HARVEST_MODEL = "no harvest model configured — rl config, or hand the project to an agent with t"
_DECISION_WORDS: dict[Decision, str] = {
    "archive": "archive",
    "harvest": "harvest",
    "keep_going": "keep going",
    "snooze": "snooze",
    "keep": "keep",
    "delete": "delete",
}
_DECISION_OPS = (ArchiveOp, DeleteOp, KeepGoingOp, SnoozeOp, KeepOp, HarvestSaveOp)
_ROUNDUP_OPS = (RoundupPhasesOp, PruneOp)
_QUIET_WHEN_OK = (RoundupPhasesOp, ClassifyOp, StampRoundupOp)
REFUSALS_SHOWN = 3


def refusal_lines(refused: Sequence[tuple[Seen, str]], limit: int | None) -> list[str]:
    shown = [f"{seen.label}: {reason}" for seen, reason in refused[:limit]]
    rest = len(refused) - len(shown)
    return [*shown, *([f"… and {rest} more skipped"] if rest > 0 else [])]


def decision_op(decision: Decision, seen: Seen, until: str | None) -> SingleOp:
    match decision:
        case "archive":
            return ArchiveOp(seen)
        case "delete":
            return DeleteOp(seen)
        case "keep_going":
            return KeepGoingOp(seen)
        case "keep":
            return KeepOp(seen)
        case "snooze" if until is not None:
            return SnoozeOp(seen, until)
    raise ValueError(f"{decision} is not a single-step decision")


class _HostedPicker(PickerScreen):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "leave", "back")]

    def action_refresh(self) -> None:
        cast(InboxApp, self.app).rescan()
        if self._refreshing:
            self._refresh_status = "Refreshing sessions… refresh queued"
            self._show_refresh_status()
        else:
            self._start_refresh()

    async def _receive_refresh(self) -> None:
        inbox = cast(InboxApp, self.app)
        while self._alive():
            outcome = inbox.scans.outcome()
            if outcome is None:
                await asyncio.sleep(0.02)
                continue
            if isinstance(outcome, Done) and outcome.value.snapshot.scanned:
                landed = outcome.value
                result = RefreshResult(
                    inbox._decided_view(landed),
                    landed.live_ids or frozenset(),
                    None,
                    None,
                    landed.unavailable_sources,
                )
            else:
                result = RefreshResult(None, None, None, "inbox session scan failed")
            self._refresh_status = self._result_status(result)
            self._show_refresh_status()
            self._adopt(result.reg, result.running, None)
            return


def _default_models(task: ModelTask) -> Model | None:
    return active_model(load_config(), task)


def _target_binding(key: str, target: ScreenTarget) -> Binding:
    return Binding(key, f"go_to('{target.value}')", target.value, show=False)


class InboxApp(App[ExitIntent | None]):
    TITLE = "rl inbox"
    COMMANDS = App.COMMANDS | {InboxCommands}

    BINDINGS: ClassVar[list[BindingType]] = [
        *(_target_binding(str(index), target) for index, target in enumerate(ScreenTarget, 1)),
        Binding("q", "quit", "quit"),
        Binding("ctrl+c", "cancel", "cancel", show=False, priority=True),
    ]

    def __init__(  # noqa: PLR0913 -- injection points the suite drives
        self,
        registry_path: str | Path,
        *,
        refresher: Refresher | None = None,
        presence: Presence | None = None,
        now: Clock | None = None,
        models: ModelResolver | None = None,
        execute: Execute | None = None,
        read_only: bool = False,
        cwd: str | Path | None = None,
        delete_grace: float = DELETE_GRACE_SECONDS,
        strict: bool = False,
        keymap: Keymap | None = None,
    ) -> None:
        super().__init__()
        config = load_config()
        self.keymap = keymap or keymap_for(key_preset(config.inbox_keys))
        # bound here, after the preset's undo, so the footer keeps undo before rescan
        self._bindings = with_bindings(
            self._bindings,
            (Binding(self.keymap.undo, "undo", "undo"), Binding("ctrl+r", "rescan", "rescan")),
        )
        self.registry_path = str(registry_path)
        self.read_only = read_only
        self._strict = strict
        self._now: Clock = now or (lambda: datetime.now(UTC))
        self._presence: Presence = presence or live_session_ids
        self._models: ModelResolver = models or _default_models
        self._cwd = str(cwd) if cwd is not None else os.getcwd()
        self._stale_hours = config.roundup_stale_hours
        pool = scanner.SourcePool()
        default_refresher: Refresher = partial(
            refresh.auto_refresh, persist=not read_only, pool=pool
        )
        self._state = AppState(
            snapshot=Snapshot(registry.empty_registry(), 0, False, time.monotonic())
        )
        self._undelivered = 0
        self._undelivered_lock = threading.Lock()
        self._scanned_at: datetime | None = None
        self._scan_failed = False
        self._batch_labels: dict[int, tuple[str, int]] = {}
        self._decided = 0
        self._last_archived: str | None = None
        self._delete_grace = delete_grace
        self._leaving = False
        self._leave_intent: ExitIntent | None = None
        self._render_pending = False
        self._synced: tuple[InboxScreen, int] | None = None
        self._view_memo: _ViewMemo | None = None
        self._home_memo: _HomeMemo | None = None
        self.jobs = JobBook(self._post)
        self.roundups = RoundupDriver(
            read_only=read_only,
            submit=lambda ops, label: self.submit_batch(ops, label=label),
            classify=self._apply_classified,
            cancel_op=self.cancel_op,
            enrich_model=lambda: self.model_for("enrich"),
            leaving=lambda: self._leaving,
            post=self._post,
            toast=lambda message, warning: self._toast(message, warning=warning),
            ended=self._roundup_ended,
        )
        self._prints = PrintCoalescer(
            toast=lambda text: self._toast(text, warning=True),
            schedule=self.set_timer,
        )
        self.writer = Writer(
            self.registry_path,
            executor=decisions.as_executor(execute),
            make_context=self._make_context,
            deliver=lambda event: self._post(WriterMessage(event)),
            delete_grace=delete_grace,
            read_only=read_only,
        )
        self.scans = ScanService(
            self.registry_path,
            refresher=refresher or default_refresher,
            full_refresher=None
            if refresher
            else partial(refresh.refresh_registry, persist=not read_only, pool=pool),
            presence=self._presence,
            models=self._models,
            clock=self._now,
            generation=lambda: self.writer.generation,
            deliver=lambda landed: self._post(ScanMessage(landed)),
            fail=lambda error: self._post(ScanFailedMessage(error)),
        )

    @property
    def state(self) -> AppState:
        return self._state

    @property
    def idle(self) -> bool:
        with self._undelivered_lock:
            undelivered = self._undelivered
        return (
            undelivered == 0
            and not self.scans.running
            and not self._render_pending
            and not self._background_busy()
        )

    def _background_busy(self) -> bool:
        return not self.writer.idle or self.jobs.busy or self.roundups.enriching

    @property
    def decided_count(self) -> int:
        return self._decided

    @property
    def harvesting(self) -> tuple[str, ...]:
        return self.jobs.harvesting

    def _view(self) -> _ViewMemo:
        snapshot = self._state.snapshot
        hidden = frozenset(self._state.overlay)
        memo = self._view_memo
        if memo is None or memo.snapshot is not snapshot or memo.hidden != hidden:
            reg = overlay.visible(snapshot.reg, self._state.overlay)
            serial = memo.serial + 1 if memo is not None else 1
            memo = _ViewMemo(serial, snapshot, hidden, reg, visible_threads(reg))
            self._view_memo = memo
        return memo

    def render_basis(self) -> RenderBasis:
        return RenderBasis.at(self._view().serial, self._now(), self._state.live_ids)

    def view(self) -> dict[str, Any]:
        return self._view().reg

    def view_threads(self) -> list[dict[str, Any]]:
        return self._view().threads

    def clock(self) -> datetime:
        return self._now()

    def now_iso(self) -> str:
        return self._now().isoformat()

    def now_epoch(self) -> float:
        return self._now().timestamp()

    def model_for(self, task: ModelTask) -> Model | None:
        return None if self.read_only else self._models(task)

    def configured_model(self, task: ModelTask) -> Model | None:
        return self._models(task)

    def home_state(self) -> HomeState:
        basis = self.render_basis()
        harvests = self._state.harvests
        pending = None if harvests is None else len(harvests)
        key = (basis.view, basis.minute, pending)
        memo = self._home_memo
        if memo is None or memo.key != key:
            state = build_home_state(
                self.view(),
                basis.now.isoformat(),
                pending_harvests=pending,
                stale_hours=self._stale_hours,
            )
            memo = _HomeMemo(key, state)
            self._home_memo = memo
        return memo.state

    def home_steps(self, state: HomeState) -> tuple[NextStep, ...]:
        return next_steps(state)

    def session_running(self, thread: dict[str, Any]) -> bool:
        live = self._state.live_ids
        key = (resume.agent_name(source_of(thread)), thread.get("sessionId") or "")
        return live is not None and key in live

    def status_line(self) -> str:
        pending = self.writer.unfinished + self.jobs.harvest_count
        parts = [
            *([f"{plural(pending, 'change')} pending"] if pending else []),
            *(["roundup running"] if self.roundup_active else []),
        ]
        return " · ".join(parts)

    def _set(self, **changes: Any) -> None:
        self._state = replace(self._state, **changes, version=self._state.version + 1)

    def _rerender(self) -> None:
        if self._render_pending:
            return
        self._render_pending = True
        self.call_after_refresh(self._render_now)

    def _render_now(self) -> None:
        self._render_pending = False
        self._update_subtitle()
        if not self.screen_stack:
            return
        screen = self.screen
        if isinstance(screen, InboxScreen):
            version = self._state.version
            synced = self._synced
            if synced is None or synced[0] is not screen or synced[1] != version:
                screen.sync()
                self._synced = (screen, version)
            elif isinstance(screen, LiveStatus):
                screen.render_status()
            self.show_leaving(screen)

    def _update_subtitle(self) -> None:
        if self._scanned_at is None:
            self.sub_title = "scan failed" if self._scan_failed else "scanning…"
            return
        age = overview.age_human(self._scanned_at.isoformat(), self.now_iso())
        unknown = " · presence unknown" if self._state.live_ids is None else ""
        self.sub_title = f"scanned {age}{unknown}"

    def _toast(self, message: str, *, warning: bool = False) -> None:
        self.notify(message, severity="warning" if warning else "information", markup=False)

    def on_mount(self) -> None:
        self.begin_capture_print(self, stdout=False, stderr=True)
        reg = registry.load_registry(self.registry_path)
        self._state = AppState(snapshot=Snapshot(reg, 0, False, time.monotonic()), version=1)
        self._update_subtitle()
        self.push_screen(HomeScreen())
        self.writer.start()
        self.scans.request()
        self.set_interval(SUBTITLE_REFRESH_SECONDS, self._update_subtitle)

    def on_unmount(self) -> None:
        proc.kill_all_children()
        self.roundups.release_lock()

    def _make_context(
        self, cancelled: CancelCheck, progress: Callable[[RoundupProgress], None]
    ) -> OpContext:
        return OpContext(
            registry_path=self.registry_path,
            archive_root_path=str(paths.archive_root()),
            now_iso=self.now_iso,
            now_epoch=self.now_epoch,
            presence=revalidate.probe(self._presence),
            unknown="refuse",
            cancelled=cancelled,
            progress=progress,
        )

    def _post(self, message: Message) -> None:
        with self._undelivered_lock:
            self._undelivered += 1
        if not self.post_message(message):
            self._delivered()

    def _delivered(self) -> None:
        with self._undelivered_lock:
            self._undelivered -= 1

    def on_writer_message(self, message: WriterMessage) -> None:
        self._delivered()
        event = message.event
        match event:
            case Started():
                self._set(overlay=overlay.started(self._state.overlay, event.ticket))
                self._rerender()
            case OpProgress():
                if self.roundups.op_progress(event.ticket, event.progress):
                    self._rerender()
                for screen in self._inbox_screens():
                    screen.op_progress(event.ticket, event.progress)
            case Finished():
                self._finished(event)
            case Drained():
                self._leave_if_settled()

    def _finished(self, event: Finished) -> None:
        self._batch_labels.pop(event.ticket, None)
        outcome = event.outcome
        if not outcome.ok and not outcome.wrote and isinstance(event.op, _DECISION_OPS):
            self._decided = max(0, self._decided - 1)
        if outcome.ok and isinstance(event.op, ArchiveOp | HarvestSaveOp):
            self._last_archived = event.op.seen.identity[1]
        self._set(
            overlay=overlay.finished(
                self._state.overlay, event.ticket, event.op, outcome, event.generation
            )
        )
        quiet = outcome.ok and isinstance(event.op, _QUIET_WHEN_OK)
        if outcome.message and not quiet:
            self._toast(outcome.message, warning=not outcome.ok)
        capped = REFUSALS_SHOWN if isinstance(event.op, _ROUNDUP_OPS) else None
        for line in refusal_lines(outcome.refused, capped):
            self._toast(line, warning=True)
        for warning in outcome.warnings:
            self._toast(warning, warning=True)
        if outcome.refused:
            self.rescan()
        if event.snapshot is not None:
            self._adopt(event.snapshot)
        self.roundups.finished(event)
        for screen in self._inbox_screens():
            screen.op_finished(event)
        self._rerender()
        if event.internal is not None and self._strict:
            raise event.internal

    def _inbox_screens(self) -> list[InboxScreen]:
        return [screen for screen in self.screen_stack if isinstance(screen, InboxScreen)]

    def _adopt(self, snapshot: Snapshot) -> bool:
        if snapshot.generation < self._state.snapshot.generation:
            return False
        self._set(
            snapshot=snapshot, overlay=overlay.adopted(self._state.overlay, snapshot.generation)
        )
        return True

    def on_scan_message(self, message: ScanMessage) -> None:
        self._delivered()
        landed = message.landed
        self._scan_failed = not landed.snapshot.scanned
        if landed.snapshot.scanned:
            self._scanned_at = self._now()
        if not self._adopt(landed.snapshot) and not self._leaving:
            self.writer.submit([ReloadOp()], label="reload")
        snapshot = landed.harvests
        self._set(
            live_ids=landed.live_ids,
            harvests=None if snapshot is None else snapshot.items,
            harvest_problems=0 if snapshot is None else len(snapshot.problems),
            enrich_model=landed.enrich_model,
            harvest_model=landed.harvest_model,
            scan_note=landed.note,
        )
        for warning in landed.warnings:
            self._toast(warning, warning=True)
        if landed.note:
            self._toast(landed.note)
        self._rerender()

    def on_scan_failed_message(self, message: ScanFailedMessage) -> None:
        self._delivered()
        self._scan_failed = True
        self._toast(f"scan failed: {message.error}", warning=True)
        self._rerender()

    def rescan(self) -> None:
        if not self.scans.request():
            self._toast("rescan queued")

    def action_rescan(self) -> None:
        self.rescan()

    def on_print(self, event: events.Print) -> None:
        if event.stderr:
            self._prints.feed(event.text)

    def go(self, target: ScreenTarget) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        while len(self.screen_stack) > 1 and not isinstance(self.screen, HomeScreen):
            self.pop_screen()
        match target:
            case ScreenTarget.HOME:
                return
            case ScreenTarget.RESUME:
                self._open_resume()
            case ScreenTarget.PROJECTS:
                self.push_screen(ProjectsScreen())
            case ScreenTarget.ROUNDUP:
                self.push_screen(RoundupScreen())
            case ScreenTarget.PRUNE:
                self.push_screen(PruneScreen())
            case ScreenTarget.HARVESTS:
                self.push_screen(HarvestsScreen())
            case ScreenTarget.USAGE:
                self.push_screen(UsageScreen())

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return not (action == "command_palette" and isinstance(self.screen, ModalScreen))

    def action_go_to(self, name: str) -> None:
        self.go(ScreenTarget(name))

    def _open_resume(self) -> None:
        picker = _HostedPicker(
            self.registry_path,
            self._cwd,
            scope=Scope.ALL,
            presence=self._presence,
            now=self._now,
            initial=self._decided_view(None),
        )
        self.push_screen(picker, callback=self._resume_chosen)

    def _decided_view(self, landed: ScanLanded | None) -> dict[str, Any]:
        state = self._state
        current = state.snapshot
        base = (
            landed.snapshot
            if landed and landed.snapshot.generation >= current.generation
            else current
        )
        return overlay.visible(base.reg, state.overlay)

    def _resume_chosen(self, choice: Choice | None) -> None:
        if choice is not None:
            self.exit_with(ResumeIntent(choice))

    def activate(self, step: NextStep) -> None:
        match step.kind:
            case "roundup":
                self.go(ScreenTarget.ROUNDUP)
            case "woke":
                self.open_triage(WokeScope())
            case "recent":
                self.open_triage(RecentScope(RECENT_DAYS))
            case "project":
                if step.project is not None:
                    self.open_triage(ProjectScope(step.project))
            case "publish":
                self.start_publish_handoff(None)
            case "resume":
                thread = next(
                    (t for t in self.view_threads() if registry.identity(t) == step.session), None
                )
                if thread is not None:
                    self.resume_thread(thread)

    def open_triage(self, scope: TriageScope) -> None:
        self.go(ScreenTarget.HOME)
        if isinstance(self.screen, HomeScreen):
            self.push_screen(TriageScreen(scope, self.keymap))

    def resume_thread(self, thread: dict[str, Any]) -> None:
        self._resume_flow(thread)

    @work(exclusive=True, group="resume")
    async def _resume_flow(self, thread: dict[str, Any]) -> None:
        choice = await ask_resume_choice(
            self.screen,
            thread,
            target=None,
            running=self.session_running(thread),
            threads=tuple(self._state.snapshot.reg["threads"]),
        )
        if choice is not None:
            self.exit_with(ResumeIntent(choice))

    def submit(self, ops: Sequence[Op], *, label: str) -> bool:
        return self.submit_batch(ops, label=label) is not None

    def submit_batch(self, ops: Sequence[Op], *, label: str) -> Batch | None:
        if self._leaving:
            self._toast(f"leaving — {label} was not queued", warning=True)
            return None
        return self._queue(ops, label=label, replacing=())

    def _queue(self, ops: Sequence[Op], *, label: str, replacing: Sequence[int]) -> Batch | None:
        result = self.writer.submit(ops, label=label)
        if isinstance(result, Refused):
            self._toast(result.reason, warning=True)
            if replacing:
                self._set(overlay=overlay.removed(self._state.overlay, replacing))
                self._rerender()
            return None
        size = len(result.tickets)
        self._batch_labels.update((ticket, (label, size)) for ticket, _ in result.tickets)
        base = overlay.removed(self._state.overlay, replacing)
        self._set(overlay=overlay.submitted(base, result.tickets))
        self._rerender()
        return result

    def cancel_op(self, ticket: int) -> bool:
        match self.writer.cancel(ticket):
            case "running":
                return True
            case "dropped":
                self._batch_labels.pop(ticket, None)
                self._set(overlay=overlay.removed(self._state.overlay, [ticket]))
                self._dropped([ticket])
                self._rerender()
                return True
            case "gone":
                return False

    def run_job[T](
        self,
        job: Job[T, None],
        *,
        name: str,
        on_result: Callable[[Done[T] | Failed], None],
    ) -> CancelToken:
        return self.jobs.run_side(job, name=name, on_result=on_result)

    def on_job_message(self, message: JobMessage) -> None:
        self._delivered()
        self.jobs.side_landed()
        message.deliver()
        if self._leaving:
            self._leave_if_settled()

    def decide(
        self, decision: Decision, threads: Sequence[dict[str, Any]], *, until: str | None = None
    ) -> bool:
        if not threads:
            return False
        if decision == "harvest":
            if len(threads) > 1:
                self._toast("harvest works on one session at a time", warning=True)
                return False
            self.harvest(threads[0])
            return True
        allowed = []
        for thread in threads:
            reason = lifecycle_reason(thread, decision)
            if reason is None:
                allowed.append(thread)
            else:
                self._toast(reason, warning=True)
        if not allowed:
            return False
        ops = [decision_op(decision, revalidate.seen(t), until) for t in allowed]
        what = resume.label(allowed[0]) if len(allowed) == 1 else plural(len(allowed), "session")
        if not self.submit(ops, label=f"{_DECISION_WORDS[decision]} {what}"):
            return False
        self._decided += len(ops)
        if decision == "delete":
            self._toast(f"deleting {what} in {self._delete_grace:g} s — {self.keymap.undo} to undo")
        return True

    def harvest(self, thread: dict[str, Any]) -> None:
        label = resume.label(thread)
        identity = registry.identity(thread)
        if identity in self._state.overlay:
            return
        model = self.model_for("harvest")
        refusal = (
            "read-only: harvest is disabled"
            if self.read_only
            else f"leaving — harvest {label} was not started"
            if self._leaving
            else lifecycle_reason(thread, "harvest")
        )
        if refusal is not None or model is None:
            self._toast(refusal or NO_HARVEST_MODEL, warning=True)
            return
        ticket = self.jobs.start_harvest(
            label,
            revalidate.seen(thread),
            harvest_job(thread, model, revalidate.probe(self._presence)),
        )
        self._set(overlay=overlay.held(self._state.overlay, identity, ticket))
        self._rerender()

    def on_harvest_message(self, message: HarvestMessage) -> None:
        self._delivered()
        token = self.jobs.harvest_landed(message.ticket)
        cancelled = token is None or token.is_cancelled()
        seen = message.seen
        markdown: str | None = None
        ready = False
        match message.result:
            case Done(value=HarvestReady(markdown=answer)) if not cancelled:
                markdown, ready = answer, True
            case Done(value=HarvestEmpty()):
                self._toast(NOTHING_TO_HARVEST)
            case Done(value=HarvestBlocked(reason=reason)):
                self._toast(reason, warning=True)
            case Failed(error=error) if not cancelled:
                self._toast(f"harvest {seen.label} failed: {error}", warning=True)
        if ready:
            if (
                self._queue(
                    [HarvestSaveOp(seen, markdown)],
                    label=f"harvest {seen.label}",
                    replacing=(message.ticket,),
                )
                is not None
            ):
                self._decided += 1
        else:
            self._set(overlay=overlay.removed(self._state.overlay, (message.ticket,)))
        self._leave_if_settled()

    def action_undo(self) -> None:
        undone = self.writer.undo_last()
        if not undone:
            hint = (
                f"nothing to undo — already applied (rl session unarchive {self._last_archived} "
                "restores an archive)"
                if self._last_archived
                else "nothing to undo"
            )
            self._toast(hint)
            return
        tickets = [ticket for ticket, _ in undone]
        label, size = self._batch_labels.get(tickets[0], (plural(len(tickets), "change"), 0))
        for ticket in tickets:
            self._batch_labels.pop(ticket, None)
        decided = sum(1 for _, op in undone if isinstance(op, _DECISION_OPS))
        self._decided = max(0, self._decided - decided)
        self._set(overlay=overlay.removed(self._state.overlay, tickets))
        self._dropped(tickets)
        self._rerender()
        applied = size - len(tickets)
        self._toast(
            f"undone: {len(tickets)} of {label} ({applied} already applied)"
            if applied > 0
            else f"undone: {label}"
        )

    def _dropped(self, tickets: Sequence[int]) -> None:
        self.roundups.dropped(tickets)
        for screen in self._inbox_screens():
            screen.ops_dropped(tickets)

    @property
    def roundup(self) -> RoundupRun | None:
        return self.roundups.run

    @property
    def roundup_active(self) -> bool:
        return self.roundups.active

    @property
    def roundup_error(self) -> str | None:
        return self.roundups.error

    @property
    def roundup_stopping(self) -> bool:
        return self.roundups.stopping

    def start_roundup(self, plan: RoundupPlan, scope: EnrichScope) -> None:
        if self.roundups.start(plan, scope):
            self._rerender()

    def cancel_roundup(self) -> None:
        if self.roundups.cancel():
            self._rerender()

    def _apply_classified(self, batch: tuple[Classified, ...]) -> None:
        self.writer.submit([ClassifyOp(batch)], label="classify")

    def on_enrich_progress_message(self, message: EnrichProgressMessage) -> None:
        self._delivered()
        if self.roundups.enrich_progress(message.progress):
            self._rerender()

    def on_enrich_done_message(self, message: EnrichDoneMessage) -> None:
        self._delivered()
        self.roundups.enrich_done(message.result)
        self._leave_if_settled()

    def _roundup_ended(self) -> None:
        self._rerender()
        if not self._leaving:
            self.rescan()

    def start_triage_handoff(self, project: ProjectRef) -> None:
        self._triage_flow(project)

    @work(exclusive=True, group="triage-handoff")
    async def _triage_flow(self, project: ProjectRef) -> None:
        agent = await self.push_screen_wait(
            AgentPickModal(f"Triage {project.label} with which agent?", resume.launchable_agents())
        )
        if agent is not None:
            self.exit_with(
                triage_handoff(agent, project, self._state.snapshot.reg["threads"], Path.home())
            )

    def exit_with(self, intent: ExitIntent) -> None:
        if self._leaving:
            self._toast("leaving — that choice was not applied", warning=True)
            return
        self._leave(intent)

    async def action_cancel(self) -> None:
        if isinstance(self.screen, HomeScreen):
            await self.action_quit()
        else:
            self.simulate_key("escape")

    async def action_quit(self) -> None:
        if self._leaving:
            self._offer_discard()
            return
        self._leave(None)

    def _leave(self, intent: ExitIntent | None) -> None:
        self._leaving = True
        self._leave_intent = intent
        self._cancel_background(include_writer=False)
        if not self._background_busy():
            self.exit(intent)
            return
        self.writer.flush_grace()
        self._rerender()

    def _cancel_background(self, *, include_writer: bool) -> tuple[tuple[int, Op], ...]:
        self.jobs.cancel_side()
        self.roundups.cancel_enrich()
        if not include_writer:
            return ()
        self.jobs.cancel_harvests()
        dropped = self.writer.discard_pending()
        self.writer.cancel_current()
        return dropped

    def abort_background_work(self) -> None:
        self._cancel_background(include_writer=True)

    def _leave_if_settled(self) -> None:
        self._rerender()
        if self._leaving and not self._background_busy():
            self.exit(self._leave_intent)

    def _not_started(self) -> int:
        return self.writer.waiting + self.jobs.harvest_count

    def show_leaving(self, screen: Screen[Any]) -> None:
        banner = next(iter(screen.query("#leaving").results(Static)), None)
        if banner is None:
            return
        banner.display = self._leaving
        if self._leaving:
            pending = self.writer.unfinished + self.jobs.harvest_count + self.roundups.enriching
            banner.update(
                Text(
                    f"finishing {plural(pending, 'change')}… — q again to discard what has not "
                    "started"
                )
            )

    def _offer_discard(self) -> None:
        waiting = self._not_started()
        if waiting <= 0:
            self._toast("the change being written now will finish, then rl inbox exits")
            return
        message = Text(
            f"Discard {plural(waiting, 'queued change')} that have not started? "
            "The change being written now still finishes. [y/n]"
        )
        self.push_screen(ConfirmModal(message), callback=self._discard_confirmed)

    def _discard_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        dropped = self._cancel_background(include_writer=True)
        tickets = [ticket for ticket, _ in dropped]
        self._set(overlay=overlay.removed(self._state.overlay, tickets))
        self._dropped(tickets)
        self._leave_if_settled()

    def start_publish_handoff(self, group: HarvestGroup | None) -> None:
        self._publish_flow(group)

    @work(exclusive=True, group="publish")
    async def _publish_flow(self, group: HarvestGroup | None) -> None:
        everything = self._state.harvests or ()
        items = group.items if group is not None else everything
        if not items:
            self._toast("no pending harvests to publish")
            return
        count = len(items)
        agent = await self.push_screen_wait(
            AgentPickModal(
                f"Publish {plural(count, 'harvest')} with which agent?",
                resume.launchable_agents(),
            )
        )
        if agent is None:
            return
        home = Path.home()
        cwd = (group.folder if group is not None else None) or publish_all_cwd(everything, home)
        if home.is_relative_to(cwd):
            message = Text(
                f"Publish all {plural(count, 'harvest')} from {overview.tilde(cwd)}? "
                "The agent may touch any repository under it. [y/n]"
            )
            if not await self.push_screen_wait(ConfirmModal(message)):
                return
        self.exit_with(publish_handoff(agent, group, cwd))
