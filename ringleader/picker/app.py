from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Thread
from typing import Any, ClassVar, Literal, cast

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Footer, Header, Input, Label, OptionList
from textual.widgets.option_list import Option

from .. import contract, overview, refresh, registry, resume, scanner
from ..agents import source_of
from ..presence import SETUP_COMMAND, PresenceClient
from ..resume import Haystack, Placement, Scope, SessionAction, agent_name, launchable_agents
from ..sources.base import WorkspaceRelocatingSource
from ..tui.keys import nav_bindings
from ..tui.modals import (
    AgentChoice,
    AgentModal,
    ChoiceModal,
    ChoiceOption,
    ConfirmModal,
    ForkNameModal,
    agent_row_text,
)
from ..tui.types import Clock, Presence, Refresher

__all__ = [
    "AgentChoice",
    "AgentModal",
    "Choice",
    "ConfirmModal",
    "PickerApp",
    "PickerScreen",
    "Where",
    "agent_row_text",
    "ask_resume_choice",
    "empty_hint",
    "launchable_agents",
    "live_session_ids",
    "location_options",
    "row_text",
]

FILTER_DELAY = 0.12

type FullRefresher = Callable[[str, str], refresh.Refreshed]
type WorkspaceRefresher = Callable[[str, str, str, bool], refresh.Refreshed]

