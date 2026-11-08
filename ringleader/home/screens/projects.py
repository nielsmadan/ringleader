from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Footer, Header, OptionList, Static

from ... import overview
from ...tui.keys import nav_bindings
from ...tui.rows import highlighted_key, set_rows
from ..base import InboxScreen
from ..state import HomeState, ProjectRef, ProjectScope, plural
from .triage import TriageScreen

LABEL_WIDTH = 36
AGE_WIDTH = 6


def _age(iso: str, now_iso: str) -> str:
    return overview.age_human(iso, now_iso).removesuffix(" ago")


def project_row(project: ProjectRef, now_iso: str) -> Text:
    label = project.label
    shown = label if len(label) <= LABEL_WIDTH else label[: LABEL_WIDTH - 1] + "…"
    text = Text(f"{shown:<{LABEL_WIDTH}} ", style="bold")
    text.append(f"{project.pending:>5} waiting  ")
    text.append(f"{project.unclassified:>5} unclassified  ", style="dim")
    text.append(f"oldest {_age(project.oldest_mtime, now_iso):<{AGE_WIDTH}} ")
    text.append(f"newest {_age(project.newest_mtime, now_iso)}", style="dim")
    return text


def projects_title(projects: tuple[ProjectRef, ...]) -> str:
    waiting = sum(project.pending for project in projects)
    return f"Projects — {waiting} waiting across {plural(len(projects), 'project')}"


class ProjectsScreen(InboxScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        *nav_bindings(),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("t", "handoff", "triage with an agent"),
    ]

    DEFAULT_CSS = """
    ProjectsScreen #projects-title, ProjectsScreen #projects-empty {
        padding: 0 1;
    }
    ProjectsScreen #projects-title {
        text-style: bold;
    }
    ProjectsScreen #projects {
        height: 1fr;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    _projects: tuple[ProjectRef, ...] = ()
    _shown: HomeState | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="projects-title")
        yield OptionList(id="projects", compact=True)
        yield Static(Text("Nothing waiting — escape for Home"), id="projects-empty")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#projects", OptionList).focus()
        self.sync()

    def render_data(self) -> None:
        if not self.query("#projects"):
            return
        state = self.inbox.home_state()
        if state is self._shown:
            return
        self._shown = state
        now_iso = state.now_iso
        self._projects = state.projects
        self.query_one("#projects-title", Static).update(Text(projects_title(self._projects)))
        projects = self.query_one("#projects", OptionList)
        set_rows(
            projects,
            [(project.key, project_row(project, now_iso)) for project in self._projects],
            keep=highlighted_key(projects),
        )
        self.query_one("#projects-empty", Static).display = not self._projects

    def _highlighted(self) -> ProjectRef | None:
        index = self.query_one("#projects", OptionList).highlighted
        if index is None or not (0 <= index < len(self._projects)):
            return None
        return self._projects[index]

    def action_cursor_down(self) -> None:
        self.query_one("#projects", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#projects", OptionList).action_cursor_up()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        if 0 <= index < len(self._projects):
            self.app.push_screen(
                TriageScreen(ProjectScope(self._projects[index]), self.inbox.keymap)
            )

    def action_handoff(self) -> None:
        project = self._highlighted()
        if project is not None:
            self.inbox.start_triage_handoff(project)
