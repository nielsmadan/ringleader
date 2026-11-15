from datetime import UTC, datetime

import pytest
from rich.text import Text
from ringleader.tui.modals import (
    AgentChoice,
    AgentModal,
    AgentPickModal,
    ChoiceModal,
    ChoiceOption,
    ConfirmModal,
    ForkNameModal,
    SnoozeModal,
)
from ringleader.tui.snapshot import screen_text
from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Label, ListView, OptionList

from tui_helpers import drawn_text, until

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
UNSET = object()


class Host(App[None]):
    def __init__(self, modal: ModalScreen[object]) -> None:
        super().__init__()
        self.modal = modal
        self.result: object = UNSET

    def compose(self) -> ComposeResult:
        yield Label("background screen")

    def on_mount(self) -> None:
        self.push_screen(self.modal, callback=self._store)

    def _store(self, result: object) -> None:
        self.result = result


def _choices():
    return ChoiceModal(
        "Enrich 40 sessions? about 3 min",
        [
            ChoiceOption("Run, enrich all 40", "all", "about 3 min"),
            ChoiceOption("Run, enrich the 6 from the last 7 days", "recent", "about 30 s"),
            ChoiceOption("Cancel", None),
        ],
    )


async def _run(modal, *keys):
    app = Host(modal)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.screen is modal)
        await pilot.press(*keys)
        await until(pilot, lambda: app.result is not UNSET)
    return app.result


DIALOGS = [
    pytest.param(lambda: AgentModal({"source": "codex"}), id="resume-agent"),
    pytest.param(_choices, id="choice"),
    pytest.param(lambda: AgentPickModal("Which agent?", ["claude-code", "codex"]), id="agent"),
    pytest.param(lambda: ConfirmModal(Text("Continue? [y/n]")), id="confirm"),
    pytest.param(lambda: ForkNameModal("new fork"), id="fork-name"),
    pytest.param(lambda: SnoozeModal(lambda: NOW), id="snooze"),
]


@pytest.mark.parametrize("make_modal", DIALOGS)
async def test_dialog_fits_its_content_and_stays_centered_when_resized(make_modal):
    modal = make_modal()
    app = Host(modal)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        box = modal.query_one("Grid")
        height = box.region.height
        children = [child for child in box.children if child.display]
        assert height == box.styles.gutter.height + sum(
            child.outer_size.height for child in children
        ) + box.styles.grid_gutter_vertical * (len(children) - 1)
        assert 0 < box.region.y < box.region.bottom < app.size.height
        assert abs(box.region.x * 2 + box.region.width - app.size.width) <= 1
        assert abs(box.region.y * 2 + height - app.size.height) <= 1

        await pilot.resize_terminal(120, 60)
        await pilot.pause()
        assert box.region.height == height
        assert abs(box.region.x * 2 + box.region.width - app.size.width) <= 1
        assert abs(box.region.y * 2 + height - app.size.height) <= 1


@pytest.mark.parametrize("make_modal", DIALOGS)
async def test_dialog_stays_within_a_small_terminal(make_modal):
    modal = make_modal()
    app = Host(modal)
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.pause()
        box = modal.query_one("Grid")
        assert app.screen.region.contains_region(box.region)
        await pilot.press("escape")
        await until(pilot, lambda: app.result is not UNSET)


async def test_resume_agent_scrolls_to_the_last_choice_in_a_short_terminal(monkeypatch):
    agents = ("claude-code", "codex", "cursor", "droid", "opencode", "pi")
    monkeypatch.setattr("ringleader.tui.modals.launchable_agents", lambda: agents)
    modal = AgentModal({"source": "codex"})
    app = Host(modal)
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.pause()
        await pilot.press(*(["down"] * (len(agents) + 1)))
        await pilot.pause()
        options = modal.query_one(ListView)
        assert options.highlighted_child is not None
        assert options.scroll_y > 0
        assert options.scrollable_content_region.contains_region(options.highlighted_child.region)
        assert modal.query_one("Grid").scrollable_content_region.contains_region(options.region)
        await pilot.press("enter")
        await until(pilot, lambda: app.result is not UNSET)
    assert app.result == AgentChoice("pi")


async def test_choice_modal_returns_the_highlighted_value_on_enter():
    assert await _run(_choices(), "enter") == "all"


async def test_choice_modal_enter_returns_the_initial_option():
    modal = ChoiceModal(
        "which?", [ChoiceOption("first", "a"), ChoiceOption("last", "z")], initial=1
    )
    assert await _run(modal, "enter") == "z"


async def test_choice_modal_picks_by_digit():
    assert await _run(_choices(), "2") == "recent"


async def test_choice_modal_follows_the_cursor():
    assert await _run(_choices(), "down", "enter") == "recent"


async def test_choice_modal_escape_returns_none():
    assert await _run(_choices(), "escape") is None


async def test_choice_modal_golden(golden):
    modal = _choices()
    app = Host(modal)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.screen is modal)
        await pilot.pause()
        golden("modals/choice", await drawn_text(pilot, app))


async def test_choice_modal_renders_a_bracketed_title_verbatim():
    modal = ChoiceModal("[cadet-city] [/] which?", [ChoiceOption("[/] first", 1)])
    app = Host(modal)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.screen is modal)
        await pilot.pause()
        text = screen_text(app)
    assert "[cadet-city] [/] which?" in text
    assert "[/] first" in text


async def test_snooze_modal_returns_the_iso_timestamp_for_a_duration():
    result = await _run(SnoozeModal(lambda: NOW), *"3d", "enter")
    assert result == "2026-10-18T12:00:00+00:00"


async def test_snooze_modal_shows_an_error_for_an_unreadable_duration():
    modal = SnoozeModal(lambda: NOW)
    app = Host(modal)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.screen is modal)
        await pilot.press(*"soon-ish", "enter")
        await until(pilot, lambda: "can't read 'soon-ish'" in screen_text(app))
        assert app.screen is modal
        await pilot.press("escape")
        await until(pilot, lambda: app.result is not UNSET)
    assert app.result is None


async def test_snooze_modal_shows_an_error_for_an_out_of_range_duration():
    modal = SnoozeModal(lambda: NOW)
    app = Host(modal)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.screen is modal)
        await pilot.press(*"3000000d", "enter")
        await until(pilot, lambda: "can't read '3000000d'" in screen_text(app))
        assert app.screen is modal
        await pilot.press("escape")
        await until(pilot, lambda: app.result is not UNSET)
    assert app.result is None


async def test_agent_pick_modal_returns_the_canonical_source_name():
    modal = AgentPickModal("Publish 3 harvests with which agent?", ["claude-code", "codex"])
    assert await _run(modal, "enter") == "claude-code"


async def test_agent_pick_modal_shows_display_names():
    modal = AgentPickModal("Publish 3 harvests with which agent?", ["claude-code", "codex"])
    app = Host(modal)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.screen is modal)
        options = app.screen.query_one("#choice-options", OptionList)
        labels = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
    assert labels == ["claude", "codex"]
