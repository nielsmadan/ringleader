from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Footer, Header, Log, OptionList, Static
from textual.widgets.option_list import Option

from ... import overview
from ...harvests import Harvest
from ...tui.keys import nav_bindings
from ...tui.rows import highlighted_key
from ..base import InboxScreen
from ..harvest_view import HarvestGroup, harvest_groups, preview_line
from ..state import plural

NAME_WIDTH = 30
AGE_WIDTH = 9
ITEM_PREFIX = "item:"
GROUP_PREFIX = "group:"


def group_header(group: HarvestGroup) -> Text:
    return Text(f"{group.label} — {plural(len(group.items), 'harvest')}", style="bold")


def harvest_row(item: Harvest, now_iso: str) -> Text:
    age = overview.age_human(item.harvested_at, now_iso) if item.harvested_at else "—"
    name = item.name or item.session_id
    shown = name if len(name) <= NAME_WIDTH else name[: NAME_WIDTH - 1] + "…"
    text = Text(f"  {shown:<{NAME_WIDTH}} ")
    text.append(f"{age:<{AGE_WIDTH}}", style="dim")
    text.append(preview_line(item))
    return text


def problems_line(count: int) -> str:
    return f"{plural(count, 'harvest file')} could not be read"


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
        yield Static(Text(self.item.name or self.item.session_id), id="harvest-title")
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
        self._groups = harvest_groups(items) if items else ()
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
        rows = self.query_one("#harvest-rows", OptionList)
        options = harvest_options(self._groups, self.inbox.now_iso())
        keep = highlighted_key(rows)
        rows.set_options(options)
        rows.highlighted = None
        rows.highlighted = self._target(options, keep)
        rows.display = bool(options)
        empty = self.query_one("#harvests-empty", Static)
        empty.update(Text("Loading harvests…" if items is None else "No pending harvests."))
        empty.display = not options

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

    def action_publish(self) -> None:
        group = self._group()
        if group is not None:
            self.inbox.start_publish_handoff(group)

    def action_publish_all(self) -> None:
        self.inbox.start_publish_handoff(None)
