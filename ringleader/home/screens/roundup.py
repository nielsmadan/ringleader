from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar, Literal

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, ProgressBar, Static

from ... import roundup
from ...config import load_config
from ...roundup import EnrichScope, PhasesResult, RoundupPhase, RoundupPlan, duration
from ...tui.background import CancelToken, Done, Failed
from ...tui.modals import ChoiceModal, ChoiceOption, ConfirmModal
from .. import overlay
from ..base import InboxScreen, RoundupRun
from ..policy import SCAN_FRESH_SECONDS
from ..state import plural

type ScopeChoice = EnrichScope | Literal["cancel"]

CANCEL: ScopeChoice = "cancel"

STAGES: tuple[RoundupPhase, ...] = ("sweep", "prune", "purge", "enrich", "stamp")
ACTIVE = frozenset({"phases", "enrich", "stamp"})
ERRORS_SHOWN = 5
PLAN_HINT = "enter run · escape back"
RUN_HINT = "c stop after the current session · escape back — it keeps running"
DONE_HINT = "enter plan again · escape back"


def run_scope(plan: RoundupPlan) -> EnrichScope:
    return "all" if plan.engine is not None else "none"


def _purges(plan: RoundupPlan) -> str:
    count = len(plan.purge_ids)
    handoffs = len(plan.handoff_paths)
    return f"{count} and {plural(handoffs, 'handoff export')}" if handoffs else str(count)


def confirm_text(plan: RoundupPlan, scope: EnrichScope) -> str:
    return (
        f"Run the roundup? deletes {len(plan.prune_items)}, purges {_purges(plan)}, "
        f"archives {len(plan.sweep_items)}, enriches {len(plan.enrich_ids(scope))}. [y/n]"
    )


def warning_title(plan: RoundupPlan) -> str:
    return (
        f"Run the roundup? deletes {len(plan.prune_items)}, purges {_purges(plan)}, "
        f"archives {len(plan.sweep_items)}.\n"
        f"Enrich {len(plan.enrich_all_ids)} sessions? "
        f"about {duration(plan.estimated_seconds('all'))}"
    )


def scope_options(plan: RoundupPlan) -> list[ChoiceOption[ScopeChoice]]:
    every = len(plan.enrich_all_ids)
    recent = len(plan.enrich_recent_ids)
    return [
        ChoiceOption(
            f"Run, enrich all {every} — about {duration(plan.estimated_seconds('all'))}", "all"
        ),
        ChoiceOption(
            f"Run, enrich the {recent} from the last {roundup.RECENT_DAYS} days — about "
            f"{duration(plan.estimated_seconds('recent'))}",
            "recent",
        ),
        ChoiceOption("Run, skip enrich", "none"),
        ChoiceOption("Cancel", CANCEL),
    ]


def purged_text(phases: PhasesResult) -> str:
    handoffs = phases.handoffs_purged
    return f"purged {phases.purged}" + (f" (+{handoffs} handoff exports)" if handoffs else "")


def _planned(run: RoundupRun, stage: RoundupPhase) -> str:
    plan = run.plan
    match stage:
        case "sweep":
            return f"archive {plural(len(plan.sweep_items), 'finished session')}"
        case "prune":
            return f"delete {plural(len(plan.prune_items), 'tiny session')}"
        case "purge":
            line = f"purge {plural(len(plan.purge_ids), 'archived session')}"
            handoffs = len(plan.handoff_paths)
            return f"{line}, and {plural(handoffs, 'handoff export')}" if handoffs else line
        case "enrich":
            ids = plan.enrich_ids(run.scope)
            if not ids or plan.engine is None:
                return "enrich: skipped"
            return f"enrich {plural(len(ids), 'session')} ({run.scope})"
        case "stamp":
            return "stamp the roundup"


def _result(run: RoundupRun, stage: RoundupPhase) -> str | None:
    phases = run.phases
    counts = (
        {}
        if phases is None
        else {
            "sweep": f"archived {phases.swept}",
            "prune": f"deleted {phases.deleted}",
            "purge": purged_text(phases),
        }
    )
    if stage == "enrich" and run.enrich is not None:
        stale = f", {run.enrich.stale} changed meanwhile" if run.enrich.stale else ""
        return f"{run.enrich.failed} failed{stale}"
    return counts.get(stage)


