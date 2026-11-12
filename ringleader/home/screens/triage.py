from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Footer, Header, OptionList, Static

from ... import overview, registry, resume
from ...agents import source_of
from ...decisions import Decision
from ...tui.keys import nav_bindings
from ...tui.modals import ConfirmModal, SnoozeModal
from ...tui.rows import highlighted_key, set_rows
from ..base import InboxScreen
from ..keymap import EnterTarget, Keymap, hint, with_bindings
from ..policy import CONFIRM_BULK, CONFIRM_EACH_DELETE
from ..state import (
    ProjectRef,
    ProjectScope,
    ProjectsView,
    RenderBasis,
    TriageScope,
    ViewEntry,
    next_entry,
    project_label,
    scope_rows,
    scope_title,
    view_for,
)
from .detail import DetailScreen

STATUS_WIDTH = 13
AGENT_WIDTH = 9
AGE_WIDTH = 9

_STATUS_STYLES = {
    "unclassified": "dim",
    "unfinished": "yellow",
    "blocked": "red",
    "finished": "green",
    "abandoned": "dim italic",
}


def triage_row(thread: dict[str, Any], now_iso: str, *, marked: bool, running: bool) -> Text:
    status = overview.effective_status(thread, now_iso)
    text = Text("● " if marked else "  ", style="bold")
    text.append(f"{status:<{STATUS_WIDTH}}", style=_STATUS_STYLES.get(status, "italic"))
    text.append(f"{resume.agent_name(source_of(thread)):<{AGENT_WIDTH}}", style="bold")
    text.append(f"{overview.age_human(thread.get('mtime'), now_iso):<{AGE_WIDTH}}", style="dim")
    text.append(resume.label(thread))
    what = thread.get("whatItIs")
    if what:
        text.append(f"  {what}", style="dim")
    if running:
        text.append("  [running]", style="bold")
    return text


def subtitle(title: str, waiting: int, decided: int, harvesting: tuple[str, ...]) -> str:
    parts = [title, f"{waiting} waiting", f"{decided} decided this session"]
    return " · ".join([*parts, *(f"harvesting {label}…" for label in harvesting)])


def thread_project(thread: dict[str, Any]) -> ProjectRef:
    mtime = thread.get("mtime") or ""
    return ProjectRef(
        key=overview.group_key(thread),
        label=project_label(thread),
        pending=1,
        unclassified=int(thread.get("status", "unclassified") == "unclassified"),
        oldest_mtime=mtime,
        newest_mtime=mtime,
    )


def done_text(title: str, following: ViewEntry | None, view: ProjectsView) -> Text:
    text = Text(f"{title} done.\n", style="bold")
    if following is None and view is ProjectsView.TREE:
        text.append("Nothing next in the tree — escape for the tree")
    elif following is None:
        text.append("All caught up — escape for Home")
    else:
        text.append(f"Next: Triage {following.label} ({following.pending} waiting) — enter")
    return text


_HANDOFF = Binding("t", "handoff", "triage the project with an agent", show=False)


