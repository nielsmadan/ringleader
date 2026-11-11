from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import Any

from ringleader.tui.snapshot import footers_drawn, screen_text
from textual.app import App
from textual.pilot import Pilot


async def until(pilot: Pilot[Any], predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    deadline = monotonic() + timeout
    while not predicate():
        if monotonic() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await pilot.pause(0.02)


# A Footer composes empty and fills in its keys on a later refresh.
async def drawn_text(pilot: Pilot[Any], app: App[Any]) -> str:
    await until(pilot, lambda: footers_drawn(app))
    await pilot.pause()
    return screen_text(app)
