from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Footer, Header, OptionList, ProgressBar, Static

from ... import lifecycle, overview, prune, registry, resume, revalidate
from ...agents import source_of
from ...config import load_config
from ...decisions import Outcome, PruneOp
from ...revalidate import Seen
from ...roundup import RoundupProgress
from ...tui.keys import nav_bindings
from ...tui.modals import ConfirmModal
from ...tui.rows import highlighted_key, set_rows
from ..base import InboxScreen
from ..state import plural, project_label
from ..writer import Finished

AGE_WIDTH = 9
AGENT_WIDTH = 9


@dataclass(frozen=True)
class PruneResult:
    deleted: int
    skipped: tuple[tuple[Seen, str], ...]
    stopped: bool
    failure: str | None


def prune_rows(reg: dict[str, Any], now_iso: str, now_epoch: float) -> list[dict[str, Any]]:
    swept = {registry.identity(t) for t in lifecycle.due(reg, now_epoch)}
    return sorted(
        (t for t in prune.prune_candidates(reg, now_iso) if registry.identity(t) not in swept),
        key=lambda t: t.get("mtime") or "",
    )


def prune_row(thread: dict[str, Any], now_iso: str) -> Text:
    text = Text(f"{overview.age_human(thread.get('mtime'), now_iso):<{AGE_WIDTH}}", style="dim")
    text.append(f"{thread.get('messageCount') or 0:>3} msgs  ")
    text.append(f"{resume.agent_name(source_of(thread)):<{AGENT_WIDTH}}", style="bold")
    text.append(resume.label(thread))
    text.append(f"  {project_label(thread)}", style="dim")
    return text


def prune_title(count: int) -> str:
    return (
        f"{plural(count, 'tiny session')} older than {prune.DEFAULT_OLDER_THAN_DAYS} days "
        f"(fewer than {prune.DEFAULT_MAX_TURNS} messages) — D to delete all"
    )


def purge_line(count: int) -> str:
    return f"{plural(count, 'archived session')} past retention — run the roundup to purge"


def result_lines(result: PruneResult) -> list[str]:
    head = f"deleted {result.deleted}, skipped {len(result.skipped)}"
    notes = [
        *(["stopped early — the rest is still listed"] if result.stopped else []),
        *([result.failure] if result.failure else []),
    ]
    return [head, *notes, *(f"  {seen.label}: {reason}" for seen, reason in result.skipped)]


def prune_result(outcome: Outcome) -> PruneResult:
    phases = outcome.phases
    if phases is None:
        return PruneResult(0, outcome.refused, stopped=False, failure=outcome.message)
    return PruneResult(phases.deleted, phases.skipped, stopped=phases.cancelled, failure=None)


