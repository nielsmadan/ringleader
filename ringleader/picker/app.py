from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Grid
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView

from .. import overview, refresh, registry, resume
from ..presence import PresenceClient
from ..resume import Scope, agent_name, launchable_agents
from ..tui.keys import nav_bindings

Refresher = Callable[[str, str], tuple[dict, str | None]]
Presence = Callable[[], frozenset[tuple[str, str]]]

_WIDEN_HINTS: dict[Scope, tuple[str, ...]] = {
    Scope.CWD: ("ctrl+r for this tree", "ctrl+a for everywhere"),
    Scope.RECURSIVE: ("ctrl+a for everywhere",),
    Scope.ALL: (),
}


@dataclass(frozen=True)
class Choice:
    thread: dict
    target: str
    threads: tuple[dict, ...] = ()
    confirmed: bool = False


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def live_session_ids() -> frozenset[tuple[str, str]]:
    return PresenceClient().require_running_session_ids()


def empty_hint(scope: Scope, query: str) -> str:
    options = [*(["clear the filter"] if query else []), *_WIDEN_HINTS[scope]]
    if not options:
        return "nothing here — no sessions on this machine yet"
    return "nothing here — " + ", ".join(options)


def _preview(thread: dict) -> str:
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


def row_text(thread: dict, now_iso: str, *, running: bool = False) -> Text:
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


def agent_row_text(agent: str, *, original: bool) -> Text:
    suffix = "(original)" if original else "(hand off)"
    return Text(f"{agent_name(agent):<9} {suffix}")


