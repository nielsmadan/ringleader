from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class Done[T]:
    value: T


@dataclass(frozen=True)
class Failed:
    error: BaseException
    trace: str


type Job[T, P] = Callable[[CancelToken, Callable[[P], None]], T]


class _Throttle[P]:
    def __init__(self, forward: Callable[[P], None], interval: float) -> None:
        self._forward = forward
        self._interval = interval
        self._lock = threading.Lock()
        self._last = float("-inf")
        self._pending: list[P] = []
        self._timer: threading.Timer | None = None
        self._closed = False

    def report(self, progress: P) -> None:
        with self._lock:
            if self._closed:
                return
            now = time.monotonic()
            if now - self._last >= self._interval:
                self._send(progress, now)
                return
            self._pending = [progress]
            if self._timer is None:
                self._timer = threading.Timer(self._last + self._interval - now, self._release)
                self._timer.daemon = True
                self._timer.start()

    def _send(self, progress: P, now: float) -> None:
        self._last = now
        self._pending = []
        self._forward(progress)

    def _release(self) -> None:
        with self._lock:
            self._timer = None
            if self._pending and not self._closed:
                self._send(self._pending[0], time.monotonic())

    def close(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._pending:
                self._send(self._pending[0], time.monotonic())
            self._closed = True


def start_daemon[T, P](
    job: Job[T, P],
    *,
    on_progress: Callable[[P], None],
    on_result: Callable[[Done[T] | Failed], None],
    name: str,
    token: CancelToken | None = None,
    progress_interval: float = 0.1,
) -> CancelToken:
    active = token if token is not None else CancelToken()
    throttle = _Throttle(on_progress, progress_interval)

    def run() -> None:
        result: Done[T] | Failed
        try:
            result = Done(job(active, throttle.report))
        except BaseException as error:  # noqa: BLE001 -- the result callback is the only exit
            result = Failed(error, traceback.format_exc())
        throttle.close()
        on_result(result)

    threading.Thread(target=run, name=name, daemon=True).start()
    return active