class PruneScreen(InboxScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        *nav_bindings(),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("D", "delete_all", "delete all"),
        Binding("c", "stop", "stop"),
    ]

    DEFAULT_CSS = """
    PruneScreen #prune-title, PruneScreen #prune-purge, PruneScreen #prune-status,
    PruneScreen #prune-empty {
        padding: 0 1;
    }
    PruneScreen #prune-title {
        text-style: bold;
    }
    PruneScreen #prune-purge {
        color: $text-muted;
    }
    PruneScreen #prune-bar {
        padding: 0 1;
        display: none;
    }
    PruneScreen #prune-rows {
        height: 1fr;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []
        self._retention_days = 0
        self._ticket: int | None = None
        self._progress: RoundupProgress | None = None
        self._result: PruneResult | None = None
        self._basis: tuple[int, datetime] | None = None

    @property
    def result(self) -> PruneResult | None:
        return self._result

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="prune-title")
        yield Static("", id="prune-purge")
        yield Static("", id="prune-status")
        yield ProgressBar(id="prune-bar", show_eta=False)
        yield OptionList(id="prune-rows", compact=True)
        yield Static(Text("Nothing to prune."), id="prune-empty")
        yield Footer()

    def on_mount(self) -> None:
        self._retention_days = load_config().archive_retention_days
        self.query_one("#prune-rows", OptionList).focus()
        self.sync()

    def render_data(self) -> None:
        if not self.query("#prune-rows"):
            return
        inbox = self.inbox
        basis = inbox.render_basis()
        shown = (basis.view, basis.minute)
        if shown != self._basis:
            self._basis = shown
            self._render_rows(inbox.view(), basis.now)
        self._render_status()

    def _render_rows(self, view: dict[str, Any], now: datetime) -> None:
        now_iso = now.isoformat()
        self._rows = prune_rows(view, now_iso, now.timestamp())
        self.query_one("#prune-title", Static).update(Text(prune_title(len(self._rows))))
        purge = self.query_one("#prune-purge", Static)
        purging = (
            prune.archive_purge_candidates(view, now_iso, retention_days=self._retention_days)
            if self._retention_days > 0
            else []
        )
        purge.update(Text(purge_line(len(purging))))
        purge.display = bool(purging)
        rows = self.query_one("#prune-rows", OptionList)
        set_rows(
            rows,
            [(registry.qualified_id(t), prune_row(t, now_iso)) for t in self._rows],
            keep=highlighted_key(rows),
        )
        rows.display = bool(self._rows)
        self.query_one("#prune-empty", Static).display = not self._rows

    def _render_status(self) -> None:
        status = self.query_one("#prune-status", Static)
        bar = self.query_one("#prune-bar", ProgressBar)
        progress = self._progress
        running = self._ticket is not None
        bar.display = running
        if running:
            done, total = (progress.done, progress.total) if progress else (0, 0)
            bar.update(total=total or 1, progress=done if total else 0)
            status.update(Text("pruning… c to stop after the current batch"))
        elif self._result is not None:
            status.update(Text("\n".join(result_lines(self._result))))
        status.display = running or self._result is not None
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return self._ticket is not None if action == "stop" else True

    def action_cursor_down(self) -> None:
        self.query_one("#prune-rows", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#prune-rows", OptionList).action_cursor_up()

    def action_delete_all(self) -> None:
        inbox = self.inbox
        if inbox.read_only:
            self.notify("read-only: prune is disabled", severity="warning", markup=False)
            return
        if self._ticket is not None:
            self.notify("a prune is already running", markup=False)
            return
        items = tuple(revalidate.seen(t) for t in self._rows)
        if not items:
            return
        message = Text(
            f"Delete {plural(len(items), 'session')} permanently? Nothing is harvested. [y/n]"
        )

        def answered(confirmed: bool | None) -> None:
            if confirmed:
                self._start(items)

        self.app.push_screen(ConfirmModal(message), callback=answered)

    def _start(self, items: tuple[Seen, ...]) -> None:
        batch = self.inbox.submit_batch(
            [PruneOp(items)], label=f"prune {plural(len(items), 'session')}"
        )
        if batch is None:
            return
        self._ticket = batch.tickets[0][0]
        self._progress = None
        self._result = None
        self.render_data()

    def action_stop(self) -> None:
        if self._ticket is not None and self.inbox.cancel_op(self._ticket):
            message = (
                "stopping after the current batch…"
                if self._ticket is not None
                else "prune cancelled before it started — nothing was deleted"
            )
            self.notify(message, markup=False)

    def op_progress(self, ticket: int, progress: RoundupProgress) -> None:
        if ticket == self._ticket:
            self._progress = progress
            if self.is_attached:
                self._render_status()

    def ops_dropped(self, tickets: Sequence[int]) -> None:
        if self._ticket in tickets:
            self._ticket = None
            if self.is_attached:
                self._render_status()

    def op_finished(self, event: Finished) -> None:
        if event.ticket != self._ticket:
            return
        self._ticket = None
        self._result = prune_result(event.outcome)
