from __future__ import annotations

import time
from collections.abc import Callable

PRINT_COALESCE_SECONDS = 1.0


class PrintCoalescer:
    def __init__(
        self,
        *,
        toast: Callable[[str], None],
        schedule: Callable[[float, Callable[[], None]], object],
        clock: Callable[[], float] = time.monotonic,
        window: float = PRINT_COALESCE_SECONDS,
    ) -> None:
        self._toast = toast
        self._schedule = schedule
        self._clock = clock
        self._window = window
        self._buffer = ""
        self._lines: list[str] = []
        self._last_toast = float("-inf")
        self._timer_armed = False

    def feed(self, text: str) -> None:
        *complete, self._buffer = (self._buffer + text).split("\n")
        self._lines.extend(line for line in complete if line.strip())
        self._flush()

    def _timed_flush(self) -> None:
        self._timer_armed = False
        self._flush()

    def _flush(self) -> None:
        if not self._lines or self._timer_armed:
            return
        wait = self._last_toast + self._window - self._clock()
        if wait > 0:
            self._timer_armed = True
            self._schedule(wait, self._timed_flush)
            return
        lines, self._lines = self._lines, []
        self._last_toast = self._clock()
        self._toast("\n".join(lines))
