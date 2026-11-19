from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Footer, Header, Log, OptionList, Static
from textual.widgets.option_list import Option

from ... import overview
from ...decisions import MemoryAssignOp
from ...harvests import Harvest
from ...memory_types import MemoryOrigin
from ...tui.keys import nav_bindings
from ...tui.modals import CheckoutModal
from ...tui.rows import highlighted_key
from ..base import InboxScreen
from ..harvest_view import HarvestGroup, harvest_groups, needs_checkout, preview_line
from ..state import plural
from ..writer import Finished

NAME_WIDTH = 30
AGE_WIDTH = 9
ITEM_PREFIX = "item:"
GROUP_PREFIX = "group:"


def group_header(group: HarvestGroup) -> Text:
    return Text(f"{group.label} — {plural(len(group.items), 'harvest')}", style="bold")


def harvest_row(item: Harvest, now_iso: str) -> Text:
    age = overview.age_human(item.harvested_at, now_iso) if item.harvested_at else "—"
    name = item.name or item.origin_id
    shown = name if len(name) <= NAME_WIDTH else name[: NAME_WIDTH - 1] + "…"
    text = Text(f"  {shown:<{NAME_WIDTH}} ")
    text.append(f"{age:<{AGE_WIDTH}}", style="dim")
    if isinstance(item.origin, MemoryOrigin):
        text.append(f"memory · {item.origin.source}  ", style="dim")
    text.append(preview_line(item))
    return text


def problems_line(count: int) -> str:
    return f"{plural(count, 'harvest file')} could not be queued"


def harvest_options(groups: tuple[HarvestGroup, ...], now_iso: str) -> list[Option]:
    return [
        option
        for index, group in enumerate(groups)
        for option in (
            Option(group_header(group), id=f"{GROUP_PREFIX}{index}", disabled=True),
            *(
                Option(harvest_row(item, now_iso), id=f"{ITEM_PREFIX}{item.token}")
                for item in group.items
            ),
        )
    ]


class HarvestContentScreen(InboxScreen):
    DEFAULT_CSS = """
    HarvestContentScreen #harvest-title {
        padding: 0 1;
        text-style: bold;
    }
    HarvestContentScreen #harvest-content {
        height: 1fr;
    }
    """

    def __init__(self, item: Harvest) -> None:
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(Text(self.item.name or self.item.origin_id), id="harvest-title")
        yield Log(id="harvest-content", auto_scroll=False)
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#harvest-content", Log)
        log.write(self.item.content or "(empty harvest)")
        log.focus()


