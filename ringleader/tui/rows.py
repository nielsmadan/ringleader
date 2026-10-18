from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rich.text import Text
from textual.widgets import OptionList
from textual.widgets.option_list import Option

MAX_PATCHED_REMOVALS = 64
MAX_PATCHED_CHANGE_SHARE_WITH_REMOVALS = 0.75


@dataclass(frozen=True)
class Patch:
    removed: tuple[int, ...]
    changed: tuple[int, ...]


def target_index(keys: Sequence[str], keep: str | None, previous: int | None) -> int | None:
    if not keys:
        return None
    if keep is not None and keep in keys:
        return keys.index(keep)
    return min(previous or 0, len(keys) - 1)


def removed_indices(current: Sequence[str | None], keys: Sequence[str]) -> list[int] | None:
    if len(keys) > len(current) or len(current) - len(keys) > MAX_PATCHED_REMOVALS:
        return None
    removed: list[int] = []
    position = 0
    for index, key in enumerate(current):
        if position < len(keys) and key == keys[position]:
            position += 1
        else:
            removed.append(index)
    return removed if position == len(keys) else None


def plan_patch(current: Sequence[Option], rows: Sequence[tuple[str, Text]]) -> Patch | None:
    removed = removed_indices([option.id for option in current], [key for key, _ in rows])
    if removed is None:
        return None
    gone = set(removed)
    kept = [option for index, option in enumerate(current) if index not in gone]
    changed = tuple(
        index
        for index, ((_, text), option) in enumerate(zip(rows, kept, strict=True))
        if option.prompt != text
    )
    if removed and len(changed) > len(rows) * MAX_PATCHED_CHANGE_SHARE_WITH_REMOVALS:
        return None
    return Patch(tuple(removed), changed)


def _apply(option_list: OptionList, rows: Sequence[tuple[str, Text]], patch: Patch) -> None:
    for index in reversed(patch.removed):
        option_list.remove_option_at_index(index)
    for index in patch.changed:
        option_list.replace_option_prompt_at_index(index, rows[index][1])


def set_rows(
    option_list: OptionList, rows: Sequence[tuple[str, Text]], *, keep: str | None
) -> None:
    previous = option_list.highlighted
    shown = highlighted_key(option_list)
    patch = plan_patch(option_list.options, rows)
    if patch is None:
        option_list.set_options(Option(text, id=key) for key, text in rows)
    else:
        _apply(option_list, rows, patch)
    target = target_index([key for key, _ in rows], keep, previous)
    moved = option_list.highlighted != target or highlighted_key(option_list) != shown
    if patch is None or moved:
        option_list.highlighted = None
        option_list.highlighted = target


def highlighted_key(option_list: OptionList) -> str | None:
    option = option_list.highlighted_option
    return option.id if option is not None else None
