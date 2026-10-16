from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .. import paths, registry
from ..decisions import ClassifyOp, Op, OpContext, Outcome, ReloadOp, StampRoundupOp, is_delete
from ..progress import CancelCheck
from ..roundup import RoundupProgress
from .policy import DELETE_GRACE_SECONDS


@dataclass(frozen=True)
class Snapshot:
    reg: dict[str, Any]
    generation: int
    scanned: bool
    taken_at: float


@dataclass(frozen=True)
class Refused:
    reason: str


@dataclass(frozen=True)
class Batch:
    tickets: tuple[tuple[int, Op], ...]
    label: str


@dataclass(frozen=True)
class Started:
    ticket: int
    op: Op


@dataclass(frozen=True)
class OpProgress:
    ticket: int
    progress: RoundupProgress


@dataclass(frozen=True)
class Finished:
    ticket: int
    op: Op
    outcome: Outcome
    generation: int
    snapshot: Snapshot | None
    internal: BaseException | None


@dataclass(frozen=True)
class Drained:
    pass


type WriterEvent = Started | OpProgress | Finished | Drained
type Execute = Callable[[Op, OpContext], Outcome]
type MakeContext = Callable[[CancelCheck, Callable[[RoundupProgress], None]], OpContext]


@dataclass(frozen=True)
class _Entry:
    ticket: int
    op: Op
    not_before: float
    batch: int
    label: str


_UNDOABLE_EXCLUDED = (ReloadOp, ClassifyOp, StampRoundupOp)


