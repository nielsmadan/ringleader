from rich.text import Text
from ringleader.tui.rows import set_rows, target_index
from textual.app import App, ComposeResult
from textual.widgets import OptionList

from tui_helpers import until


class Host(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.highlights: list[str | None] = []

    def compose(self) -> ComposeResult:
        yield OptionList(id="rows")

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.highlights.append(event.option.id)


def _rows(*keys):
    return [(key, Text(f"row {key}")) for key in keys]


def _highlighted_key(rows: OptionList) -> str | None:
    assert rows.highlighted is not None
    return rows.get_option_at_index(rows.highlighted).id


def test_target_index_prefers_the_kept_key():
    assert target_index(["a", "b", "c"], "c", 0) == 2


def test_target_index_clamps_the_previous_index():
    assert target_index(["a", "b"], "gone", 5) == 1


def test_target_index_starts_at_the_top_without_a_previous_highlight():
    assert target_index(["a", "b"], None, None) == 0


async def test_a_rebuild_keeps_the_highlighted_key():
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        rows = app.query_one("#rows", OptionList)
        set_rows(rows, _rows("a", "b", "c"), keep=None)
        rows.highlighted = 1
        set_rows(rows, _rows("z", "c", "a", "b"), keep="b")
        await pilot.pause()
        assert _highlighted_key(rows) == "b"
        assert rows.highlighted == 3


async def test_a_rebuild_falls_back_to_the_clamped_index_when_the_key_vanished():
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        rows = app.query_one("#rows", OptionList)
        set_rows(rows, _rows("a", "b", "c"), keep=None)
        rows.highlighted = 2
        set_rows(rows, _rows("a", "b"), keep="c")
        await pilot.pause()
        assert _highlighted_key(rows) == "b"


async def test_a_rebuild_posts_option_highlighted_for_the_kept_row():
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        rows = app.query_one("#rows", OptionList)
        set_rows(rows, _rows("a", "b"), keep="b")
        await until(pilot, lambda: app.highlights == ["b"])
        set_rows(rows, _rows("b", "c"), keep="b")
        await until(pilot, lambda: app.highlights == ["b", "b"])
