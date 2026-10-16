from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.widgets import OptionList
from textual.widgets.option_list import Option


def target_index(keys: Sequence[str], keep: str | None, previous: int | None) -> int | None:
    if not keys:
        return None
    if keep is not None and keep in keys:
        return keys.index(keep)
    return min(previous or 0, len(keys) - 1)


def set_rows(
    option_list: OptionList, rows: Sequence[tuple[str, Text]], *, keep: str | None
) -> None:
    previous = option_list.highlighted
    option_list.set_options(Option(text, id=key) for key, text in rows)
    option_list.highlighted = None
    option_list.highlighted = target_index([key for key, _ in rows], keep, previous)


def highlighted_key(option_list: OptionList) -> str | None:
    option = option_list.highlighted_option
    return option.id if option is not None else None