_WIDEN_HINTS: dict[Scope, tuple[str, ...]] = {
    Scope.CWD: ("ctrl+t for this tree", "ctrl+a for everywhere"),
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
    name: str | None = None
    placement: Placement = "there"


type Where = Literal["there", "follow", "move-here", "fork-here"]

SHELL_SETUP = "needs rl shell init"
SHELL_SETUP_HOW = (
    'cd there needs the rl shell function — add eval "$(rl shell init zsh)" to ~/.zshrc '
    "(or bash to ~/.bashrc) and open a new shell"
)
PRESENCE_UNKNOWN = "can't tell yet whether it is running"
PRESENCE_UNKNOWN_HOW = (
    "move it here needs to know the session is not running — wait for the refresh to "
    f"finish, or set up live sessions with {SETUP_COMMAND}"
)


@dataclass(frozen=True)
class RefreshResult:
    reg: dict[str, Any] | None
    running: frozenset[tuple[str, str]] | None
    note: str | None
    error: str | None
    unavailable_sources: frozenset[str] = frozenset()
    stage: Literal["workspace", "full", "presence"] = "full"


@dataclass
class _RowCache:
    haystacks: dict[tuple[str, str], Haystack] = dataclass_field(default_factory=dict)
    options: dict[tuple[tuple[str, str], bool], Option] = dataclass_field(default_factory=dict)
    minute: str | None = None
    shown: tuple[object, ...] | None = None

    def advance(
        self, minute: str, live_ids: frozenset[tuple[str, str]], rows: list[dict[str, Any]]
    ) -> bool:
        shown = (minute, live_ids, *(registry.identity(t) for t in rows))
        if shown == self.shown:
            return False
        self.shown = shown
        # a row's age reads in minutes at its finest, so a row built this minute still holds
        if minute != self.minute:
            self.options, self.minute = {}, minute
        return True


PREVIEW_CHARS = 500


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
                return collapsed[:PREVIEW_CHARS]
    return ""


def _age(mtime: object, now_iso: str) -> str:
    if not isinstance(mtime, str):
        return "never"
    try:
        return overview.age_human(mtime, now_iso)
    except ValueError:
        return "?"


def row_text(
    thread: dict[str, Any],
    now_iso: str,
    *,
    running: bool = False,
    sources: Mapping[str, WorkspaceRelocatingSource] | None = None,
) -> Text:
    marks = resume.badges(thread, now_iso, sources)
    if running:
        marks = [*marks, "running"]
    text = Text()
    text.append(f"{agent_name(source_of(thread)):<9} ", style="bold")
    text.append(f"{_age(thread.get('mtime'), now_iso):<12}", style="dim")
    started_at = thread.get("startedAt")
    started = _age(started_at, now_iso) if isinstance(started_at, str) else "?"
    text.append(f"{started:<12}", style="dim")
    text.append(resume.label(thread))
    if marks:
        text.append("  [" + " ".join(marks) + "]", style="italic")
    preview = _preview(thread)
    if preview:
        text.append("  " + preview, style="dim")
    return text


def location_options(
    thread: dict[str, Any], *, running: bool | None, shell_wrapped: bool
) -> list[ChoiceOption[Where]]:
    source = source_of(thread)
    launchable = source in launchable_agents()
    movable = launchable and running is not True and resume.moves_sessions(source)
    forkable = launchable and resume.forks_across_directories(source)
    return [
        ChoiceOption[Where]("open it where it is", "there"),
        ChoiceOption[Where](
            "cd there, then open it", "follow", None if shell_wrapped else SHELL_SETUP
        ),
        *(
            [
                ChoiceOption[Where](
                    "move it here", "move-here", PRESENCE_UNKNOWN if running is None else None
                )
            ]
            if movable
            else []
        ),
        *(
            [ChoiceOption[Where]("fork it here", "fork-here", "a converted copy")]
            if forkable
            else []
        ),
    ]


def _location_title(thread: dict[str, Any], launch: str, cwd: str) -> Text:
    title = Text(resume.label(thread), style="bold")
    title.append(f" lives in {launch}\nyou are in {cwd}")
    return title


async def _ask_where(
    screen: Screen[Any],
    thread: dict[str, Any],
    launch: str,
    cwd: str,
    *,
    running: bool | None,
    shell_wrapped: bool,
) -> Where | None:
    options = location_options(thread, running=running, shell_wrapped=shell_wrapped)
    where = await screen.app.push_screen_wait(
        ChoiceModal(_location_title(thread, launch, cwd), options)
    )
    if where == "follow" and not shell_wrapped:
        screen.app.notify(SHELL_SETUP_HOW, title="rl resume", severity="warning", markup=False)
        return None
    if where == "move-here" and running is None:
        screen.app.notify(PRESENCE_UNKNOWN_HOW, title="rl resume", severity="warning", markup=False)
        return None
    return where


async def _confirm_running(screen: Screen[Any], thread: dict[str, Any], question: str) -> bool:
    message = Text(resume.label(thread))
    message.append(f" is running right now — {question} [y/n]")
    confirmed: bool = await screen.app.push_screen_wait(ConfirmModal(message))
    return confirmed


async def _fork_name(
    screen: Screen[Any], thread: dict[str, Any], pool: Sequence[dict[str, Any]]
) -> str | None:
    suggested = resume.suggested_fork_name(thread, pool)
    name: str | None = await screen.app.push_screen_wait(ForkNameModal(suggested))
    return name


async def _fork_here(
    screen: Screen[Any],
    thread: dict[str, Any],
    *,
    running: bool | None,
    threads: tuple[dict[str, Any], ...],
    pool: Sequence[dict[str, Any]],
) -> Choice | None:
    confirmed = False
    if running:
        confirmed = await _confirm_running(screen, thread, "fork its current state here anyway?")
        if not confirmed:
            return None
    # a converted fork takes a title in every agent, so it is always named
    name = await _fork_name(screen, thread, pool)
    if name is None:
        return None
    return Choice(
        thread,
        source_of(thread),
        threads,
        confirmed=confirmed,
        action="fork",
        name=name or None,
        placement="here",
    )


async def _ask_agent(  # noqa: PLR0913 -- the flow ask_resume_choice hands on
    screen: Screen[Any],
    thread: dict[str, Any],
    *,
    target: str | None,
    action: SessionAction | None,
    running: bool | None,
    threads: tuple[dict[str, Any], ...],
    pool: Sequence[dict[str, Any]],
    placement: Literal["there", "follow"],
) -> Choice | None:
    choice = (
        AgentChoice(source_of(thread), action)
        if action is not None
        else AgentChoice(target)
        if target
        else await screen.app.push_screen_wait(AgentModal(thread))
    )
    if choice is None:
        return None
    handoff = choice.target != source_of(thread)
    confirmed = False
    if choice.action == "resume" and not handoff and running:
        confirmed = await _confirm_running(screen, thread, "resume it anyway?")
        if not confirmed:
            return None
    name = None
    if choice.action == "fork" and resume.names_forks(choice.target):
        name = await _fork_name(screen, thread, pool)
        if name is None:
            return None
    return Choice(
        thread=thread,
        target=choice.target,
        action=choice.action,
        threads=threads,
        confirmed=confirmed,
        name=name or None,
        placement=placement,
    )


async def ask_resume_choice(  # noqa: PLR0913 -- known is the picker's registry before a scan
    screen: Screen[Any],
    thread: dict[str, Any],
    *,
    target: str | None,
    running: bool | None,
    threads: tuple[dict[str, Any], ...],
    cwd: str,
    known: Sequence[dict[str, Any]] = (),
    action: SessionAction | None = None,
    shell_wrapped: bool = False,
) -> Choice | None:
    pool = known or threads
    where: Where = "there"
    launch = resume.launch_cwd(thread, pool) if action is None and target is None else None
    if launch is not None and resume.launched_elsewhere(launch, cwd):
        asked = await _ask_where(
            screen, thread, launch, cwd, running=running, shell_wrapped=shell_wrapped
        )
        if asked is None:
            return None
        where = asked
    if where == "move-here":
        return Choice(thread, source_of(thread), threads, action="resume", placement="here")
    if where == "fork-here":
        return await _fork_here(screen, thread, running=running, threads=threads, pool=pool)
    return await _ask_agent(
        screen,
        thread,
        target=target,
        action=action,
        running=running,
        threads=threads,
        pool=pool,
        placement=where,
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
        Binding("ctrl+t", "scope_recursive", "this tree", priority=True),
        Binding("ctrl+r,f5", "refresh", "refresh", priority=True),
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
        action: SessionAction | None = None,
        persist: bool = True,
        refresher: Refresher | None = None,
        full_refresher: FullRefresher | None = None,
        workspace_refresher: WorkspaceRefresher | None = None,
        presence: Presence | None = None,
        now: Clock | None = None,
        initial: dict[str, Any] | None = None,
        filter_delay: float = FILTER_DELAY,
        shell_wrapped: bool = False,
    ) -> None:
        super().__init__()
        self._shell_wrapped = shell_wrapped
        self._initial = initial
        self._filter_delay = filter_delay
        self._filter_timer: Timer | None = None
        self._cache = _RowCache()
        self.registry_path = str(registry_path)
        self.cwd = str(cwd)
        self._scope = scope
        self._query = query
        self._target = target
        self._requested_action = action
        self._refresher = refresher
        self._persist = persist
        self._pool = scanner.SourcePool()
        self._scan_baseline: dict[str, Any] | None = None
        self._full_refresher = full_refresher or self._full_refresh
        self._workspace_refresher = workspace_refresher
        if refresher is None and full_refresher is None:
            self._workspace_refresher = workspace_refresher or self._workspace_refresh
        self._refreshing = False
        self._refresh_queued = False
        self._refresh_status = "Refreshing sessions… showing cached results"
        self._presence: Presence = presence or live_session_ids
        self._now: Clock = now or (lambda: datetime.now(UTC))
        self._reg: dict[str, Any] = registry.empty_registry()
        self._rows: list[dict[str, Any]] = []
        self._live_ids: frozenset[tuple[str, str]] | None = None
        self._scanned = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="filter…", id="filter")
        yield Label("Refreshing sessions… showing cached results", id="refresh-status")
        yield Label(f"{'Agent':<10}{'Last active':<12}{'Started':<12}Session", id="columns")
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
        self._start_refresh()

    def _full_refresh(self, path: str, now_iso: str) -> refresh.Refreshed:
        return refresh.refresh_registry(
            path,
            now_iso,
            pool=self._pool,
            persist=self._persist,
            initial=None if self._persist else self._scan_baseline,
        )

    def _workspace_refresh(
        self, path: str, now_iso: str, cwd: str, recursive: bool
    ) -> refresh.Refreshed:
        return refresh.workspace_refresh(
            path,
            now_iso,
            cwd,
            recursive,
            pool=self._pool,
            persist=self._persist,
            initial=None if self._persist else self._scan_baseline,
        )

    def _alive(self) -> bool:
        return self.is_attached and self.app.is_running

    def action_refresh(self) -> None:
        self._start_refresh()

    def _start_refresh(self) -> None:
        if self._refreshing:
            self._refresh_queued = True
            self._show_refresh_status()
            return
        self._refreshing = True
        self._converge()

    def _show_refresh_status(self) -> None:
        suffix = " · refresh queued" if self._refresh_queued else ""
        self.query_one("#refresh-status", Label).update(self._refresh_status + suffix)

    @work
    async def _converge(self) -> None:
        try:
            while self._alive():
                self._refresh_status = "Refreshing sessions… showing cached results"
                self._show_refresh_status()
                await self._receive_refresh()
                if not self._refresh_queued:
                    return
                self._refresh_queued = False
        finally:
            self._refreshing = False

    async def _receive_refresh(self) -> None:
        results: SimpleQueue[RefreshResult] = SimpleQueue()
        stopped = Event()
        # The default executor joins scan threads at App.run() exit, delaying resume.
        # Presence runs beside the scan, so a move need not wait for a slow full scan.
        for target, args in (
            (self._refresh, (results, stopped, self._scope is Scope.RECURSIVE)),
            (self._probe_presence, (results, stopped)),
        ):
            Thread(target=target, args=args, daemon=True).start()
        pending = {"full", "presence"}
        try:
            while pending and self._alive():
                try:
                    result = results.get_nowait()
                except Empty:
                    await asyncio.sleep(0.02)
                    continue
                pending.discard(result.stage)
                if result.stage == "presence":
                    self._adopt_presence(result.running)
                    continue
                self._refresh_status = self._result_status(result)
                self._show_refresh_status()
                if result.error:
                    self.app.notify(
                        result.error, title="rl resume", severity="warning", markup=False
                    )
                self._adopt(result.reg, result.note)
        finally:
            stopped.set()

    def _result_status(self, result: RefreshResult) -> str:
        if result.stage == "workspace":
            status = "Workspace refresh failed" if result.error else "Workspace results ready"
            return status + " — refreshing all sessions…"
        if result.error:
            return "Refresh failed — keeping displayed results; ctrl+r or F5 to retry"
        if result.unavailable_sources:
            return "Partially refreshed — cached sources: " + ", ".join(
                sorted(result.unavailable_sources)
            )
        return "Sessions refreshed"

    def _refresh(
        self, results: SimpleQueue[RefreshResult], stopped: Event, recursive: bool
    ) -> None:
        if self._workspace_refresher is not None:
            try:
                local = self._workspace_refresher(
                    self.registry_path, self._now().isoformat(), self.cwd, recursive
                )
                self._scan_baseline = local.reg
                results.put(
                    RefreshResult(
                        local.reg, None, local.note, None, local.unavailable_sources, "workspace"
                    )
                )
            except Exception as error:  # noqa: BLE001 -- the full scan can still recover
                results.put(RefreshResult(None, None, None, str(error), stage="workspace"))
        if stopped.is_set():
            return
        reg: dict[str, Any] | None = None
        note: str | None = None
        warning: str | None = None
        unavailable_sources: frozenset[str] = frozenset()
        try:
            if self._refresher is not None:
                reg, note = self._refresher(self.registry_path, self._now().isoformat())
            else:
                refreshed = self._full_refresher(self.registry_path, self._now().isoformat())
                reg, note = refreshed.reg, refreshed.note
                unavailable_sources = refreshed.unavailable_sources
        except Exception as error:  # noqa: BLE001 -- report in the UI, do not crash it
            warning = f"scan failed: {error}"
        if stopped.is_set():
            return
        if reg is not None:
            self._scan_baseline = reg
        results.put(RefreshResult(reg, None, note, warning, unavailable_sources))

    def _probe_presence(self, results: SimpleQueue[RefreshResult], stopped: Event) -> None:
        if stopped.is_set():
            return
        running: frozenset[tuple[str, str]] | None
        try:
            running = self._presence()
        except Exception:  # noqa: BLE001 -- no badges; a move still refuses on unknown
            running = None
        results.put(RefreshResult(None, running, None, None, stage="presence"))

    def _adopt_presence(self, running: frozenset[tuple[str, str]] | None) -> None:
        if not self._alive():
            return
        self._live_ids = running
        self.call_next(self._render_rows)

    def _adopt(self, reg: dict[str, Any] | None, note: str | None) -> None:
        if not self._alive():
            return
        if reg is not None:
            if reg.get("threads") != self._reg.get("threads"):
                self._cache = _RowCache()
            self._reg = reg
            self._scanned = True
        if note:
            self.app.notify(note, title="rl resume", markup=False)
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
        return bool(contract.is_running(thread, self._live_ids))

    def _haystack(self, thread: dict[str, Any]) -> Haystack:
        key = registry.identity(thread)
        found = self._cache.haystacks.get(key)
        if found is None:
            found = self._cache.haystacks[key] = resume.haystack_of(thread)
        return found

    def _option(
        self,
        thread: dict[str, Any],
        now_iso: str,
        sources: Mapping[str, WorkspaceRelocatingSource],
    ) -> Option:
        running = self._is_running(thread)
        key = (registry.identity(thread), running)
        found = self._cache.options.get(key)
        if found is None:
            text = row_text(thread, now_iso, running=running, sources=sources)
            found = self._cache.options[key] = Option(text)
        return found

    async def _render_rows(self) -> None:
        # the scan lands on a later tick, by which time the user may already have quit
        if not self._alive():
            return
        keep = self._highlighted_id()
        in_scope = resume.candidates(self._reg, scope=self._scope, cwd=self.cwd)
        self._rows = resume.rank(in_scope, self._query, self._haystack)
        self.sub_title = f"[{self._scope.value}] ({len(self._rows)})"
        self._render_empty()
        now = self._now().isoformat()
        if not self._cache.advance(now[:16], self._live_ids or frozenset(), self._rows):
            return
        sources = resume.slug_sources()
        list_view = self._list_view()
        list_view.clear_options()
        list_view.add_options(self._option(t, now, sources) for t in self._rows)
        if self._rows:
            list_view.highlighted = next(
                (i for i, t in enumerate(self._rows) if registry.identity(t) == keep), 0
            )

    def _render_empty(self) -> None:
        empty = self.query_one("#empty", Label)
        list_view = self._list_view()
        list_view.display = bool(self._rows)
        empty.display = not self._rows
        if not self._rows:
            empty.update(empty_hint(self._scope, self._query))

    # -- filtering ---------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        self._cancel_filter()
        self._filter_timer = self.set_timer(self._filter_delay, self._filter_due)

    def _filter_due(self) -> None:
        self.call_later(self._apply_filter)

    def _cancel_filter(self) -> None:
        if self._filter_timer is not None:
            self._filter_timer.stop()
            self._filter_timer = None

    async def _apply_filter(self) -> None:
        self._cancel_filter()
        self._query = self.query_one("#filter", Input).value
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
        await self._apply_filter()

    # -- choosing ----------------------------------------------------------

    def _current(self) -> dict[str, Any] | None:
        index = self._list_view().highlighted
        if index is None or not (0 <= index < len(self._rows)):
            return None
        return self._rows[index]

    @work(exclusive=True, group="choose")
    async def action_choose(self) -> None:
        await self._apply_filter()
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
            running=contract.is_running(thread, self._live_ids),
            threads=threads,
            cwd=self.cwd,
            known=cast(list[dict[str, Any]], self._reg.get("threads") or []),
            action=self._requested_action,
            shell_wrapped=self._shell_wrapped,
        )
        if choice is not None and self._alive():
            self.dismiss(choice)

    def action_leave(self) -> None:
        self.dismiss(None)