class Writer:
    def __init__(  # noqa: PLR0913 -- injection points the suite drives
        self,
        registry_path: str,
        *,
        execute: Execute,
        make_context: MakeContext,
        deliver: Callable[[WriterEvent], None],
        delete_grace: float = DELETE_GRACE_SECONDS,
        read_only: bool = False,
        clock: Callable[[], float] = time.monotonic,
        error_log: Path | None = None,
    ) -> None:
        self._registry_path = registry_path
        self._execute = execute
        self._make_context = make_context
        self._deliver = deliver
        self._delete_grace = delete_grace
        self._read_only = read_only
        self._clock = clock
        self._error_log = error_log
        self._cond = threading.Condition()
        self._queue: list[_Entry] = []
        self._current: _Entry | None = None
        self._cancel = threading.Event()
        self._generation = 0
        self._next_ticket = 1
        self._next_batch = 1
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._cond:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="rl-writer", daemon=True)
        self._thread.start()

    def submit(self, ops: Sequence[Op], *, label: str) -> Batch | Refused:
        if self._read_only and not all(isinstance(op, ReloadOp) for op in ops):
            return Refused(f"read-only: {label} is disabled")
        with self._cond:
            now = self._clock()
            entries = [
                _Entry(
                    ticket=self._next_ticket + index,
                    op=op,
                    not_before=now + self._delete_grace if is_delete(op) else now,
                    batch=self._next_batch,
                    label=label,
                )
                for index, op in enumerate(ops)
            ]
            self._next_ticket += len(entries)
            self._next_batch += 1
            self._queue.extend(entries)
            self._cond.notify_all()
        return Batch(tuple((entry.ticket, entry.op) for entry in entries), label)

    def undo_last(self) -> tuple[tuple[int, Op], ...]:
        with self._cond:
            batches = [
                entry.batch for entry in self._queue if not isinstance(entry.op, _UNDOABLE_EXCLUDED)
            ]
            if not batches:
                return ()
            newest = max(batches)
            undone = [entry for entry in self._queue if entry.batch == newest]
            self._queue = [entry for entry in self._queue if entry.batch != newest]
            emptied = self._current is None and not self._queue
            self._cond.notify_all()
        if emptied:
            self._deliver(Drained())
        return tuple((entry.ticket, entry.op) for entry in undone)

    def flush_grace(self) -> None:
        with self._cond:
            now = self._clock()
            self._queue = [
                replace(entry, not_before=min(entry.not_before, now)) for entry in self._queue
            ]
            self._cond.notify_all()

    def discard_pending(self) -> tuple[tuple[int, Op], ...]:
        with self._cond:
            dropped, self._queue = self._queue, []
            emptied = bool(dropped) and self._current is None
            self._cond.notify_all()
        if emptied:
            self._deliver(Drained())
        return tuple((entry.ticket, entry.op) for entry in dropped)

    def cancel_current(self) -> None:
        with self._cond:
            if self._current is not None:
                self._cancel.set()

    def cancel(self, ticket: int) -> Literal["running", "dropped", "gone"]:
        with self._cond:
            if self._current is not None and self._current.ticket == ticket:
                self._cancel.set()
                return "running"
            if not any(entry.ticket == ticket for entry in self._queue):
                return "gone"
            self._queue = [entry for entry in self._queue if entry.ticket != ticket]
            emptied = self._current is None and not self._queue
            self._cond.notify_all()
        if emptied:
            self._deliver(Drained())
        return "dropped"

    def join_current(self, timeout: float | None = None) -> bool:
        with self._cond:
            return self._cond.wait_for(
                lambda: self._current is None and self._ready(self._clock()) is None, timeout
            )

    @property
    def idle(self) -> bool:
        with self._cond:
            return not self._queue and self._current is None

    @property
    def pending(self) -> int:
        with self._cond:
            return len(self._queue) + (self._current is not None)

    @property
    def generation(self) -> int:
        with self._cond:
            return self._generation

    def _ready(self, now: float) -> _Entry | None:
        return next((entry for entry in self._queue if entry.not_before <= now), None)

    def _take(self) -> tuple[_Entry, threading.Event]:
        with self._cond:
            while True:
                now = self._clock()
                entry = self._ready(now)
                if entry is not None:
                    self._queue.remove(entry)
                    self._current = entry
                    self._cancel = threading.Event()
                    return entry, self._cancel
                due = min((queued.not_before for queued in self._queue), default=None)
                self._cond.wait(None if due is None else due - now)

    def _run(self) -> None:
        while True:
            entry, cancel = self._take()
            try:
                self._deliver(Started(entry.ticket, entry.op))
                finished = self._process(entry, cancel)
            except Exception as error:  # noqa: BLE001 -- the writer thread must survive
                self._log(entry, traceback.format_exc())
                finished = Finished(
                    entry.ticket,
                    entry.op,
                    Outcome(
                        ok=False,
                        message=f"internal error: {type(error).__name__}: {error}",
                        wrote=False,
                    ),
                    self.generation,
                    None,
                    error,
                )
            with self._cond:
                self._current = None
                drained = not self._queue
                self._cond.notify_all()
            self._deliver_quietly(entry, finished)
            if drained:
                self._deliver_quietly(entry, Drained())

    def _deliver_quietly(self, entry: _Entry, event: WriterEvent) -> None:
        try:
            self._deliver(event)
        except Exception:  # noqa: BLE001 -- a failing consumer must not stop the writer
            self._log(entry, traceback.format_exc())

    def _process(self, entry: _Entry, cancel: threading.Event) -> Finished:
        def progress(update: RoundupProgress) -> None:
            self._deliver(OpProgress(entry.ticket, update))

        internal: BaseException | None = None
        try:
            outcome = self._execute(entry.op, self._make_context(cancel.is_set, progress))
        except Exception as error:  # noqa: BLE001 -- an op failure must not kill the writer
            internal = error
            outcome = Outcome(
                ok=False, message=f"internal error: {type(error).__name__}: {error}", wrote=False
            )
            self._log(entry, traceback.format_exc())
        with self._cond:
            self._generation += outcome.wrote
            generation = self._generation
            reload = isinstance(entry.op, ReloadOp) or self._ready(self._clock()) is None
        snapshot = self._snapshot(entry, generation) if reload else None
        return Finished(entry.ticket, entry.op, outcome, generation, snapshot, internal)

    def _snapshot(self, entry: _Entry, generation: int) -> Snapshot | None:
        try:
            reg = registry.load_registry(self._registry_path)
        except (OSError, ValueError, KeyError):
            self._log(entry, traceback.format_exc())
            return None
        return Snapshot(reg, generation, scanned=True, taken_at=self._clock())

    def _log(self, entry: _Entry, trace: str) -> None:
        path = self._error_log or paths.state_dir() / "inbox-errors.log"
        stamp = datetime.now(UTC).isoformat()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{stamp} {type(entry.op).__name__} ({entry.label})\n{trace}\n")
        except OSError:
            return
