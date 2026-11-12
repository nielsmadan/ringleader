from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Footer, Header, OptionList, Static, Tree
from textual.widgets.tree import TreeNode as TreeItem

from ... import overview
from ...tui.keys import nav_bindings
from ...tui.rows import highlighted_key, set_rows
from ..base import InboxScreen
from ..state import (
    HomeState,
    ProjectsView,
    ViewEntry,
    ViewScope,
    handoff_key,
    plural,
    scope_title,
)
from ..views import FolderNode, folder_of
from .triage import TriageScreen

LABEL_WIDTH = 36
AGE_WIDTH = 6
VIEWS = tuple(ProjectsView)
EXPANDED_DEPTH = 1
HANDOFF_NEEDS_ONE_PROJECT = "a hand-off needs one project — {title} spans several"


def _age(iso: str, now_iso: str) -> str:
    return overview.age_human(iso, now_iso).removesuffix(" ago")


def _fit(label: str, *, keep_tail: bool) -> str:
    if len(label) <= LABEL_WIDTH:
        return label
    return "…" + label[1 - LABEL_WIDTH :] if keep_tail else label[: LABEL_WIDTH - 1] + "…"


def entry_row(entry: ViewEntry, now_iso: str, *, keep_tail: bool = False) -> Text:
    shown = _fit(entry.label, keep_tail=keep_tail)
    text = Text(f"{shown:<{LABEL_WIDTH}} ", style="bold")
    text.append(f"{entry.pending:>5} waiting  ")
    text.append(f"{entry.unclassified:>5} unclassified  ", style="dim")
    text.append(f"oldest {_age(entry.oldest_mtime, now_iso):<{AGE_WIDTH}} ")
    text.append(f"newest {_age(entry.newest_mtime, now_iso)}", style="dim")
    return text


_NOUNS = {
    ProjectsView.REPOS: "project",
    ProjectsView.FOLDERS: "folder",
    ProjectsView.TREE: "folder",
    ProjectsView.DATES: "period",
}


def projects_title(view: ProjectsView, *, waiting: int, groups: int) -> str:
    return f"Projects · {view.label} — {waiting} waiting across {plural(groups, _NOUNS[view])}"


def _contains(path: str, folder: str) -> bool:
    return folder.startswith(path.rstrip("/") + "/")


def tree_label(node: FolderNode) -> Text:
    text = Text(node.name, style="bold")
    text.append(f"  {node.total}", style="dim")
    return text


