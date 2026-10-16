from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Thread
from typing import Any, ClassVar, cast

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Label, OptionList

from .. import overview, refresh, registry, resume
from ..presence import PresenceClient
from ..resume import Scope, SessionAction, agent_name, launchable_agents
from ..tui.keys import nav_bindings
from ..tui.modals import AgentChoice, AgentModal, ConfirmModal, agent_row_text
from ..tui.types import Clock, Presence, Refresher

__all__ = [
    "AgentChoice",
    "AgentModal",
    "Choice",
    "ConfirmModal",
    "PickerApp",
    "PickerScreen",
    "agent_row_text",
    "ask_resume_choice",
    "empty_hint",
    "launchable_agents",
    "live_session_ids",
    "row_text",
]

_WIDEN_HINTS: dict[Scope, tuple[str, ...]] = {
    Scope.CWD: ("ctrl+r for this tree", "ctrl+a for everywhere"),
    Scope.RECURSIVE: ("ctrl+a for everywhere",),
    Scope.ALL: (),
}


@dataclass(frozen=True)
class Choice:
    thread: dict[str, Any]
    target: str
    threads: tuple[dict[str, Any], ...] = ()
    confirmed: bool = False
    action: SessionAction = "resume"


@dataclass(frozen=True)
class RefreshResult:
    reg: dict[str, Any] | None
    running: frozenset[tuple[str, str]] | None
    note: str | None
    error: str | None


def live_session_ids() -> frozenset[tuple[str, str]]:
    return PresenceClient().require_running_session_ids()


def empty_hint(scope: Scope, query: str) -> str:
    options = [*(["clear the filter"] if query else []), *_WIDEN_HINTS[scope]]
    if not options:
        return "nothing here — no sessions on this machine yet"
    return "nothing here — " + ", ".join(options)


def _preview(thread: dict[str, Any]) -> str:
    for field in ("lastPrompt", "summary"):
        value = thread.get(field)
        if isinstance(value, str):
            collapsed = " ".join(value.split())
            if collapsed:
                return collapsed
    return ""


def _age(mtime: object, now_iso: str) -> str:
    if not isinstance(mtime, str):
        return "never"
    try:
        return overview.age_human(mtime, now_iso)
    except ValueError:
        return "?"


def row_text(thread: dict[str, Any], now_iso: str, *, running: bool = False) -> Text:
    marks = resume.badges(thread, now_iso)
    if running:
        marks = [*marks, "running"]
    text = Text()
    text.append(f"{agent_name(resume.source_of(thread)):<9} ", style="bold")
    text.append(f"{_age(thread.get('mtime'), now_iso):<10}", style="dim")
    text.append(resume.label(thread))
    if marks:
        text.append("  [" + " ".join(marks) + "]", style="italic")
    preview = _preview(thread)
    if preview:
        text.append("  " + preview, style="dim")
    return text


async def ask_resume_choice(
    screen: Screen[Any],
    thread: dict[str, Any],
    *,
    target: str | None,
    running: bool,
    threads: tuple[dict[str, Any], ...],
) -> Choice | None:
    choice = (
        AgentChoice(target) if target else await screen.app.push_screen_wait(AgentModal(thread))
    )
    if choice is None:
        return None
    handoff = choice.target != resume.source_of(thread)
    confirmed = False
    if choice.action == "resume" and not handoff and running:
        message = Text(resume.label(thread))
        message.append(" is running right now — resume it anyway? [y/n]")
        confirmed = await screen.app.push_screen_wait(ConfirmModal(message))
        if not confirmed:
            return None
    return Choice(
        thread=thread,
        target=choice.target,
        action=choice.action,
        threads=threads,
        confirmed=confirmed,
    )