class AgentModal(ModalScreen[str | None]):
    """Which agent resumes this thread — its own first, the rest as a handoff."""

    BINDINGS: ClassVar[list[BindingType]] = [
        *nav_bindings(),
        Binding("escape", "cancel", "cancel", show=False),
    ]

    DEFAULT_CSS = """
    AgentModal {
        align: center middle;
    }
    #agent-box {
        grid-size: 1;
        grid-gutter: 1;
        padding: 1 2;
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
    }
    """

    def __init__(self, thread: dict) -> None:
        super().__init__()
        self._source = resume.source_of(thread)
        agents = launchable_agents()
        own = (self._source,) if self._source in agents else ()
        self._agents = [*own, *(a for a in agents if a != self._source)]

    def compose(self) -> ComposeResult:
        yield Grid(
            Label("resume with which agent?"),
            ListView(
                *(
                    ListItem(Label(agent_row_text(agent, original=agent == self._source)))
                    for agent in self._agents
                ),
                id="agents",
            ),
            id="agent-box",
        )

    def on_mount(self) -> None:
        agents = self.query_one("#agents", ListView)
        agents.index = 0
        agents.focus()

    def action_cursor_down(self) -> None:
        self.query_one("#agents", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#agents", ListView).action_cursor_up()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is None or not (0 <= index < len(self._agents)):
            return
        self.dismiss(self._agents[index])

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "confirm", "yes", show=False),
        Binding("n", "cancel", "no", show=False),
        Binding("escape", "cancel", "cancel", show=False),
    ]

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    #confirm-box {
        grid-size: 1;
        grid-gutter: 1;
        padding: 1 2;
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
    }
    """

    def __init__(self, message: Text) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Grid(Label(self._message, id="confirm-message"), id="confirm-box")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class PickerApp(App[Choice | None]):
    """Pick a dormant session to resume, and the agent that resumes it. Returns the
    choice; running it is the caller's job, once the terminal is back."""

    TITLE = "rl resume"

    _GATED_ACTIONS: ClassVar[tuple[str, ...]] = (
        "cursor_down",
        "cursor_up",
        "scope_all",
        "scope_recursive",
        "choose",
    )

    DEFAULT_CSS = """
    PickerApp #rows {
        height: 1fr;
    }
    PickerApp #rows Label {
        width: 100%;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    PickerApp #empty {
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
        Binding("escape", "quit", "quit"),
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
    ) -> None:
        super().__init__()
        self.registry_path = str(registry_path)
        self.cwd = str(cwd)
        self._scope = scope
        self._query = query
        self._target = target
        self._refresher: Refresher = refresher or refresh.auto_refresh
        self._presence: Presence = presence or live_session_ids
        self._reg: dict = registry.empty_registry()
        self._rows: list[dict] = []
        self._live_ids: frozenset[tuple[str, str]] = frozenset()
        self._scanned = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="filter…", id="filter")
        yield ListView(id="rows")
        yield Label("", id="empty")
        yield Footer()

    async def on_mount(self) -> None:
        # render from the registry as it stands on disk; a scan over a large store is
        # slow enough that waiting for one would cost the picker its instant open
        self._reg = registry.load_registry(self.registry_path)
        filter_input = self.query_one("#filter", Input)
        filter_input.value = self._query
        filter_input.focus()
        await self._render()
        self._converge()

    def _alive(self) -> bool:
        return self.is_running and not self._exit

    @work(thread=True)
    def _converge(self) -> None:
        reg: dict | None = None
        note: str | None = None
        try:
            reg, note = self._refresher(self.registry_path, _now_iso())
        except Exception as error:
            if not self._alive():
                return
            self.call_from_thread(
                self.notify, f"scan failed: {error}", title="rl resume", severity="warning"
            )
        running: frozenset[tuple[str, str]] | None = None
        try:
            running = self._presence()
        except Exception:
            running = None
        if not self._alive():
            return
        self.call_from_thread(self._adopt, reg, running, note)

    def _adopt(
        self,
        reg: dict | None,
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
            self.notify(note, title="rl resume")
        self.call_next(self._render)

    # -- rendering ---------------------------------------------------------

    def _list_view(self) -> ListView:
        return self.query_one("#rows", ListView)

    def _highlighted_id(self) -> str | None:
        index = self._list_view().index
        if index is None or not (0 <= index < len(self._rows)):
            return None
        return self._rows[index].get("sessionId")

    def _is_running(self, thread: dict) -> bool:
        session_id = thread.get("sessionId")
        if not session_id:
            return False
        return (agent_name(resume.source_of(thread)), session_id) in self._live_ids

    async def _render(self) -> None:
        # the scan lands on a later tick, by which time the user may already have quit
        if not self._alive():
            return
        keep = self._highlighted_id()
        now = _now_iso()
        in_scope = resume.candidates(self._reg, scope=self._scope, cwd=self.cwd)
        self._rows = resume.rank(in_scope, self._query)
        list_view = self._list_view()
        await list_view.clear()
        await list_view.extend(
            ListItem(Label(row_text(t, now, running=self._is_running(t)))) for t in self._rows
        )
        if self._rows:
            list_view.index = next(
                (i for i, t in enumerate(self._rows) if t.get("sessionId") == keep), 0
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
        await self._render()

    # -- cursor movement ---------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # priority bindings reach run_action even with a modal on the screen stack;
        # gate them here so the modal keeps its own keys
        return not (action in self._GATED_ACTIONS and len(self.screen_stack) > 1)

    def action_cursor_down(self) -> None:
        self._list_view().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._list_view().action_cursor_up()

    # -- scope -------------------------------------------------------------

    async def action_scope_all(self) -> None:
        await self._set_scope(Scope.ALL)

    async def action_scope_recursive(self) -> None:
        await self._set_scope(Scope.RECURSIVE)

    async def _set_scope(self, scope: Scope) -> None:
        self._scope = scope
        await self._render()

    # -- choosing ----------------------------------------------------------

    def _current(self) -> dict | None:
        index = self._list_view().index
        if index is None or not (0 <= index < len(self._rows)):
            return None
        return self._rows[index]

    @work(exclusive=True, group="choose")
    async def action_choose(self) -> None:
        thread = self._current()
        if thread is None:
            return
        target = self._target or await self.push_screen_wait(AgentModal(thread))
        if target is None:
            return
        handoff = target != resume.source_of(thread)
        confirmed = False
        if not handoff and self._is_running(thread):
            message = Text(resume.label(thread))
            message.append(" is running right now — resume it anyway? [y/n]")
            confirmed = await self.push_screen_wait(ConfirmModal(message))
            if not confirmed:
                return
        self.exit(
            Choice(
                thread=thread,
                target=target,
                threads=tuple(self._reg.get("threads") or []) if self._scanned else (),
                confirmed=confirmed,
            )
        )
