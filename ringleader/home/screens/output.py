from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Footer, Header, Log, Static

from ...tui import runner
from ...tui.background import CancelToken, Done, Failed
from ..base import InboxScreen

MIN_COLUMNS = 40


def status_line(runs: Sequence[runner.CommandRun], *, cancelled: bool) -> str:
    seconds = sum(run.seconds for run in runs)
    if cancelled or any(run.returncode is None for run in runs):
        return f"cancelled · {seconds:.1f}s"
    code = next((run.returncode for run in runs if run.returncode), 0)
    return f"exit {code} · {seconds:.1f}s"


def joined_output(runs: Sequence[runner.CommandRun]) -> str:
    return "\n\n".join(run.output.rstrip("\n") for run in runs)


def run_commands(
    argvs: Sequence[Sequence[str]], columns: int
) -> Callable[[CancelToken, Callable[[None], None]], tuple[runner.CommandRun, ...]]:
    def job(token: CancelToken, _progress: Callable[[None], None]) -> tuple[runner.CommandRun, ...]:
        runs: list[runner.CommandRun] = []
        for argv in argvs:
            if token.is_cancelled():
                break
            runs.append(runner.run_rl(argv, cancelled=token.is_cancelled, columns=columns))
        return tuple(runs)

    return job


class OutputScreen(InboxScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("c", "cancel", "cancel"),
        Binding("r", "rerun", "re-run"),
        Binding("escape", "back", "back"),
    ]

    DEFAULT_CSS = """
    OutputScreen #output-title, OutputScreen #output-status {
        padding: 0 1;
    }
    OutputScreen #output-title {
        text-style: bold;
    }
    OutputScreen #output-status {
        color: $text-muted;
    }
    OutputScreen #output-log {
        height: 1fr;
    }
    """

    def __init__(self, argv: Sequence[str], title: str) -> None:
        super().__init__()
        self.argv = tuple(argv)
        self.heading = title
        self._token: CancelToken | None = None
        self._generation = 0
        self._runs: tuple[runner.CommandRun, ...] | None = None
        self._error: str | None = None
        self._cancelled = False
        self._shown: tuple[int, bool, bool] | None = None

    def commands(self) -> Sequence[Sequence[str]]:
        return [self.argv]

    @property
    def running(self) -> bool:
        return self._token is not None

    @property
    def runs(self) -> tuple[runner.CommandRun, ...] | None:
        return self._runs

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(Text(self.heading), id="output-title")
        yield Static("", id="output-status")
        yield Log(id="output-log", auto_scroll=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#output-log", Log).focus()
        self._start()

    def _start(self) -> None:
        self._generation += 1
        generation = self._generation
        self._runs = None
        self._error = None
        self._cancelled = False
        columns = max(MIN_COLUMNS, self.size.width or 80)
        self._token = self.inbox.run_job(
            run_commands(self.commands(), columns),
            name="rl-output",
            on_result=lambda result: self._finished(generation, result),
        )
        self.render_data()

    def _finished(
        self, generation: int, result: Done[tuple[runner.CommandRun, ...]] | Failed
    ) -> None:
        if generation != self._generation:
            return
        self._token = None
        match result:
            case Done(value=runs):
                self._runs = runs
            case Failed(error=error):
                self._error = f"could not run: {type(error).__name__}: {error}"
        if self.is_attached:
            self.render_data()

    def render_data(self) -> None:
        shown = (self._generation, self.running, self._cancelled)
        if not self.query("#output-log") or shown == self._shown:
            return
        self._shown = shown
        log = self.query_one("#output-log", Log)
        status = self.query_one("#output-status", Static)
        log.clear()
        if self.running:
            status.update(Text("running… c to cancel"))
            log.write_line(f"running {self.heading}…")
        elif self._runs is not None:
            status.update(Text(status_line(self._runs, cancelled=self._cancelled)))
            log.write(joined_output(self._runs) or "(no output)")
        else:
            status.update(Text("failed"))
            log.write_line(self._error or "(no output)")
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "cancel":
            return self.running
        if action == "rerun":
            return not self.running
        return True

    def action_cancel(self) -> None:
        if self._token is not None:
            self._cancelled = True
            self._token.cancel()

    def action_rerun(self) -> None:
        if not self.running:
            self._start()

    def action_back(self) -> None:
        self.action_cancel()
        self.app.pop_screen()

    def on_unmount(self) -> None:
        if self._token is not None:
            self._token.cancel()