class HarvestsScreen(InboxScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        *nav_bindings(),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("enter", "view", "view"),
        Binding("p", "publish", "publish"),
        Binding("a", "assign", "assign checkout"),
        Binding("P", "publish_all", "publish all"),
        Binding("escape", "back", "back"),
    ]

    DEFAULT_CSS = """
    HarvestsScreen #harvests-title, HarvestsScreen #harvests-problems,
    HarvestsScreen #harvests-empty {
        padding: 0 1;
    }
    HarvestsScreen #harvests-title {
        text-style: bold;
    }
    HarvestsScreen #harvests-problems {
        color: $warning;
    }
    HarvestsScreen #harvest-rows {
        height: 1fr;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._groups: tuple[HarvestGroup, ...] = ()
        self._grouped: tuple[tuple[Harvest, ...] | None, frozenset[str]] | None = None
        self._listed: tuple[tuple[HarvestGroup, ...], datetime] | None = None
        self._publish_ticket: tuple[int, str] | None = None
        self._assigned: tuple[str, str, int] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="harvests-title")
        yield Static("", id="harvests-problems")
        yield OptionList(id="harvest-rows", compact=True)
        yield Static("", id="harvests-empty")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#harvest-rows", OptionList).focus()
        self.sync()

    def render_data(self) -> None:
        if not self.query("#harvest-rows"):
            return
        state = self.inbox.state
        items = state.harvests
        grouped = items, state.harvest_routed
        if grouped != self._grouped:
            self._grouped = grouped
            self._groups = harvest_groups(items, routed=state.harvest_routed) if items else ()
        count = len(items or ())
        self.query_one("#harvests-title", Static).update(
            Text(
                f"Harvests — {plural(count, 'pending harvest')}"
                + (" · enter reads one" if count else "")
            )
        )
        problems = self.query_one("#harvests-problems", Static)
        problems.update(Text(problems_line(state.harvest_problems)))
        problems.display = state.harvest_problems > 0
        basis = self.inbox.render_basis()
        listed = (self._groups, basis.minute)
        shown = self._listed
        if shown is None or shown[0] is not listed[0] or shown[1] != listed[1]:
            self._listed = listed
            self._render_rows(basis.now.isoformat())
        empty = self.query_one("#harvests-empty", Static)
        empty.update(Text("Loading harvests…" if items is None else "No pending harvests."))
        empty.display = not self._groups
        self.refresh_bindings()
        self._continue_publish()

    def _render_rows(self, now_iso: str) -> None:
        rows = self.query_one("#harvest-rows", OptionList)
        options = harvest_options(self._groups, now_iso)
        keep = highlighted_key(rows)
        rows.set_options(options)
        rows.highlighted = None
        rows.highlighted = self._target(options, keep)
        rows.display = bool(options)

    @staticmethod
    def _target(options: list[Option], keep: str | None) -> int | None:
        ids = [option.id for option in options]
        if keep in ids:
            return ids.index(keep)
        return next((i for i, option in enumerate(options) if not option.disabled), None)

    def _item(self) -> Harvest | None:
        option = self.query_one("#harvest-rows", OptionList).highlighted_option
        if option is None or option.id is None or not option.id.startswith(ITEM_PREFIX):
            return None
        token = option.id.removeprefix(ITEM_PREFIX)
        return next((i for group in self._groups for i in group.items if i.token == token), None)

    def _group(self) -> HarvestGroup | None:
        item = self._item()
        return next((g for g in self._groups if item in g.items), None) if item else None

    def action_cursor_down(self) -> None:
        self.query_one("#harvest-rows", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#harvest-rows", OptionList).action_cursor_up()

    def action_view(self) -> None:
        item = self._item()
        if item is not None:
            self.app.push_screen(HarvestContentScreen(item))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_view()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "assign":
            item = self._item() if self.query("#harvest-rows") else None
            return bool(
                item
                and needs_checkout(item, self.inbox.state.harvest_routed)
                and not self.inbox.read_only
                and self._publish_ticket is None
            )
        return super().check_action(action, parameters)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.refresh_bindings()

    def action_assign(self) -> None:
        item = self._item()
        if item:
            self._assign(item, publish=False)

    def _assign(self, item: Harvest, *, publish: bool) -> None:
        if (
            self.inbox.read_only
            or self._publish_ticket
            or not needs_checkout(item, self.inbox.state.harvest_routed)
        ):
            return

        def chosen(value: str | None) -> None:
            if value is None:
                return
            path = Path(value)
            if not path.is_absolute():
                self.notify("Enter an absolute checkout path", severity="warning")
                return
            batch = self.inbox.submit_batch(
                [MemoryAssignOp(item.token, path)], label="assign memory harvest"
            )
            if publish and batch:
                self._publish_ticket = batch.tickets[0][0], item.harvest_id

        self.app.push_screen(CheckoutModal(item.name or item.origin_id), chosen)

    def op_finished(self, event: Finished) -> None:
        if self._publish_ticket and self._publish_ticket[0] == event.ticket:
            _, harvest_id = self._publish_ticket
            self._publish_ticket = None
            if event.outcome.ok and event.outcome.path and not self.inbox.leaving:
                self._assigned = harvest_id, event.outcome.path, time.monotonic_ns()

    def ops_dropped(self, tickets: Sequence[int]) -> None:
        if self._publish_ticket and self._publish_ticket[0] in tickets:
            self._publish_ticket = None

    def _continue_publish(self) -> None:
        state = self.inbox.state
        if not self._assigned or self.app.screen is not self:
            return
        harvest_id, checkout, assigned_at = self._assigned
        if state.harvest_read_started <= assigned_at:
            return
        self._assigned = None
        for group in self._groups:
            for item in group.items:
                if item.harvest_id == harvest_id and item.folder_path == checkout:
                    if needs_checkout(item, state.harvest_routed):
                        break
                    rows = self.query_one("#harvest-rows", OptionList)
                    rows.highlighted = rows.get_option_index(f"{ITEM_PREFIX}{item.token}")
                    self.inbox.start_publish_handoff(group)
                    return
        self.notify(
            "The assigned harvest is unavailable. Refresh and select it again.", severity="warning"
        )

    def action_publish(self) -> None:
        item = self._item()
        if self._publish_ticket or self._assigned:
            return
        if item and needs_checkout(item, self.inbox.state.harvest_routed):
            self._assign(item, publish=True)
            return
        group = self._group()
        if group is not None:
            self.inbox.start_publish_handoff(group)

    def action_publish_all(self) -> None:
        self.inbox.start_publish_handoff(None)
