from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual import events, work
from textual.app import App
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import Static

from .. import (
    decisions,
    enrichment,
    overview,
    paths,
    refresh,
    registry,
    resume,
    revalidate,
    scanner,
    stateio,
)
from ..config import load_config
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
)
from ..enrichment import Classified, EnrichOutcome
from ..models import Model, ModelTask, active_model, proc, with_cancel
from ..picker.app import Choice, PickerScreen, ask_resume_choice, live_session_ids
from ..progress import CancelCheck
from ..resume import Scope
from ..revalidate import Identity, PresenceProbe, Seen
from ..roundup import EnrichScope, RoundupPlan, RoundupProgress
from ..tui.background import CancelToken, Done, Failed, Job, start_daemon
from ..tui.modals import AgentPickModal, ConfirmModal
from ..tui.types import Clock, Presence, Refresher
from . import overlay
from .base import AppState, InboxScreen, RoundupRun, ScreenTarget
from .harvest_view import HarvestGroup, publish_all_cwd
from .intents import (
    AgentHandoffIntent,
    ExitIntent,
    ResumeIntent,
    handoff_cwd,
    publish_prompt,
    triage_prompt,
)
from .palette import InboxCommands
from .policy import DELETE_GRACE_SECONDS
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

PRINT_COALESCE_SECONDS = 1.0
SUBTITLE_REFRESH_SECONDS = 30.0
PICKER_SCAN_WAIT_SECONDS = 60.0


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


@dataclass(frozen=True)
class HarvestReady:
    markdown: str | None


@dataclass(frozen=True)
class HarvestEmpty:
    pass


@dataclass(frozen=True)
class HarvestBlocked:
    reason: str


type HarvestResult = HarvestReady | HarvestEmpty | HarvestBlocked


class HarvestMessage(Message):
    def __init__(self, ticket: int, seen: Seen, result: Done[HarvestResult] | Failed) -> None:
        super().__init__()
        self.ticket = ticket
        self.seen = seen
        self.result = result


class EnrichProgressMessage(Message):
    def __init__(self, progress: RoundupProgress) -> None:
        super().__init__()
        self.progress = progress


class EnrichDoneMessage(Message):
    def __init__(self, result: Done[EnrichOutcome] | Failed) -> None:
        super().__init__()
        self.result = result


class JobMessage(Message):
    def __init__(self, deliver: Callable[[], None]) -> None:
        super().__init__()
        self.deliver = deliver


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
ROUNDUP_ACTIVE = frozenset({"phases", "enrich", "stamp"})
REFUSALS_SHOWN = 3


def harvest_job(
    thread: dict[str, Any], model: Model, presence: PresenceProbe
) -> Callable[[CancelToken, Callable[[None], None]], HarvestResult]:
    def job(token: CancelToken, _progress: Callable[[None], None]) -> HarvestResult:
        running = revalidate.running_reason(thread, presence(), "refuse")
        if running is not None:
            return HarvestBlocked(running)
        if thread.get("decision") == "harvested":
            return HarvestReady(None)
        markdown = decisions.harvest_markdown(thread, with_cancel(model, token.is_cancelled))
        return HarvestEmpty() if markdown is None else HarvestReady(markdown)

    return job


def enrich_job(
    plan: RoundupPlan,
    ids: tuple[Identity, ...],
    model: Model,
    apply: Callable[[tuple[Classified, ...]], None],
) -> Job[EnrichOutcome, RoundupProgress]:
    def job(token: CancelToken, report: Callable[[RoundupProgress], None]) -> EnrichOutcome:
        return enrichment.enrich_sessions(
            plan.registry_path,
            ids,
            model=model,
            concurrency=plan.concurrency,
            progress=lambda done, total: report(RoundupProgress("enrich", done, total)),
            cancelled=token.is_cancelled,
            apply=apply,
        )

    return job


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


def lifecycle_reason(thread: dict[str, Any], decision: Decision) -> str | None:
    if decision not in ("archive", "delete", "harvest"):
        return None
    try:
        scanner.filesystem_path(thread, decision)
    except RuntimeError as error:
        return str(error)
    return None