class FolderTree(Tree[FolderNode]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("right,l", "expand_node", "expand", show=False),
        Binding("left,h", "collapse_node", "collapse", show=False),
    ]

    def action_expand_node(self) -> None:
        node = self.cursor_node
        if node is not None and node.allow_expand:
            node.expand()

    def action_collapse_node(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        if node.allow_expand and node.is_expanded:
            node.collapse()
        elif node.parent is not None and node.parent is not self.root:
            self.action_cursor_parent()


class ProjectsScreen(InboxScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        *nav_bindings(),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("t", "handoff", "triage with an agent"),
        Binding("tab", "cycle_view(1)", "next view"),
        Binding("shift+tab", "cycle_view(-1)", "previous view", show=False),
    ]

    DEFAULT_CSS = """
    ProjectsScreen #projects-title, ProjectsScreen #projects-empty {
        padding: 0 1;
    }
    ProjectsScreen #projects-title {
        text-style: bold;
    }
    ProjectsScreen #projects, ProjectsScreen #tree {
        height: 1fr;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._entries: tuple[ViewEntry, ...] = ()
        self._shown: tuple[HomeState, ProjectsView] | None = None

    @property
    def view(self) -> ProjectsView:
        return self.inbox.projects_memory.view

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="projects-title")
        yield OptionList(id="projects", compact=True)
        tree = FolderTree("folders", id="tree")
        tree.show_root = False
        tree.auto_expand = False
        yield tree
        yield Static(Text("Nothing waiting — escape for Home"), id="projects-empty")
        yield Footer()

    def on_mount(self) -> None:
        self.sync()

    def render_data(self) -> None:
        if not self.query("#projects"):
            return
        state = self.inbox.home_state()
        if self._shown == (state, self.view):
            return
        focus_moves = self._shown is None or self._shown[1] != self.view
        self._shown = (state, self.view)
        views = self.inbox.session_views()
        is_tree = self.view is ProjectsView.TREE
        projects = self.query_one("#projects", OptionList)
        tree = self.query_one("#tree", FolderTree)
        projects.display = not is_tree
        tree.display = is_tree
        if is_tree:
            self._entries = ()
            self._render_tree(tree, views.tree)
            groups = len({folder_of(t) for t in state.inbox})
        else:
            self._entries = self.inbox.view_entries(self.view)
            keeps_tail = self.view is ProjectsView.FOLDERS
            set_rows(
                projects,
                [
                    (entry.key, entry_row(entry, state.now_iso, keep_tail=keeps_tail))
                    for entry in self._entries
                ],
                keep=None if focus_moves else highlighted_key(projects),
            )
            if focus_moves and self._entries:
                projects.highlighted = 0
            groups = len(self._entries)
        title = projects_title(self.view, waiting=len(state.inbox), groups=groups)
        self.query_one("#projects-title", Static).update(Text(title))
        self.query_one("#projects-empty", Static).display = not state.inbox
        if focus_moves:
            (tree if is_tree else projects).focus()

    def _expanded(self, node: FolderNode, depth: int) -> bool:
        return self.inbox.projects_memory.expanded(node.path, default=depth <= EXPANDED_DEPTH)

    def _render_tree(self, tree: FolderTree, roots: tuple[FolderNode, ...]) -> None:
        cursor = tree.cursor_node.data.path if tree.cursor_node and tree.cursor_node.data else None
        tree.move_cursor(None)
        tree.clear()
        items: dict[str, TreeItem[FolderNode]] = {}
        pending = [(tree.root, root, 0) for root in reversed(roots)]
        while pending:
            parent, node, depth = pending.pop()
            items[node.path] = parent.add(
                tree_label(node),
                data=node,
                expand=bool(node.children) and self._expanded(node, depth),
                allow_expand=bool(node.children),
            )
            pending.extend(
                (items[node.path], child, depth + 1) for child in reversed(node.children)
            )
        # reading last_line builds the line cache that gives each visible node its line
        _ = tree.last_line
        target = self._restore_target(items, cursor) if cursor is not None else None
        if target is None:
            tree.cursor_line = 0
        else:
            tree.move_cursor(target)

    @staticmethod
    def _restore_target(
        items: dict[str, TreeItem[FolderNode]], cursor: str
    ) -> TreeItem[FolderNode] | None:
        item = items.get(cursor)
        if item is None:
            enclosing = [path for path in items if _contains(path, cursor)]
            item = items[max(enclosing, key=len)] if enclosing else None
        while item is not None and item.line < 0:
            item = item.parent
        return item if item is not None and item.line >= 0 else None

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[FolderNode]) -> None:
        if event.node.data is not None:
            self.inbox.projects_memory.remember_expanded(event.node.data.path, expanded=True)

    def on_tree_node_collapsed(self, event: Tree.NodeCollapsed[FolderNode]) -> None:
        if event.node.data is not None:
            self.inbox.projects_memory.remember_expanded(event.node.data.path, expanded=False)

    def action_cycle_view(self, step: int) -> None:
        index = (VIEWS.index(self.view) + step) % len(VIEWS)
        self.inbox.projects_memory.view = VIEWS[index]
        self.render_data()

    def _highlighted(self) -> ViewScope | None:
        if self.view is ProjectsView.TREE:
            node = self.query_one("#tree", FolderTree).cursor_node
            return node.data.scope if node is not None and node.data is not None else None
        index = self.query_one("#projects", OptionList).highlighted
        if index is None or not (0 <= index < len(self._entries)):
            return None
        return self._entries[index].scope

    def _list(self) -> OptionList | FolderTree:
        if self.view is ProjectsView.TREE:
            return self.query_one("#tree", FolderTree)
        return self.query_one("#projects", OptionList)

    def action_cursor_down(self) -> None:
        self._list().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._list().action_cursor_up()

    def _open(self, scope: ViewScope) -> None:
        self.app.push_screen(TriageScreen(scope, self.inbox.keymap))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if 0 <= event.option_index < len(self._entries):
            self._open(self._entries[event.option_index].scope)

    def on_tree_node_selected(self, event: Tree.NodeSelected[FolderNode]) -> None:
        event.stop()
        if event.node.data is not None:
            self._open(event.node.data.scope)

    def action_handoff(self) -> None:
        scope = self._highlighted()
        if scope is None:
            return
        key = handoff_key(scope, self.inbox.view_threads(), self.inbox.now_iso())
        project = next((p for p in self.inbox.home_state().projects if p.key == key), None)
        if project is None:
            title = scope_title(scope, self.inbox.home_dir)
            self.notify(
                HANDOFF_NEEDS_ONE_PROJECT.format(title=title), severity="warning", markup=False
            )
            return
        self.inbox.start_triage_handoff(project)