class PickerScreen(Screen[Choice | None]):
    """Pick a dormant session to resume, and the agent that resumes it. Dismisses with
    the choice; running it is the caller's job, once the terminal is back."""

    TITLE = "rl resume"

    DEFAULT_CSS = """
    PickerScreen #rows {
        height: 1fr;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    PickerScreen #empty {
        width: 100%;
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
        display: none;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        # the filter Input has focus and binds ctrl+a/ctrl+k itself, so these only
        # arrive as priority bindings
        *nav_bindings(priority=True),
        Binding("ctrl+a", "scope_all", "everywhere", priority=True),
        Binding("ctrl+r", "scope_recursive", "this tree", priority=True),
        Binding("enter", "choose", "resume", priority=True),
        Binding("escape", "leave", "quit"),
    ]

    def __init__(  # noqa: PLR0913 -- injection points the suite drives
        self,
        registry_path: str | Path,
        cwd: str | Path,
        *,
        scope: Scope = Scope.CWD,
        query: str = "",
        target: str | None = None,
        refresher: Refresher | None = None,
        presence: Presence | None = None,
        now: Clock | None = None,
        initial: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._initial = initial
        self.registry_path = str(registry_path)
        self.cwd = str(cwd)
        self._scope = scope
        self._query = query
        self._target = target
        self._refresher: Refresher = refresher or refresh.auto_refresh
        self._presence: Presence = presence or live_session_ids
        self._now: Clock = now or (lambda: datetime.now(UTC))
        self._reg: dict[str, Any] = registry.empty_registry()
        self._rows: list[dict[str, Any]] = []
        self._live_ids: frozenset[tuple[str, str]] = frozenset()
        self._scanned = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="filter…", id="filter")
        yield OptionList(id="rows", compact=True)
        yield Label("", id="empty")
        yield Footer()

    async def on_mount(self) -> None:
        # render from the registry as it stands on disk; a scan over a large store is
        # slow enough that waiting for one would cost the picker its instant open
        self._reg = (
            self._initial
            if self._initial is not None
            else registry.load_registry(self.registry_path)
        )
        filter_input = self.query_one("#filter", Input)
        filter_input.value = self._query
        filter_input.focus()
        await self._render_rows()
        self._converge()

    def _alive(self) -> bool:
        return self.is_attached and self.app.is_running

    @work
    async def _converge(self) -> None:
        results: SimpleQueue[RefreshResult] = SimpleQueue()
        stopped = Event()
        # The default executor joins scan threads at App.run() exit, delaying resume.
        Thread(target=self._refresh, args=(results, stopped), daemon=True).start()
        try:
            while self._alive():
                try:
                    result = results.get_nowait()
                except Empty:
                    await asyncio.sleep(0.02)
                    continue
                if result.error:
                    self.app.notify(result.error, title="rl resume", severity="warning")
                self._adopt(result.reg, result.running, result.note)
                return
        finally:
            stopped.set()

    def _refresh(self, results: SimpleQueue[RefreshResult], stopped: Event) -> None:
        reg: dict[str, Any] | None = None
        note: str | None = None
        warning: str | None = None
        try:
            reg, note = self._refresher(self.registry_path, self._now().isoformat())
        except Exception as error:  # noqa: BLE001 -- report in the UI, do not crash it
            warning = f"scan failed: {error}"
        if stopped.is_set():
            return
        running: frozenset[tuple[str, str]] | None = None
        try:
            running = self._presence()
        except Exception:  # noqa: BLE001 -- no presence means no live sessions
            running = None
        results.put(RefreshResult(reg, running, note, warning))

    def _adopt(
        self,
        reg: dict[str, Any] | None,
        running: frozenset[tuple[str, str]] | None,
        note: str | None,
    ) -> None:
        if not self._alive():
            return
        if reg is not None:
            self._reg = reg
            self._scanned = True
        if running is not None:
            self._live_ids = running
        if note:
            self.app.notify(note, title="rl resume")
        self.call_next(self._render_rows)

    # -- rendering ---------------------------------------------------------

    def _list_view(self) -> OptionList:
        return self.query_one("#rows", OptionList)

    def _highlighted_id(self) -> tuple[str, str] | None:
        index = self._list_view().highlighted
        if index is None or not (0 <= index < len(self._rows)):
            return None
        return registry.identity(self._rows[index])

    def _is_running(self, thread: dict[str, Any]) -> bool:
        session_id = thread.get("sessionId")
        if not session_id:
            return False
        return (agent_name(resume.source_of(thread)), session_id) in self._live_ids

    async def _render_rows(self) -> None:
        # the scan lands on a later tick, by which time the user may already have quit
        if not self._alive():
            return
        keep = self._highlighted_id()
        now = self._now().isoformat()
        in_scope = resume.candidates(self._reg, scope=self._scope, cwd=self.cwd)
        self._rows = resume.rank(in_scope, self._query)
        list_view = self._list_view()
        list_view.clear_options()
        list_view.add_options(row_text(t, now, running=self._is_running(t)) for t in self._rows)
        if self._rows:
            list_view.highlighted = next(
                (i for i, t in enumerate(self._rows) if registry.identity(t) == keep), 0
            )
        self.sub_title = f"[{self._scope.value}] ({len(self._rows)})"
        self._render_empty()

    def _render_empty(self) -> None:
        empty = self.query_one("#empty", Label)
        list_view = self._list_view()
        list_view.display = bool(self._rows)
        empty.display = not self._rows
        if not self._rows:
            empty.update(empty_hint(self._scope, self._query))

    # -- filtering ---------------------------------------------------------

    async def on_input_changed(self, event: Input.Changed) -> None:
        self._query = event.value
        await self._render_rows()

    # -- cursor movement ---------------------------------------------------

    def action_cursor_down(self) -> None:
        rows = self._list_view()
        if rows.option_count:
            rows.highlighted = 0 if rows.highlighted is None else rows.highlighted + 1

    def action_cursor_up(self) -> None:
        rows = self._list_view()
        if rows.option_count:
            rows.highlighted = (
                rows.option_count - 1 if rows.highlighted is None else rows.highlighted - 1
            )

    # -- scope -------------------------------------------------------------

    async def action_scope_all(self) -> None:
        await self._set_scope(Scope.ALL)

    async def action_scope_recursive(self) -> None:
        await self._set_scope(Scope.RECURSIVE)

    async def _set_scope(self, scope: Scope) -> None:
        self._scope = scope
        await self._render_rows()

    # -- choosing ----------------------------------------------------------

    def _current(self) -> dict[str, Any] | None:
        index = self._list_view().highlighted
        if index is None or not (0 <= index < len(self._rows)):
            return None
        return self._rows[index]

    @work(exclusive=True, group="choose")
    async def action_choose(self) -> None:
        thread = self._current()
        if thread is None:
            return
        threads = (
            tuple(cast(list[dict[str, Any]], self._reg.get("threads") or []))
            if self._scanned
            else ()
        )
        choice = await ask_resume_choice(
            self,
            thread,
            target=self._target,
            running=self._is_running(thread),
            threads=threads,
        )
        if choice is not None and self._alive():
            self.dismiss(choice)

    def action_leave(self) -> None:
        self.dismiss(None)


class PickerApp(App[Choice | None]):
    TITLE = "rl resume"

    def __init__(  # noqa: PLR0913 -- injection points the suite drives
        self,
        registry_path: str | Path,
        cwd: str | Path,
        *,
        scope: Scope = Scope.CWD,
        query: str = "",
        target: str | None = None,
        refresher: Refresher | None = None,
        presence: Presence | None = None,
        now: Clock | None = None,
    ) -> None:
        super().__init__()
        self._picker = PickerScreen(
            registry_path,
            cwd,
            scope=scope,
            query=query,
            target=target,
            refresher=refresher,
            presence=presence,
            now=now,
        )

    def on_mount(self) -> None:
        self.push_screen(self._picker, callback=self._finish)

    def _finish(self, choice: Choice | None) -> None:
        self.exit(choice)
