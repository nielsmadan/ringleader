from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Grid
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, OptionList

from .. import fuzzytime, resume
from ..resume import SessionAction, agent_name, launchable_agents
from .keys import nav_bindings
from .types import Clock

_BOX_CSS = """
    grid-size: 1;
    grid-gutter: 1;
    padding: 1 2;
    width: 60;
    height: auto;
    border: thick $accent;
    background: $surface;
"""


@dataclass(frozen=True)
class AgentChoice:
    target: str
    action: SessionAction = "resume"


def agent_row_text(choice: AgentChoice, *, original: bool) -> Text:
    suffix = "resume existing" if original else "hand off"
    if choice.action == "fork":
        suffix = "fork into new session"
    return Text(f"{agent_name(choice.target):<9} ({suffix})")


class AgentModal(ModalScreen[AgentChoice | None]):
    """Choose a same-agent resume or fork, or a handoff to another agent."""

    BINDINGS: ClassVar[list[BindingType]] = [
        *nav_bindings(),
        Binding("escape", "cancel", "cancel", show=False),
    ]

    DEFAULT_CSS = f"""
    AgentModal {{
        align: center middle;
    }}
    #agent-box {{{_BOX_CSS}}}
    """

    def __init__(self, thread: dict[str, Any]) -> None:
        super().__init__()
        self._source = resume.source_of(thread)
        agents = launchable_agents()
        own = (
            [AgentChoice(self._source), AgentChoice(self._source, "fork")]
            if self._source in agents
            else []
        )
        self._choices = [*own, *(AgentChoice(a) for a in agents if a != self._source)]

    def compose(self) -> ComposeResult:
        yield Grid(
            Label("resume with which agent?"),
            ListView(
                *(
                    ListItem(Label(agent_row_text(choice, original=choice.target == self._source)))
                    for choice in self._choices
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
        if index is None or not (0 <= index < len(self._choices)):
            return
        self.dismiss(self._choices[index])

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "confirm", "yes", show=False),
        Binding("n", "cancel", "no", show=False),
        Binding("escape", "cancel", "cancel", show=False),
    ]

    DEFAULT_CSS = f"""
    ConfirmModal {{
        align: center middle;
    }}
    #confirm-box {{{_BOX_CSS}}}
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


@dataclass(frozen=True)
class ChoiceOption[T]:
    label: str
    value: T
    detail: str | None = None


def _choice_text(option: ChoiceOption[Any]) -> Text:
    if option.detail is None:
        return Text(option.label)
    return Text(f"{option.label} — {option.detail}")


class ChoiceModal[T](ModalScreen[T | None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        *nav_bindings(),
        *(Binding(str(digit), f"pick({digit - 1})", show=False) for digit in range(1, 10)),
        Binding("escape", "cancel", "cancel", show=False),
    ]

    DEFAULT_CSS = f"""
    ChoiceModal {{
        align: center middle;
    }}
    #choice-box {{{_BOX_CSS}
        width: 80;
    }}
    #choice-options {{
        height: auto;
        max-height: 12;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }}
    #choice-hint {{
        color: $text-muted;
    }}
    """

    def __init__(self, title: Text | str, options: Sequence[ChoiceOption[T]]) -> None:
        super().__init__()
        self._title = Text(title) if isinstance(title, str) else title
        self._options = tuple(options)

    def compose(self) -> ComposeResult:
        yield Grid(
            Label(self._title, id="choice-title"),
            OptionList(*(_choice_text(option) for option in self._options), id="choice-options"),
            Label(Text("enter choose · 1-9 pick · escape cancel"), id="choice-hint"),
            id="choice-box",
        )

    def on_mount(self) -> None:
        options = self.query_one("#choice-options", OptionList)
        if self._options:
            options.highlighted = 0
        options.focus()

    def action_cursor_down(self) -> None:
        self.query_one("#choice-options", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#choice-options", OptionList).action_cursor_up()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_pick(event.option_index)

    def action_pick(self, index: int) -> None:
        if 0 <= index < len(self._options):
            self.dismiss(self._options[index].value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AgentPickModal(ChoiceModal[str]):
    def __init__(self, title: str, agents: Sequence[str]) -> None:
        super().__init__(title, [ChoiceOption(agent_name(agent), agent) for agent in agents])


class SnoozeModal(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "cancel", show=False),
    ]

    DEFAULT_CSS = f"""
    SnoozeModal {{
        align: center middle;
    }}
    #snooze-box {{{_BOX_CSS}}}
    #snooze-hint {{
        color: $text-muted;
    }}
    #snooze-error {{
        color: $error;
        display: none;
    }}
    """

    def __init__(self, now: Clock) -> None:
        super().__init__()
        self._now = now

    def compose(self) -> ComposeResult:
        yield Grid(
            Label(Text("snooze until?"), id="snooze-title"),
            Input(placeholder="3d", id="snooze-input"),
            Label(Text("3d, 2w, tomorrow, next monday…"), id="snooze-hint"),
            Label(Text(""), id="snooze-error"),
            id="snooze-box",
        )

    def on_mount(self) -> None:
        self.query_one("#snooze-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        try:
            until = fuzzytime.parse_duration(event.value, self._now())
        except (ValueError, OverflowError):
            error = self.query_one("#snooze-error", Label)
            error.update(Text(f"can't read {event.value.strip()!r} — try 3d, 2w or tomorrow"))
            error.display = True
            return
        self.dismiss(until)

    def action_cancel(self) -> None:
        self.dismiss(None)