class _HostedPicker(PickerScreen):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "leave", "back")]


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
        Binding("u", "undo", "undo"),
        Binding("ctrl+r", "rescan", "rescan"),
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
    ) -> None:
        super().__init__()
        self.registry_path = str(registry_path)
        self.read_only = read_only
        self._strict = strict
        self._now: Clock = now or (lambda: datetime.now(UTC))
        self._presence: Presence = presence or live_session_ids
        self._models: ModelResolver = models or _default_models
        self._cwd = str(cwd) if cwd is not None else os.getcwd()
        self._stale_hours = load_config().roundup_stale_hours
        default_refresher: Refresher = (
            partial(refresh.auto_refresh, persist=False) if read_only else refresh.auto_refresh
        )
        self._state = AppState(
            Snapshot(registry.empty_registry(), 0, False, time.monotonic()),
            {},
            None,
            None,
            0,
            None,
            None,
            None,
            0,
        )
        self._undelivered = 0
        self._undelivered_lock = threading.Lock()
        self._scanned_at: datetime | None = None
        self._scan_failed = False
        self._running_ticket: int | None = None
        self._jobs: dict[int, CancelToken] = {}
        self._job_labels: dict[int, str] = {}
        self._next_job_ticket = 0
        self._batch_labels: dict[int, str] = {}
        self._decided = 0
        self._last_archived: str | None = None
        self._delete_grace = delete_grace
        self._enrich_job: CancelToken | None = None
        self._roundup: RoundupRun | None = None
        self._roundup_ticket: int | None = None
        self._roundup_error: str | None = None
        self._roundup_stopping = False
        self._roundup_stale = 0
        self._roundup_lock: ExitStack | None = None
        self._side_jobs = 0
        self._side_tokens: set[CancelToken] = set()
        self._leaving = False
        self._leave_intent: ExitIntent | None = None
        self._print_buffer = ""
        self._print_lines: list[str] = []
        self._last_print_toast = float("-inf")
        self._print_timer_armed = False
        self.writer = Writer(
            self.registry_path,
            execute=execute or decisions.execute,
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
            else partial(refresh.refresh_registry, persist=not read_only),
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
            and self.writer.idle
            and not self._jobs
            and self._enrich_job is None
            and self._side_jobs == 0
        )

    @property
    def decided_count(self) -> int:
        return self._decided

    @property
    def harvesting(self) -> tuple[str, ...]:
        return tuple(self._job_labels.values())

    def view_threads(self) -> list[dict[str, Any]]:
        return visible_threads(overlay.visible(self._state.snapshot.reg, self._state.overlay))

    def clock(self) -> datetime:
        return self._now()

    def now_iso(self) -> str:
        return self._now().isoformat()

    def model_for(self, task: ModelTask) -> Model | None:
        return None if self.read_only else self._models(task)

    def home_state(self) -> HomeState:
        harvests = self._state.harvests
        return build_home_state(
            overlay.visible(self._state.snapshot.reg, self._state.overlay),
            self.now_iso(),
            pending_harvests=None if harvests is None else len(harvests),
            stale_hours=self._stale_hours,
        )

    def home_steps(self, state: HomeState) -> tuple[NextStep, ...]:
        return next_steps(state)

    def session_running(self, thread: dict[str, Any]) -> bool:
        live = self._state.live_ids
        key = (resume.agent_name(resume.source_of(thread)), thread.get("sessionId") or "")
        return live is not None and key in live

    def status_line(self) -> str:
        pending = self.writer.pending + len(self._jobs)
        parts = [
            *([f"{plural(pending, 'change')} pending"] if pending else []),
            *(["roundup running"] if self.roundup_active else []),
        ]
        return " · ".join(parts)

    def _set(self, **changes: Any) -> None:
        self._state = replace(self._state, **changes, version=self._state.version + 1)

    def _rerender(self) -> None:
        self._update_subtitle()
        if not self.screen_stack:
            return
        screen = self.screen
        if isinstance(screen, InboxScreen):
            screen.sync()
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
        self._state = AppState(
            Snapshot(reg, 0, False, time.monotonic()), {}, None, None, 0, None, None, None, 1
        )
        self._update_subtitle()
        self.push_screen(HomeScreen())
        self.writer.start()
        self.scans.request()
        self.set_interval(SUBTITLE_REFRESH_SECONDS, self._update_subtitle)

    def on_unmount(self) -> None:
        proc.kill_all_children()
        self._release_roundup_lock()

    def _make_context(
        self, cancelled: CancelCheck, progress: Callable[[RoundupProgress], None]
    ) -> OpContext:
        return OpContext(
            registry_path=self.registry_path,
            archive_root_path=str(paths.archive_root()),
            now_iso=self.now_iso,
            now_epoch=time.time,
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
                self._running_ticket = event.ticket
                self._set(overlay=overlay.started(self._state.overlay, event.ticket))
                self._rerender()
            case OpProgress():
                if event.ticket == self._roundup_ticket:
                    self._roundup_progress(event.progress)
                    self._rerender()
                for screen in self._inbox_screens():
                    screen.op_progress(event.ticket, event.progress)
            case Finished():
                self._finished(event)
            case Drained():
                self._leave_if_settled()

    def _finished(self, event: Finished) -> None:
        self._running_ticket = None
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
        if isinstance(event.op, ClassifyOp):
            self._roundup_stale += outcome.stale
        if event.ticket == self._roundup_ticket:
            self._roundup_step_finished(event)
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
        self._scan_failed = False
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
        if not event.stderr:
            return
        *complete, self._print_buffer = (self._print_buffer + event.text).split("\n")
        self._print_lines.extend(line for line in complete if line.strip())
        self._flush_prints()

    def _timed_flush(self) -> None:
        self._print_timer_armed = False
        self._flush_prints()

    def _flush_prints(self) -> None:
        if not self._print_lines or self._print_timer_armed:
            return
        wait = self._last_print_toast + PRINT_COALESCE_SECONDS - time.monotonic()
        if wait > 0:
            self._print_timer_armed = True
            self.set_timer(wait, self._timed_flush)
            return
        lines, self._print_lines = self._print_lines, []
        self._last_print_toast = time.monotonic()
        self._toast("\n".join(lines), warning=True)

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
            refresher=self._picker_registry,
            presence=self._presence,
            now=self._now,
            initial=self._decided_view(None),
        )
        self.push_screen(picker, callback=self._resume_chosen)

    def _decided_view(self, landed: ScanLanded | None) -> dict[str, Any]:
        current = self._state.snapshot
        base = (
            landed.snapshot
            if landed and landed.snapshot.generation >= current.generation
            else current
        )
        return overlay.visible(base.reg, self._state.overlay)

    def _picker_registry(self, _path: str, _now_iso: str) -> tuple[dict[str, Any], str | None]:
        landed = self.scans.latest() or self.scans.wait(PICKER_SCAN_WAIT_SECONDS)
        return self._decided_view(landed), landed.note if landed else None

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
            self.push_screen(TriageScreen(scope))

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
        self._batch_labels.update((ticket, label) for ticket, _ in result.tickets)
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
        self._side_jobs += 1

        def landed(result: Done[T] | Failed) -> None:
            self._post(JobMessage(lambda: on_result(result)))

        token = start_daemon(job, on_progress=lambda _update: None, on_result=landed, name=name)
        self._side_tokens.add(token)
        return token

    def on_job_message(self, message: JobMessage) -> None:
        self._delivered()
        self._side_jobs -= 1
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
        self._rerender()
        if decision == "delete":
            self._toast(f"deleting {what} in {self._delete_grace:g} s — u to undo")
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
        self._next_job_ticket -= 1
        ticket = self._next_job_ticket
        token = CancelToken()
        self._jobs[ticket] = token
        self._job_labels[ticket] = label
        self._set(overlay=overlay.held(self._state.overlay, identity, ticket))
        self._rerender()
        seen = revalidate.seen(thread)
        start_daemon(
            harvest_job(thread, model, revalidate.probe(self._presence)),
            on_progress=lambda _update: None,
            on_result=lambda result: self._post(HarvestMessage(ticket, seen, result)),
            name="rl-harvest",
            token=token,
        )

    def on_harvest_message(self, message: HarvestMessage) -> None:
        self._delivered()
        token = self._jobs.pop(message.ticket, None)
        self._job_labels.pop(message.ticket, None)
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
        label = self._batch_labels.get(tickets[0], plural(len(tickets), "change"))
        for ticket in tickets:
            self._batch_labels.pop(ticket, None)
        decided = sum(1 for _, op in undone if isinstance(op, _DECISION_OPS))
        self._decided = max(0, self._decided - decided)
        self._set(overlay=overlay.removed(self._state.overlay, tickets))
        self._dropped(tickets)
        self._rerender()
        self._toast(f"undone: {label}")

    def _dropped(self, tickets: Sequence[int]) -> None:
        run = self._roundup
        if run is not None and self._roundup_ticket in tickets:
            self._end_roundup(replace(run, stage="cancelled"), None)
        for screen in self._inbox_screens():
            screen.ops_dropped(tickets)

    @property
    def roundup(self) -> RoundupRun | None:
        return self._roundup

    @property
    def roundup_active(self) -> bool:
        return self._roundup is not None and self._roundup.stage in ROUNDUP_ACTIVE

    @property
    def roundup_error(self) -> str | None:
        return self._roundup_error

    @property
    def roundup_stopping(self) -> bool:
        return self._roundup_stopping

    def start_roundup(self, plan: RoundupPlan, scope: EnrichScope) -> None:
        if self.read_only:
            self._toast("read-only: roundup is disabled", warning=True)
            return
        if self.roundup_active:
            self._toast("a roundup is already running", warning=True)
            return
        batch = self.submit_batch([RoundupPhasesOp(plan)], label="roundup")
        if batch is None:
            return
        self._roundup = RoundupRun(plan, scope, "phases", None, None, None)
        self._roundup_ticket = batch.tickets[0][0]
        self._roundup_error = None
        self._roundup_stopping = False
        self._roundup_stale = 0
        self._rerender()

    def cancel_roundup(self) -> None:
        run = self._roundup
        if run is None or run.stage not in ("phases", "enrich"):
            return
        self._roundup_stopping = True
        if run.stage == "phases":
            if self._roundup_ticket is not None:
                self.cancel_op(self._roundup_ticket)
        elif self._enrich_job is not None:
            self._enrich_job.cancel()
        self._rerender()

    def _roundup_progress(self, progress: RoundupProgress) -> None:
        if self._roundup is not None:
            self._roundup = replace(self._roundup, progress=progress)

    def _roundup_step_finished(self, event: Finished) -> None:
        run = self._roundup
        if run is None:
            return
        outcome = event.outcome
        if run.stage == "stamp":
            failed = not outcome.ok or event.internal is not None
            enrich = run.enrich
            if enrich is not None:
                run = replace(run, enrich=replace(enrich, stale=self._roundup_stale))
            self._end_roundup(
                replace(run, stage="failed" if failed else "done"),
                outcome.message if failed else None,
            )
            return
        phases = outcome.phases
        if phases is None or event.internal is not None:
            self._end_roundup(replace(run, stage="failed"), outcome.message)
            return
        run = replace(run, phases=phases)
        if phases.cancelled or self._roundup_stopping or self._leaving:
            self._end_roundup(replace(run, stage="cancelled"), None)
            return
        if not self._hold_roundup_lock():
            self._end_roundup(replace(run, stage="failed"), "another roundup is running")
            return
        model = self.model_for("enrich")
        ids = run.plan.enrich_ids(run.scope)
        if model is None or not ids:
            self._stamp_roundup(run)
            return
        self._roundup = replace(
            run, stage="enrich", progress=RoundupProgress("enrich", 0, len(ids))
        )
        self._enrich_job = start_daemon(
            enrich_job(run.plan, ids, model, self._apply_classified),
            on_progress=lambda progress: self._post(EnrichProgressMessage(progress)),
            on_result=lambda result: self._post(EnrichDoneMessage(result)),
            name="rl-enrich",
        )

    def _apply_classified(self, batch: tuple[Classified, ...]) -> None:
        self.writer.submit([ClassifyOp(batch)], label="classify")

    def on_enrich_progress_message(self, message: EnrichProgressMessage) -> None:
        self._delivered()
        if self._roundup is not None and self._roundup.stage == "enrich":
            self._roundup_progress(message.progress)
            self._rerender()

    def on_enrich_done_message(self, message: EnrichDoneMessage) -> None:
        self._delivered()
        token, self._enrich_job = self._enrich_job, None
        run = self._roundup
        if run is None or run.stage != "enrich":
            self._leave_if_settled()
            return
        match message.result:
            case Done(value=outcome):
                run = replace(run, enrich=outcome)
                stopped = token is not None and token.is_cancelled()
                if outcome.cancelled or stopped or self._leaving:
                    self._end_roundup(replace(run, stage="cancelled"), None)
                else:
                    self._stamp_roundup(run)
            case Failed(error=error):
                self._end_roundup(replace(run, stage="failed"), f"enrich failed: {error}")
        self._leave_if_settled()

    def _stamp_roundup(self, run: RoundupRun) -> None:
        batch = self.submit_batch([StampRoundupOp(run.plan.now_iso)], label="roundup stamp")
        if batch is None:
            self._end_roundup(replace(run, stage="cancelled"), None)
            return
        self._roundup = replace(run, stage="stamp", progress=RoundupProgress("stamp", 0, 1))
        self._roundup_ticket = batch.tickets[0][0]

    def _end_roundup(self, run: RoundupRun, error: str | None) -> None:
        self._release_roundup_lock()
        self._roundup = run
        self._roundup_ticket = None
        self._roundup_error = error
        self._roundup_stopping = False
        match run.stage:
            case "done":
                self._toast("roundup finished")
            case "cancelled":
                self._toast("roundup cancelled — not stamped; run it again to finish")
            case _:
                self._toast(f"roundup failed: {error}", warning=True)
        self._rerender()
        if not self._leaving:
            self.rescan()

    def _hold_roundup_lock(self) -> bool:
        if self._roundup_lock is not None:
            return True
        stack = ExitStack()
        if not stack.enter_context(stateio.roundup_lock()):
            stack.close()
            return False
        self._roundup_lock = stack
        return True

    def _release_roundup_lock(self) -> None:
        stack, self._roundup_lock = self._roundup_lock, None
        if stack is not None:
            stack.close()

    def start_triage_handoff(self, project: ProjectRef) -> None:
        self._triage_flow(project)

    @work(exclusive=True, group="triage-handoff")
    async def _triage_flow(self, project: ProjectRef) -> None:
        agent = await self.push_screen_wait(
            AgentPickModal(f"Triage {project.label} with which agent?", resume.launchable_agents())
        )
        if agent is None:
            return
        members = sorted(
            (
                t
                for t in self._state.snapshot.reg["threads"]
                if overview.group_key(t) == project.key
            ),
            key=lambda t: t.get("mtime") or "",
            reverse=True,
        )
        candidates = [path for t in members for path in (t.get("gitRoot"), t.get("project"))]
        self.exit_with(
            AgentHandoffIntent(
                agent=agent,
                cwd=handoff_cwd(candidates, Path.home()),
                prompt=triage_prompt(project.label, project.key),
                purpose="triage",
                label=project.label,
            )
        )

    def exit_with(self, intent: ExitIntent) -> None:
        if self._leaving:
            self._toast("leaving — that choice was not applied", warning=True)
            return
        self._leave(intent)

    async def action_quit(self) -> None:
        if self._leaving:
            self._offer_discard()
            return
        self._leave(None)

    def _settled(self) -> bool:
        return (
            self.writer.idle
            and not self._jobs
            and self._enrich_job is None
            and self._side_jobs == 0
        )

    def _leave(self, intent: ExitIntent | None) -> None:
        self._leaving = True
        self._leave_intent = intent
        if self._enrich_job is not None:
            self._enrich_job.cancel()
        self._cancel_side_jobs()
        if self._settled():
            self.exit(intent)
            return
        self.writer.flush_grace()
        self._rerender()

    def _cancel_side_jobs(self) -> None:
        for token in self._side_tokens:
            token.cancel()

    def abort_background_work(self) -> None:
        self._cancel_side_jobs()
        if self._enrich_job is not None:
            self._enrich_job.cancel()
        for token in self._jobs.values():
            token.cancel()
        self.writer.discard_pending()
        self.writer.cancel_current()

    def _leave_if_settled(self) -> None:
        self._rerender()
        if self._leaving and self._settled():
            self.exit(self._leave_intent)

    def _not_started(self) -> int:
        return self.writer.pending - (self._running_ticket is not None) + len(self._jobs)

    def show_leaving(self, screen: Screen[Any]) -> None:
        banner = next(iter(screen.query("#leaving").results(Static)), None)
        if banner is None:
            return
        banner.display = self._leaving
        if self._leaving:
            pending = self.writer.pending + len(self._jobs) + (self._enrich_job is not None)
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
        dropped = self.writer.discard_pending()
        for token in self._jobs.values():
            token.cancel()
        self.writer.cancel_current()
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
        label = group.label if group is not None else "all harvests"
        self.exit_with(
            AgentHandoffIntent(
                agent=agent,
                cwd=cwd,
                prompt=publish_prompt(group.label if group is not None else None),
                purpose="publish",
                label=label,
            )
        )
