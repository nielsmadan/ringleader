from __future__ import annotations

import textwrap
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Footer, Header, Label, OptionList, Static

from ... import overview, registry, resume
from ...tui.keys import nav_bindings
from ...tui.rows import highlighted_key, set_rows
from ..base import InboxScreen
from ..state import HomeState, NextStep


def counts_line(state: HomeState, width: int = 80) -> str:
    harvests = "…" if state.pending_harvests is None else str(state.pending_harvests)
    roundup = (
        "never run"
        if state.last_roundup_at is None
        else overview.age_human(state.last_roundup_at, state.now_iso)
        + (" (stale)" if state.roundup_stale else "")
    )
    secondary = [
        (name, count)
        for name, count in (
            ("live", state.live_count),
            ("unclassified", state.unclassified),
            ("blocked", state.blocked_count),
            ("stale", state.stale_count),
            ("kept", state.kept_count),
        )
        if count
    ]
    items = [
        f"triage {state.triage_total}",
        *(f"{name} {count}" for name, count in secondary),
        f"projects {len(state.projects)}",
        f"harvests {harvests}",
        f"roundup {roundup}",
    ]
    return wrap_items(items, width)


def wrap_items(items: list[str], width: int) -> str:
    lines: list[str] = []
    for item in items:
        if lines and len(lines[-1]) + 3 + len(item) <= width:
            lines[-1] += f" · {item}"
        else:
            lines.append(item)
    return "\n".join(lines)


def step_text(index: int, step: NextStep) -> Text:
    text = Text(f"{index}. {step.title}")
    marks = [
        *(["may delete"] if step.deletes else []),
        *(["uses a model"] if step.costs_model else []),
    ]
    if marks:
        text.append(f"  ({' · '.join(marks)})", style="dim")
    return text


def recommended_text(steps: tuple[NextStep, ...], width: int = 80) -> Text:
    if not steps:
        return Text("All caught up — nothing waiting.", style="bold")
    first = steps[0]
    text = Text("Next: ", style="bold")
    text.append(first.title, style="bold")
    detail = textwrap.fill(first.detail, width=width, initial_indent="  ", subsequent_indent="  ")
    text.append(f"\n{detail}\n  enter runs the highlighted step", style="dim")
    return text


def in_flight_text(thread: dict[str, Any], now_iso: str, *, running: bool) -> Text:
    text = Text()
    text.append(f"{resume.agent_name(resume.source_of(thread)):<9} ", style="bold")
    text.append(f"{overview.age_human(thread.get('mtime'), now_iso):<10}", style="dim")
    text.append(resume.label(thread))
    text.append(f"  {overview.effective_status(thread, now_iso)}", style="italic")
    if running:
        text.append("  [running]", style="bold")
    return text


def waking_line(threads: tuple[dict[str, Any], ...], now_iso: str) -> str:
    if not threads:
        return "Waking soon: nothing snoozed wakes in the next 7 days"
    parts = [
        f"{resume.label(t)} (in {overview.age_human(now_iso, t['snoozedUntil']).removesuffix(' ago')})"
        for t in threads
    ]
    return "Waking soon: " + ", ".join(parts)


class HomeScreen(InboxScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        *nav_bindings(),
        Binding("escape", "back", "back", show=False),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
    ]

    DEFAULT_CSS = """
    HomeScreen #counts, HomeScreen #status, HomeScreen #recommended, HomeScreen #waking {
        padding: 0 1;
    }
    HomeScreen #status {
        color: $warning;
    }
    HomeScreen #recommended {
        margin: 1 0;
    }
    HomeScreen .heading {
        padding: 0 1;
        text-style: bold;
        color: $text-muted;
    }
    HomeScreen #steps {
        height: auto;
        max-height: 8;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    HomeScreen #in-flight {
        height: 1fr;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    _steps: tuple[NextStep, ...] = ()
    _in_flight: tuple[dict[str, Any], ...] = ()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="counts")
        yield Static("", id="status")
        yield Static("", id="recommended")
        yield Label("Next steps", id="steps-heading", classes="heading")
        yield OptionList(id="steps", compact=True)
        yield Label("In flight", id="in-flight-heading", classes="heading")
        yield OptionList(id="in-flight", compact=True)
        yield Static("", id="waking")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#steps", OptionList).focus()
        self.sync()

    def render_data(self) -> None:
        if not self.query("#steps"):
            return
        inbox = self.inbox
        state = inbox.home_state()
        self._steps = inbox.home_steps(state)
        self._in_flight = state.in_flight
        self.query_one("#counts", Static).update(Text(counts_line(state, self.size.width or 80)))
        status = inbox.status_line()
        status_widget = self.query_one("#status", Static)
        status_widget.update(Text(status))
        status_widget.display = bool(status)
        self.query_one("#recommended", Static).update(
            recommended_text(self._steps, self.size.width or 80)
        )
        steps = self.query_one("#steps", OptionList)
        set_rows(
            steps,
            [(f"{step.kind}", step_text(index, step)) for index, step in enumerate(self._steps, 1)],
            keep=highlighted_key(steps),
        )
        steps.display = bool(self._steps)
        self.query_one("#steps-heading", Label).display = bool(self._steps)
        self.query_one("#in-flight-heading", Label).update(
            Text("In flight" if self._in_flight else "In flight — nothing in the last 7 days")
        )
        in_flight = self.query_one("#in-flight", OptionList)
        set_rows(
            in_flight,
            [
                (
                    registry.qualified_id(t),
                    in_flight_text(t, state.now_iso, running=inbox.session_running(t)),
                )
                for t in self._in_flight
            ],
            keep=highlighted_key(in_flight),
        )
        self.query_one("#waking", Static).update(
            Text(waking_line(state.waking_soon, state.now_iso))
        )

    def on_resize(self) -> None:
        self.render_data()

    def action_cursor_down(self) -> None:
        if isinstance(self.focused, OptionList):
            self.focused.action_cursor_down()

    def action_cursor_up(self) -> None:
        if isinstance(self.focused, OptionList):
            self.focused.action_cursor_up()

    def action_back(self) -> None:
        return None

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        if event.option_list.id == "steps" and 0 <= index < len(self._steps):
            self.inbox.activate(self._steps[index])
        elif event.option_list.id == "in-flight" and 0 <= index < len(self._in_flight):
            self.inbox.resume_thread(self._in_flight[index])