class PickerApp(App[Choice | None]):
    TITLE = "rl resume"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "app.simulate_key('escape')", show=False, priority=True),
    ]

    def __init__(  # noqa: PLR0913 -- injection points the suite drives
        self,
        registry_path: str | Path,
        cwd: str | Path,
        *,
        scope: Scope = Scope.CWD,
        query: str = "",
        target: str | None = None,
        action: SessionAction | None = None,
        persist: bool = True,
        refresher: Refresher | None = None,
        full_refresher: FullRefresher | None = None,
        workspace_refresher: WorkspaceRefresher | None = None,
        presence: Presence | None = None,
        now: Clock | None = None,
        filter_delay: float = FILTER_DELAY,
        shell_wrapped: bool = False,
    ) -> None:
        super().__init__()
        self._picker = PickerScreen(
            registry_path,
            cwd,
            scope=scope,
            query=query,
            target=target,
            action=action,
            persist=persist,
            refresher=refresher,
            full_refresher=full_refresher,
            workspace_refresher=workspace_refresher,
            presence=presence,
            now=now,
            filter_delay=filter_delay,
            shell_wrapped=shell_wrapped,
        )

    def on_mount(self) -> None:
        self.push_screen(self._picker, callback=self._finish)

    def _finish(self, choice: Choice | None) -> None:
        self.exit(choice)
