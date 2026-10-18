from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import registry, stateio
from .progress import CancelCheck, never_cancelled
from .revalidate import Identity, PresenceProbe

PRESENCE_MAX_AGE = 1.0

type Clock = Callable[[], float]


def no_presence() -> frozenset[Identity] | None:
    return frozenset()


class Txn:
    def __init__(self, reg: dict[str, Any], presence: PresenceProbe, clock: Clock) -> None:
        self.reg = reg
        self._index: registry.SessionIndex | None = None
        self._presence = presence
        self._clock = clock
        self._reading: tuple[float, frozenset[Identity] | None] | None = None

    @property
    def index(self) -> registry.SessionIndex:
        if self._index is None:
            self._index = registry.SessionIndex(self.reg)
        return self._index

    def live(self) -> frozenset[Identity] | None:
        now = self._clock()
        if self._reading is None or now - self._reading[0] > PRESENCE_MAX_AGE:
            self._reading = (now, self._presence())
        return self._reading[1]


type Act[I, R] = Callable[[Txn, I], R]


@dataclass(frozen=True)
class Applied[I, R]:
    ran: tuple[tuple[I, R | Exception], ...]
    stopped: BaseException | None


@dataclass(frozen=True)
class BatchRun[I, R]:
    ran: tuple[tuple[I, R | Exception], ...]
    unreached: tuple[I, ...]
    error: RuntimeError | OSError | None
    committed: registry.Registry | None

    def settled(
        self,
        *,
        wrote: Callable[[R], bool],
        unrecorded: Callable[[I, R, Exception], R],
        failed: Callable[[I, Exception], R],
    ) -> list[tuple[I, R | Exception]]:
        error = self.error
        if error is None:
            return list(self.ran)
        return [
            (item, result)
            if isinstance(result, Exception) or not wrote(result)
            else (item, unrecorded(item, result, error))
            for item, result in self.ran
        ] + [(item, failed(item, error)) for item in self.unreached]


def apply_batch[I, R](
    reg: dict[str, Any],
    items: Sequence[I],
    act: Act[I, R],
    *,
    presence: PresenceProbe = no_presence,
    cancelled: CancelCheck = never_cancelled,
    clock: Clock = time.monotonic,
) -> Applied[I, R]:
    txn = Txn(reg, presence, clock)
    ran: list[tuple[I, R | Exception]] = []
    for item in items:
        if cancelled():
            break
        try:
            ran.append((item, act(txn, item)))
        except (KeyboardInterrupt, SystemExit) as stop:
            return Applied(tuple(ran), stop)
        except Exception as error:  # noqa: BLE001 -- earlier items in the batch still need recording
            ran.append((item, error))
    return Applied(tuple(ran), None)


def run_batch[I, R](  # noqa: PLR0913 -- one transaction's shared state, passed down
    registry_path: str | Path,
    items: Sequence[I],
    act: Act[I, R],
    *,
    presence: PresenceProbe = no_presence,
    cancelled: CancelCheck = never_cancelled,
    publish: bool = False,
    clock: Clock = time.monotonic,
) -> BatchRun[I, R]:
    applied: list[Applied[I, R]] = []

    def apply(reg: dict[str, Any]) -> None:
        applied.append(
            apply_batch(reg, items, act, presence=presence, cancelled=cancelled, clock=clock)
        )

    committed: registry.Registry | None = None
    error: RuntimeError | OSError | None = None
    try:
        with ExitStack() as stack:
            if publish:
                stack.enter_context(stateio.publish_lock())
            committed = registry.mutate_registry(registry_path, apply)
    except (RuntimeError, OSError) as failure:
        error = failure
    if applied and applied[0].stopped is not None:
        raise applied[0].stopped
    ran = applied[0].ran if applied else ()
    return BatchRun(ran, () if applied else tuple(items), error, committed)
