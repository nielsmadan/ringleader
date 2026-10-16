from __future__ import annotations

from collections.abc import Sequence

from .output import OutputScreen

USAGE_ARGVS: tuple[tuple[str, ...], ...] = (("usage",), ("usage", "windows"))


class UsageScreen(OutputScreen):
    def __init__(self) -> None:
        super().__init__(("usage",), "rl usage, then rl usage windows")

    def commands(self) -> Sequence[Sequence[str]]:
        return USAGE_ARGVS
