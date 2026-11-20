from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.widgets import Button, Footer, Header, Log, Static

from ...tui.background import CancelToken, Done, Failed
from ...tui.runner import CommandRun
from ...usage.remote_render import InboxReport, inbox_report_text, inbox_reports
from ...usage.remote_types import InvalidPayload, Provider, Scope
from ..base import InboxScreen
from .output import MIN_COLUMNS, joined_output, run_commands, status_line

USAGE_ARGVS = (("usage",), ("usage", "windows"))


class UsageView(StrEnum):
    LOCAL = "local"
    SUBSCRIPTION = "subscription"
    TOKEN = "token"

    @property
    def button_id(self) -> str:
        return f"usage-{self.value}"


VIEWS = tuple(UsageView)


class UsageScreen(InboxScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("c", "cancel", "cancel"),
        Binding("r", "refresh", "refresh"),
        Binding("tab", "cycle_view(1)", "next view"),
        Binding("shift+tab", "cycle_view(-1)", "previous view", show=False),
    ]

    DEFAULT_CSS = """
    UsageScreen #usage-views {
        height: 1;
    }
    UsageScreen #usage-views Button {
        width: 1fr;
        min-width: 0;
        height: 1;
        border: none;
        padding: 0;
    }
    UsageScreen #usage-views #usage-subscription {
        min-width: 14;
    }
    UsageScreen #usage-views Button.-primary {
        text-style: bold underline;
    }
    UsageScreen #output-status {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }
    UsageScreen #output-log {
        height: 1fr;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.view = UsageView.LOCAL
        self._generation = 0
        self._token: CancelToken | None = None
        self._local: tuple[str, str] | None = None
        self._reports: dict[tuple[Provider, Scope], InboxReport] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="usage-views"):
            for view in VIEWS:
                yield Button(
                    view.value.title(),
                    id=view.button_id,
                    variant="primary" if view is self.view else "default",
                )
        yield Static("", id="output-status")
        yield Log(id="output-log", auto_scroll=False)
        yield Footer()

    def on_mount(self) -> None:
        self._start()

    def _start(self, *, refresh: bool = False) -> None:
        self._generation += 1
        generation = self._generation
        argvs: tuple[tuple[str, ...], ...]
        if self.view is UsageView.LOCAL:
            argvs = USAGE_ARGVS
            status = "running… c to cancel"
            message = "running rl usage, then rl usage windows…"
        else:
            flag = "--refresh" if refresh else "--cached"
            argvs = (("usage", self.view.value, flag, "--_inbox"),)
            status = "Refreshing… c to cancel" if refresh else "Reading saved reports… c to cancel"
            message = ""
        self.query_one("#output-log", Log).focus()
        columns = max(MIN_COLUMNS, self.size.width or 80)
        self._token = self.inbox.run_job(
            run_commands(argvs, columns, stdout_only=self.view is not UsageView.LOCAL),
            name="rl-usage",
            on_result=lambda result: self._finished(generation, refresh, result),
        )
        previous = self._previous(self.inbox.clock()) if self.view is not UsageView.LOCAL else ""
        self._show(status, message, previous=previous)

    def _discard_job(self) -> None:
        if self._token is not None:
            self._token.cancel()
            self._generation += 1
            self._token = None

    def _finished(
        self, generation: int, refresh: bool, result: Done[tuple[CommandRun, ...]] | Failed
    ) -> None:
        if generation != self._generation or self._token is None:
            return
        cancelled = self._token.is_cancelled()
        self._token = None
        if not self.is_attached:
            return
        runs = result.value if isinstance(result, Done) else ()
        if self.view is UsageView.LOCAL:
            output = joined_output(runs)
            status = (
                status_line(runs, cancelled=cancelled) if isinstance(result, Done) else "failed"
            )
            output = output or ("Could not run local usage." if not cancelled else "")
            if (
                not cancelled
                and len(runs) == len(USAGE_ARGVS)
                and all(run.returncode is not None for run in runs)
            ):
                self._local = (status, output)
            self._show(status, output)
            return

        self._finish_remote(runs, refresh=refresh, cancelled=cancelled)

    def _finish_remote(
        self, runs: tuple[CommandRun, ...], *, refresh: bool, cancelled: bool
    ) -> None:
        now = self.inbox.clock()
        operation = "Refresh" if refresh else "Cache read"
        if cancelled:
            self._show(f"{operation} cancelled · r to refresh", "", previous=self._previous(now))
            return

        code = runs[0].returncode if len(runs) == 1 else None
        try:
            reports = inbox_reports(runs[0].output) if code in (0, 1) else ()
            if not reports or any(not self._in_view(report) for report in reports):
                raise InvalidPayload
        except (ValueError, RecursionError):
            self._show(
                f"{operation} failed · r to refresh",
                "Could not read the usage report.",
                previous=self._previous(now),
            )
            return

        covered = set()
        for report in reports:
            result = report.result
            key = (result.provider, result.scope)
            retained = self._reports.get(key)
            if (
                result.data is not None
                and result.fetched_at is not None
                and (retained is None or result.fetched_at >= (retained.result.fetched_at or ""))
            ):
                covered.add(key)
                self._reports[key] = report
        output = "\n\n".join(inbox_report_text(report, now) for report in reports)
        status = (
            f"{operation} failed"
            if code == 1
            else ("Refresh complete" if refresh else "Saved reports")
        )
        self._show(f"{status} · r to refresh", output, previous=self._previous(now, covered))

    def _in_view(self, report: InboxReport) -> bool:
        return (report.result.scope == "subscription") == (self.view is UsageView.SUBSCRIPTION)

    def _previous(self, now: datetime, covered: set[tuple[Provider, Scope]] | None = None) -> str:
        return "\n\n".join(
            inbox_report_text(report, now, previous=True)
            for key, report in self._reports.items()
            if self._in_view(report) and (covered is None or key not in covered)
        )

    def _show(self, status: str, output: str, *, previous: str = "") -> None:
        self.query_one("#output-status", Static).update(status)
        log = self.query_one("#output-log", Log)
        log.clear()
        parts = [
            part for part in (output, f"Previous report\n{previous}" if previous else "") if part
        ]
        if parts:
            log.write_lines(("\n\n".join(parts),))
        self.refresh_bindings()

    def _select(self, view: UsageView) -> None:
        if view is self.view:
            self.query_one("#output-log", Log).focus()
            return
        self._discard_job()
        self.query_one(f"#{self.view.button_id}", Button).variant = "default"
        self.view = view
        self.query_one(f"#{view.button_id}", Button).variant = "primary"
        if view is UsageView.LOCAL and self._local is not None:
            self.query_one("#output-log", Log).focus()
            self._show(*self._local)
        else:
            self._start()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        for view in VIEWS:
            if view.button_id == event.button.id:
                self._select(view)
                return

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "cancel":
            return self._token is not None and not self._token.is_cancelled()
        if action == "refresh":
            return self._token is None
        return True

    def action_cycle_view(self, step: int) -> None:
        index = (VIEWS.index(self.view) + step) % len(VIEWS)
        self._select(VIEWS[index])

    def action_cancel(self) -> None:
        if self._token is not None:
            self._token.cancel()
            self.query_one("#output-status", Static).update("Cancelling…")
            self.refresh_bindings()

    def action_refresh(self) -> None:
        if self._token is None:
            self._start(refresh=self.view is not UsageView.LOCAL)

    def action_back(self) -> None:
        self._discard_job()
        self.app.pop_screen()

    def on_unmount(self) -> None:
        self._discard_job()
