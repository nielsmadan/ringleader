from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import Any

from textual.pilot import Pilot


async def until(pilot: Pilot[Any], predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    deadline = monotonic() + timeout
    while not predicate():
        if monotonic() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await pilot.pause(0.02)