class TriageScreen(InboxScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        *nav_bindings(),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("q", "app.quit", "quit", show=False),
        Binding("ctrl+r", "app.rescan", "rescan", show=False),
        _HANDOFF,
    ]

    DEFAULT_CSS = """
    TriageScreen #subtitle, TriageScreen #hint, TriageScreen #done {
        padding: 0 1;
    }
    TriageScreen #hint {
        color: $text-muted;
    }
    TriageScreen #done {
        margin: 1 0;
        display: none;
    }
    TriageScreen #rows {
        height: 1fr;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    def __init__(self, scope: TriageScope, keymap: Keymap) -> None:
        super().__init__()
        self.scope = scope
        self.keymap = keymap
        self.view = view_for(scope)
        self._bindings = with_bindings(
            self._bindings,
            (*keymap.triage, Binding(keymap.undo, "app.undo", "undo", show=False)),
        )
        self._rows: list[dict[str, Any]] = []
        self._marks: frozenset[str] = frozenset()
        self._following: ViewEntry | None = None
        self._stale = False
        self._now_iso = ""
        self._basis: RenderBasis | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="subtitle")
        yield Static(Text(hint((*self.keymap.triage, _HANDOFF), self.keymap.undo)), id="hint")
        yield OptionList(id="rows", compact=True)
        yield Static("", id="done")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#rows", OptionList).focus()
        self.sync()

    @property
    def marks(self) -> frozenset[str]:
        return self._marks

    def render_data(self) -> None:
        if not self.query("#rows"):
            return
        inbox = self.inbox
        basis = inbox.render_basis()
        if basis != self._basis:
            self._basis = basis
            self._now_iso = basis.now.isoformat()
            self._render_rows(scope_rows(self.scope, inbox.view_threads(), self._now_iso))
        self._render_subtitle()

    def _row_text(self, thread: dict[str, Any]) -> Text:
        return triage_row(
            thread,
            self._now_iso,
            marked=registry.qualified_id(thread) in self._marks,
            running=self.inbox.session_running(thread),
        )

    def _render_rows(self, threads: list[dict[str, Any]]) -> None:
        self._rows = threads
        ids = {registry.qualified_id(t) for t in threads}
        rows = self.query_one("#rows", OptionList)
        shown = highlighted_key(rows)
        if (self._marks - ids) or (shown is not None and shown not in ids):
            self._stale = True
        self._marks = self._marks & ids
        set_rows(
            rows,
            [(registry.qualified_id(t), self._row_text(t)) for t in threads],
            keep=shown,
        )
        empty = not threads
        self._following = (
            next_entry(self.scope, self.inbox.view_entries(self.view)) if empty else None
        )
        done = self.query_one("#done", Static)
        title = scope_title(self.scope, self.inbox.home_dir)
        done.update(done_text(title, self._following, self.view) if empty else Text(""))
        done.display = empty
        was_hidden = not rows.display
        rows.display = not empty
        if not empty and was_hidden:
            rows.focus()

    def _render_subtitle(self) -> None:
        inbox = self.inbox
        self.query_one("#subtitle", Static).update(
            Text(
                subtitle(
                    scope_title(self.scope, inbox.home_dir),
                    len(self._rows),
                    inbox.decided_count,
                    inbox.harvesting,
                )
            )
        )

    def _set_marks(self, marks: frozenset[str]) -> None:
        flipped = self._marks ^ marks
        self._marks = marks
        rows = self.query_one("#rows", OptionList)
        for index, thread in enumerate(self._rows):
            if registry.qualified_id(thread) in flipped:
                rows.replace_option_prompt_at_index(index, self._row_text(thread))

    def _highlighted(self) -> dict[str, Any] | None:
        index = self.query_one("#rows", OptionList).highlighted
        if index is None or not (0 <= index < len(self._rows)):
            return None
        return self._rows[index]

    def _targets(self) -> list[dict[str, Any]]:
        if self._stale:
            self._stale = False
            self.notify("the list changed under you — press the key again", severity="warning")
            return []
        marked = [t for t in self._rows if registry.qualified_id(t) in self._marks]
        if marked:
            return marked
        highlighted = self._highlighted()
        return [highlighted] if highlighted is not None else []

    def _decide(
        self, decision: Decision, targets: list[dict[str, Any]], until: str | None = None
    ) -> None:
        if self.inbox.decide(decision, targets, until=until):
            self._set_marks(frozenset())
            self.sync()
        self._stale = False

    def _confirm_then(self, decision: Decision, targets: list[dict[str, Any]], prompt: str) -> None:
        def answered(confirmed: bool | None) -> None:
            if confirmed:
                self._decide(decision, targets)

        self.app.push_screen(ConfirmModal(Text(prompt)), callback=answered)

    def action_cursor_down(self) -> None:
        self._stale = False
        self.query_one("#rows", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self._stale = False
        self.query_one("#rows", OptionList).action_cursor_up()

    def action_archive(self) -> None:
        targets = self._targets()
        if len(targets) > 1 and "archive" in CONFIRM_BULK:
            self._confirm_then("archive", targets, f"Archive {len(targets)} sessions? [y/n]")
        elif targets:
            self._decide("archive", targets)

    def action_delete(self) -> None:
        targets = self._targets()
        if len(targets) > 1 and "delete" in CONFIRM_BULK:
            prompt = f"Delete {len(targets)} sessions permanently? Nothing is harvested. [y/n]"
            self._confirm_then("delete", targets, prompt)
        elif len(targets) == 1 and CONFIRM_EACH_DELETE:
            prompt = f"Delete {resume.label(targets[0])} permanently? Nothing is harvested. [y/n]"
            self._confirm_then("delete", targets, prompt)
        elif targets:
            self._decide("delete", targets)

    def action_keep_going(self) -> None:
        if targets := self._targets():
            self._decide("keep_going", targets)

    def action_keep(self) -> None:
        if targets := self._targets():
            self._decide("keep", targets)

    def action_snooze(self) -> None:
        targets = self._targets()
        if not targets:
            return

        def chosen(until: str | None) -> None:
            if until is not None:
                self._decide("snooze", targets, until)

        self.app.push_screen(SnoozeModal(self.inbox.clock), callback=chosen)

    def action_harvest(self) -> None:
        targets = self._targets()
        if len(targets) > 1:
            self.inbox.decide("harvest", targets)
        elif targets:
            self._decide("harvest", targets)

    def action_mark(self) -> None:
        highlighted = self._highlighted()
        if highlighted is None:
            return
        key = registry.qualified_id(highlighted)
        self._stale = False
        self._set_marks(self._marks ^ {key})

    def action_mark_all(self) -> None:
        ids = frozenset(registry.qualified_id(t) for t in self._rows)
        self._set_marks(frozenset() if ids and ids <= self._marks else ids)

    def action_enter(self, target: EnterTarget) -> None:
        if self._rows and target == "resume":
            self.action_resume()
        elif self._rows:
            self.action_detail()
        elif self._following is not None:
            self.app.switch_screen(TriageScreen(self._following.scope, self.keymap))

    def action_detail(self) -> None:
        highlighted = self._highlighted()
        if highlighted is not None:
            self.app.push_screen(DetailScreen(highlighted))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_detail()

    def action_resume(self) -> None:
        highlighted = self._highlighted()
        if highlighted is not None:
            self.inbox.resume_thread(highlighted)

    def action_handoff(self) -> None:
        if isinstance(self.scope, ProjectScope):
            self.inbox.start_triage_handoff(self.scope.project)
            return
        highlighted = self._highlighted()
        if highlighted is None:
            return
        key = overview.group_key(highlighted)
        projects = self.inbox.home_state().projects
        project = next((p for p in projects if p.key == key), None)
        self.inbox.start_triage_handoff(project or thread_project(highlighted))