def _stage_state(run: RoundupRun, stage: RoundupPhase) -> Literal["done", "running", "waiting"]:
    if run.stage == "done":
        return "done"
    index = STAGES.index(stage)
    phases = run.phases
    if phases is not None and not phases.cancelled and index <= STAGES.index("purge"):
        return "done"
    progress = run.progress
    if progress is None:
        return "waiting"
    current = STAGES.index(progress.phase)
    if index < current or (index == current and progress.detail == "done"):
        return "done"
    if index == current and run.stage in ACTIVE:
        return "running"
    return "waiting"


def _stage_line(run: RoundupRun, stage: RoundupPhase) -> str:
    marks = {"done": "✓", "running": "▸", "waiting": " "}
    state = _stage_state(run, stage)
    line = f"{marks[state]} {stage:<7} {_planned(run, stage)}"
    result = _result(run, stage)
    if state == "done" and result is not None:
        return f"{line} — {result}"
    if state == "running" and run.progress is not None and run.progress.total:
        return f"{line} — {run.progress.done} / {run.progress.total}"
    return line


def stage_lines(run: RoundupRun) -> list[str]:
    return [_stage_line(run, stage) for stage in STAGES]


def _enrich_summary(run: RoundupRun) -> list[str]:
    outcome = run.enrich
    if outcome is None:
        return []
    total = len(run.plan.enrich_ids(run.scope))
    if outcome.aborted:
        head = f"enrich stopped: {outcome.aborted}"
    elif outcome.cancelled:
        head = "enrich stopped early — the rest stays pending for the next roundup"
    else:
        applied = total - outcome.failed - outcome.stale
        stale = f", {outcome.stale} changed meanwhile" if outcome.stale else ""
        head = f"enriched {applied} of {total} ({outcome.failed} failed{stale})"
    errors = [f"  {error}" for error in outcome.errors[:ERRORS_SHOWN]]
    rest = len(outcome.errors) - ERRORS_SHOWN
    return [head, *errors, *([f"  … and {rest} more"] if rest > 0 else [])]


def summary_lines(run: RoundupRun, error: str | None) -> list[str]:
    head = {
        "done": "Roundup finished.",
        "cancelled": "Roundup cancelled — not stamped; run it again to finish.",
        "failed": f"Roundup failed: {error}",
    }.get(run.stage, "")
    phases = run.phases
    counts = (
        [f"archived {phases.swept}, deleted {phases.deleted}, {purged_text(phases)}"]
        if phases is not None
        else []
    )
    skipped = (
        ["Skipped:", *(f"  {seen.label}: {reason}" for seen, reason in phases.skipped)]
        if phases is not None and phases.skipped
        else []
    )
    warnings = (
        ["Warnings:", *(f"  {warning}" for warning in phases.warnings)]
        if phases is not None and phases.warnings
        else []
    )
    return [head, *counts, *_enrich_summary(run), *skipped, *warnings]


