from rich.text import Text
from ringleader.tui.rows import removed_indices, set_rows, target_index
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


def test_removed_indices_finds_rows_dropped_in_order():
    assert removed_indices(["a", "b", "c", "d"], ["a", "c"]) == [1, 3]


def test_removed_indices_gives_up_on_an_insertion_or_a_reorder():
    assert (
        removed_indices(["a", "b"], ["a", "b", "c"]),
        removed_indices(["a", "b"], ["b", "a"]),
    ) == (
        None,
        None,
    )


async def test_an_unchanged_rebuild_keeps_every_option_and_posts_nothing():
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        rows = app.query_one("#rows", OptionList)
        set_rows(rows, _rows("a", "b", "c"), keep="b")
        await until(pilot, lambda: app.highlights == ["b"])
        options = list(rows.options)
        set_rows(rows, _rows("a", "b", "c"), keep="b")
        await pilot.pause()
        assert all(new is old for new, old in zip(rows.options, options, strict=True))
        assert app.highlights == ["b"]


async def test_a_changed_row_is_replaced_in_place():
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        rows = app.query_one("#rows", OptionList)
        set_rows(rows, _rows("a", "b", "c"), keep="c")
        options = list(rows.options)
        set_rows(
            rows, [("a", Text("row a")), ("b", Text("changed")), ("c", Text("row c"))], keep="c"
        )
        await pilot.pause()
        assert [new is old for new, old in zip(rows.options, options, strict=True)] == [
            True,
            True,
            True,
        ]
        assert str(rows.get_option_at_index(1).prompt) == "changed"
        assert _highlighted_key(rows) == "c"


async def test_a_removed_row_keeps_the_others_and_the_highlight_on_its_key():
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        rows = app.query_one("#rows", OptionList)
        set_rows(rows, _rows("a", "b", "c", "d"), keep="d")
        kept = [rows.get_option_at_index(i) for i in (0, 2, 3)]
        set_rows(rows, _rows("a", "c", "d"), keep="d")
        await pilot.pause()
        assert all(new is old for new, old in zip(rows.options, kept, strict=True))
        assert (_highlighted_key(rows), rows.highlighted) == ("d", 2)


async def test_removing_the_highlighted_row_moves_to_the_next_and_posts_it():
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        rows = app.query_one("#rows", OptionList)
        set_rows(rows, _rows("a", "b", "c"), keep="b")
        await until(pilot, lambda: app.highlights == ["b"])
        set_rows(rows, _rows("a", "c"), keep="b")
        await until(pilot, lambda: app.highlights == ["b", "c"])
        assert _highlighted_key(rows) == "c"


async def test_an_unchanged_rebuild_keeps_the_scroll_position():
    app = Host()
    async with app.run_test(size=(100, 10)) as pilot:
        rows = app.query_one("#rows", OptionList)
        keys = [f"k{n:02d}" for n in range(40)]
        set_rows(rows, _rows(*keys), keep="k05")
        await pilot.pause()
        rows.scroll_to(y=3, animate=False, immediate=True)
        await pilot.pause()
        set_rows(rows, _rows(*keys), keep="k05")
        await pilot.pause()
        assert (rows.scroll_y, _highlighted_key(rows)) == (3, "k05")


async def test_every_row_changing_without_a_removal_is_replaced_in_place():
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        rows = app.query_one("#rows", OptionList)
        set_rows(rows, _rows("a", "b", "c", "d"), keep="c")
        options = list(rows.options)
        set_rows(rows, [(key, Text(f"aged {key}")) for key in "abcd"], keep="c")
        await pilot.pause()
        assert [new is old for new, old in zip(rows.options, options, strict=True)] == [True] * 4
        assert [str(option.prompt) for option in rows.options] == [f"aged {k}" for k in "abcd"]


async def test_a_removal_with_most_rows_changed_rebuilds_the_list():
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        rows = app.query_one("#rows", OptionList)
        set_rows(rows, _rows("a", "b", "c", "d", "e"), keep="c")
        options = list(rows.options)
        set_rows(rows, [(key, Text(f"aged {key}")) for key in "abcd"], keep="c")
        await pilot.pause()
        assert [any(new is old for old in options) for new in rows.options] == [False] * 4
        assert ([str(option.prompt) for option in rows.options], _highlighted_key(rows)) == (
            [f"aged {k}" for k in "abcd"],
            "c",
        )


async def test_a_removal_with_few_rows_changed_patches_the_survivors():
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        rows = app.query_one("#rows", OptionList)
        set_rows(rows, _rows("a", "b", "c", "d", "e"), keep="c")
        kept = [rows.get_option_at_index(i) for i in (0, 1, 2, 3)]
        set_rows(rows, [*_rows("a", "b", "c"), ("d", Text("aged d"))], keep="c")
        await pilot.pause()
        assert [new is old for new, old in zip(rows.options, kept, strict=True)] == [True] * 4
        assert str(rows.get_option_at_index(3).prompt) == "aged d"
