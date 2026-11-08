from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from textual.binding import Binding, BindingsMap

from ..config import KeyPreset

type EnterTarget = Literal["detail", "resume"]

_KEY_DISPLAY = {"space": "space", "asterisk": "*", "number_sign": "#"}


@dataclass(frozen=True)
class Keymap:
    preset: KeyPreset
    undo: str
    triage: tuple[Binding, ...]


def _enter(target: EnterTarget) -> Binding:
    # priority: beats the OptionList's own enter, whose select (also a click) opens detail
    return Binding("enter", f"enter('{target}')", target, show=False, priority=True)


VIM = Keymap(
    preset="vim",
    undo="u",
    triage=(
        Binding("a", "archive", "archive"),
        Binding("h", "harvest", "harvest"),
        Binding("g", "keep_going", "going"),
        Binding("s", "snooze", "snooze"),
        Binding("K", "keep", "keep"),
        Binding("d", "delete", "delete"),
        Binding("space", "mark", "mark", show=False),
        Binding("asterisk", "mark_all", "mark all", show=False),
        Binding("A", "mark_all", "mark all", show=False),
        _enter("detail"),
        Binding("r", "resume", "resume", show=False),
    ),
)

CIRCUS = Keymap(
    preset="circus",
    undo="z",
    triage=(
        Binding("e", "harvest", "harvest"),
        Binding("E", "archive", "archive"),
        Binding("r", "keep_going", "going"),
        Binding("h", "snooze", "snooze"),
        Binding("s", "keep", "keep"),
        Binding("number_sign", "delete", "delete", key_display="#"),
        Binding("x", "mark", "mark", show=False),
        Binding("asterisk", "mark_all", "mark all", show=False),
        _enter("resume"),
        Binding("o", "resume", "resume", show=False),
        Binding("v", "detail", "detail", show=False),
    ),
)

_KEYMAPS: dict[KeyPreset, Keymap] = {keymap.preset: keymap for keymap in (VIM, CIRCUS)}


def keymap_for(preset: KeyPreset) -> Keymap:
    return _KEYMAPS[preset]


def _display(binding: Binding) -> str:
    return binding.key_display or _KEY_DISPLAY.get(binding.key, binding.key)


def hint(bindings: Sequence[Binding], undo: str) -> str:
    keys: dict[str, list[str]] = {}
    for binding in bindings:
        if not binding.show:
            keys.setdefault(binding.description, []).append(_display(binding))
    parts = [f"{'/'.join(shown)} {description}" for description, shown in keys.items()]
    return " · ".join([*parts, f"{undo} undo"])


def with_bindings(base: BindingsMap, extra: Iterable[Binding]) -> BindingsMap:
    return BindingsMap.merge([base, BindingsMap(extra)])