class RoundupScreen(InboxScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "run", "run"),
        Binding("c", "stop", "stop"),
    ]

    DEFAULT_CSS = """
    RoundupScreen #roundup-title, RoundupScreen #roundup-body, RoundupScreen #roundup-hint {
        padding: 0 1;
    }
    RoundupScreen #roundup-title {
        text-style: bold;
    }
    RoundupScreen #roundup-hint {
        color: $text-muted;
        margin-top: 1;
    }
    RoundupScreen #roundup-bar {
        padding: 0 1;
        display: none;
    }
    RoundupScreen #roundup-scroll {
        height: 1fr;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._plan: RoundupPlan | None = None
        self._plan_error: str | None = None
        self._planning = False
        self._requested = False
        self._dismissed: RoundupRun | None = None

    @property
    def plan(self) -> RoundupPlan | None:
        return self._plan

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(Text("Roundup"), id="roundup-title")
        yield ProgressBar(id="roundup-bar", show_eta=False)
        with VerticalScroll(id="roundup-scroll"):
            yield Static("", id="roundup-body")
            yield Static("", id="roundup-hint")
        yield Footer()

    def on_mount(self) -> None:
        self.sync()

    def _shown_run(self) -> RoundupRun | None:
        run = self.inbox.roundup
        return run if run is not None and run is not self._dismissed else None

    def render_data(self) -> None:
        if not self.query("#roundup-body"):
            return
        self.refresh_bindings()
        run = self._shown_run()
        if run is not None:
            self._render_run(run)
            return
        self._advance()
        self._render_plan()

    def render_status(self) -> None:
        self.render_data()

    def _show(self, lines: list[str], hint: str) -> None:
        self.query_one("#roundup-body", Static).update(Text("\n".join(lines)))
        self.query_one("#roundup-hint", Static).update(Text(hint))

    def _render_run(self, run: RoundupRun) -> None:
        active = run.stage in ACTIVE
        bar = self.query_one("#roundup-bar", ProgressBar)
        progress = run.progress
        bar.display = active and progress is not None
        if active and progress is not None:
            bar.update(total=progress.total or 1, progress=progress.done if progress.total else 1)
        title = "Roundup running" if active else "Roundup"
        self.query_one("#roundup-title", Static).update(Text(title))
        lines = stage_lines(run)
        if active:
            stopping = ["", "stopping after the current session…"]
            self._show([*lines, *(stopping if self.inbox.roundup_stopping else [])], RUN_HINT)
        else:
            self._show([*lines, "", *summary_lines(run, self.inbox.roundup_error)], DONE_HINT)

    def _render_plan(self) -> None:
        self.query_one("#roundup-bar", ProgressBar).display = False
        self.query_one("#roundup-title", Static).update(Text("Roundup"))
        scans = self.inbox.scans
        if self._plan_error is not None:
            self._show([f"couldn't plan the roundup: {self._plan_error}"], "escape back")
        elif self._plan is not None:
            self._show(roundup.describe_plan(self._plan), PLAN_HINT)
        elif self._requested and not self._planning and not scans.running:
            self._show(["the scan did not finish — ctrl+r to scan again"], "escape back")
        else:
            self._show(["scanning and planning…"], "escape back")

    def _advance(self) -> None:
        if self._plan is not None or self._planning or self._plan_error is not None:
            return
        inbox = self.inbox
        landed = inbox.scans.latest(SCAN_FRESH_SECONDS)
        if landed is None:
            if not inbox.scans.running and not self._requested:
                self._requested = True
                inbox.rescan()
            return
        self._planning = True
        reg = overlay.visible(inbox.state.snapshot.reg, inbox.state.overlay)
        registry_path = inbox.registry_path
        now_iso = inbox.now_iso()
        now_epoch = inbox.now_epoch()
        note = landed.note
        unavailable = landed.unavailable_sources
        model_for = inbox.model_for
        configured_model = inbox.configured_model
        read_only = inbox.read_only

        def job(_token: CancelToken, _progress: Callable[[None], None]) -> RoundupPlan:
            return roundup.plan_roundup(
                registry_path,
                reg,
                now_iso,
                scan_note=note,
                model=model_for("enrich"),
                now_epoch=now_epoch,
                retention_days=load_config().archive_retention_days,
                unavailable_sources=unavailable,
                read_only_model=configured_model("enrich") if read_only else None,
            )

        inbox.run_job(job, name="rl-roundup-plan", on_result=self._planned)

    def _planned(self, result: Done[RoundupPlan] | Failed) -> None:
        self._planning = False
        match result:
            case Done(value=plan):
                self._plan = plan
            case Failed(error=error):
                self._plan_error = str(error)
        if self.is_attached:
            self.render_data()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        active = self.inbox.roundup_active and self._shown_run() is not None
        if action == "stop":
            return active
        if action == "run":
            return not active
        return True

    def action_run(self) -> None:
        inbox = self.inbox
        run = self._shown_run()
        if run is not None:
            if run.stage not in ACTIVE:
                self._dismissed = run
                self._plan = None
                self._plan_error = None
                self._requested = False
                self.render_data()
            return
        if inbox.read_only:
            self.notify("read-only: roundup is disabled", severity="warning", markup=False)
            return
        plan = self._plan
        if plan is None:
            return
        if plan.enrich_warning:
            options = scope_options(plan)
            cancel = next(i for i, option in enumerate(options) if option.value == CANCEL)
            self.app.push_screen(
                ChoiceModal(warning_title(plan), options, initial=cancel),
                callback=lambda choice: self._chosen(plan, choice),
            )
            return
        scope = run_scope(plan)
        self.app.push_screen(
            ConfirmModal(Text(confirm_text(plan, scope))),
            callback=lambda confirmed: self._chosen(plan, scope if confirmed else "cancel"),
        )

    def _chosen(self, plan: RoundupPlan, choice: ScopeChoice | None) -> None:
        if choice is None or choice == "cancel":
            return
        self._plan = None
        self.inbox.start_roundup(plan, choice)
        self.render_data()

    def action_stop(self) -> None:
        if self._shown_run() is not None and self.inbox.roundup_active:
            self.inbox.cancel_roundup()
