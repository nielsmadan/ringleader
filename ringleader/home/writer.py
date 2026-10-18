from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .. import paths, prune, registry
from ..decisions import (
    ClassifyOp,
    Executor,
    GroupResult,
    Op,
    OpContext,
    Outcome,
    ReloadOp,
    StampRoundupOp,
    group_key,
    is_delete,
)
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
type Execute = Callable[[Op, OpContext], Outcome] | Executor

GROUP_LIMIT = prune.DELETE_BATCH
ERROR_LOG_LIMIT = 1_000_000
type MakeContext = Callable[[CancelCheck, Callable[[RoundupProgress], None]], OpContext]
type _Result = tuple[Outcome, BaseException | None]


def _pairs(entries: Sequence[_Entry]) -> tuple[tuple[int, Op], ...]:
    return tuple((entry.ticket, entry.op) for entry in entries)


def _internal_outcome(error: BaseException) -> Outcome:
    return Outcome(
        ok=False, message=f"internal error: {type(error).__name__}: {error}", wrote=False
    )


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
        executor: Executor,
        make_context: MakeContext,
        deliver: Callable[[WriterEvent], None],
        delete_grace: float = DELETE_GRACE_SECONDS,
        read_only: bool = False,
        clock: Callable[[], float] = time.monotonic,
        error_log: Path | None = None,
    ) -> None:
        self._registry_path = registry_path
        self._executor = executor
        self._make_context = make_context
        self._deliver = deliver
        self._delete_grace = delete_grace
        self._read_only = read_only
        self._clock = clock
        self._error_log = error_log
        self._cond = threading.Condition()
        self._queue: list[_Entry] = []
        self._current: tuple[_Entry, ...] = ()
        self._busy = False
        self._cancel_requested = False
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
        return Batch(_pairs(entries), label)

    def _withdraw(self, keep: Callable[[_Entry], bool]) -> tuple[_Entry, ...]:
        with self._cond:
            dropped = tuple(entry for entry in self._queue if not keep(entry))
            self._queue = [entry for entry in self._queue if keep(entry)]
            emptied = bool(dropped) and not self._busy and not self._queue
            self._cond.notify_all()
        if emptied:
            self._deliver(Drained())
        return dropped

    def undo_last(self) -> tuple[tuple[int, Op], ...]:
        with self._cond:
            newest = max(
                (
                    entry.batch
                    for entry in self._queue
                    if not isinstance(entry.op, _UNDOABLE_EXCLUDED)
                ),
                default=None,
            )
        if newest is None:
            return ()
        return _pairs(self._withdraw(lambda entry: entry.batch != newest))

    def flush_grace(self) -> None:
        with self._cond:
            now = self._clock()
            self._queue = [
                replace(entry, not_before=min(entry.not_before, now)) for entry in self._queue
            ]
            self._cond.notify_all()

    def discard_pending(self) -> tuple[tuple[int, Op], ...]:
        return _pairs(self._withdraw(lambda entry: False))

    def cancel_current(self) -> None:
        with self._cond:
            if self._current:
                self._cancel_requested = True

    def cancel(self, ticket: int) -> Literal["running", "dropped", "gone"]:
        with self._cond:
            if self._current and self._current[-1].ticket == ticket:
                self._cancel_requested = True
                return "running"
            if any(entry.ticket == ticket for entry in self._current):
                return "running"
        if self._withdraw(lambda entry: entry.ticket != ticket):
            return "dropped"
        return "gone"

    def join_current(self, timeout: float | None = None) -> bool:
        with self._cond:
            return self._cond.wait_for(
                lambda: not self._busy and not self._ready(self._clock()), timeout
            )

    @property
    def idle(self) -> bool:
        with self._cond:
            return not self._queue and not self._busy

    @property
    def waiting(self) -> int:
        with self._cond:
            return len(self._queue)

    @property
    def unfinished(self) -> int:
        with self._cond:
            return len(self._queue) + len(self._current)

    @property
    def generation(self) -> int:
        with self._cond:
            return self._generation

    def _ready(self, now: float) -> list[_Entry]:
        return [entry for entry in self._queue if entry.not_before <= now]

    @staticmethod
    def _group(ready: Sequence[_Entry]) -> tuple[_Entry, ...]:
        key = group_key(ready[0].op)
        if key is None:
            return (ready[0],)
        run = next(
            (index for index, entry in enumerate(ready) if group_key(entry.op) != key),
            len(ready),
        )
        return tuple(ready[: min(run, GROUP_LIMIT)])

    def _take(self) -> tuple[_Entry, ...]:
        with self._cond:
            while True:
                now = self._clock()
                ready = self._ready(now)
                if ready:
                    self._busy = True
                    return self._group(ready)
                due = min((queued.not_before for queued in self._queue), default=None)
                self._cond.wait(None if due is None else due - now)

    def _claim(self, entry: _Entry) -> bool:
        with self._cond:
            if not any(queued.ticket == entry.ticket for queued in self._queue):
                return False
            self._queue = [queued for queued in self._queue if queued.ticket != entry.ticket]
            self._current = (*self._current, entry)
            self._cancel_requested = False
        self._deliver(Started(entry.ticket, entry.op))
        return True

    def _cancelled(self) -> bool:
        with self._cond:
            return self._cancel_requested

    def _run(self) -> None:
        while True:
            group = self._take()
            try:
                results = self._results(group)
            except Exception as error:  # noqa: BLE001 -- the writer thread must survive
                self._log(group[0], traceback.format_exc())
                results = [(_internal_outcome(error), error)] * len(group)
            finished = self._finish(group, results)
            with self._cond:
                self._current = ()
                self._busy = False
                drained = not self._queue
                self._cond.notify_all()
            for event in finished:
                self._deliver_quietly(group[0], event)
            if drained:
                self._deliver_quietly(group[0], Drained())

    def _deliver_quietly(self, entry: _Entry, event: WriterEvent) -> None:
        try:
            self._deliver(event)
        except Exception:  # noqa: BLE001 -- a failing consumer must not stop the writer
            self._log(entry, traceback.format_exc())

    def _context(self) -> OpContext:
        def progress(update: RoundupProgress) -> None:
            with self._cond:
                ticket = self._current[-1].ticket
            self._deliver(OpProgress(ticket, update))

        return self._make_context(self._cancelled, progress)

    def _results(self, group: Sequence[_Entry]) -> list[_Result | None]:
        context = self._context()
        if group_key(group[0].op) is None:
            [entry] = group
            if not self._claim(entry):
                return [None]
            try:
                return [(self._executor.execute(entry.op, context), None)]
            except Exception as error:  # noqa: BLE001 -- an op failure must not kill the writer
                self._log(entry, traceback.format_exc())
                return [(_internal_outcome(error), error)]
        results = list(
            self._executor.execute_group(
                [entry.op for entry in group], context, lambda index: self._claim(group[index])
            )
        )
        if len(results) != len(group):
            raise RuntimeError(f"{len(results)} results for {len(group)} ops")
        return [self._result(entry, result) for entry, result in zip(group, results, strict=True)]

    def _result(self, entry: _Entry, result: GroupResult | None) -> _Result | None:
        if result is None or isinstance(result, Outcome):
            return None if result is None else (result, None)
        self._log(entry, "".join(traceback.format_exception(result)))
        return _internal_outcome(result), result

    def _finish(
        self, group: Sequence[_Entry], results: Sequence[_Result | None]
    ) -> list[WriterEvent]:
        with self._cond:
            claimed = {entry.ticket for entry in self._current}
            queued = {entry.ticket for entry in self._queue}
            unclaimed = {entry.ticket for entry in group if entry.ticket in queued}
            self._queue = [entry for entry in self._queue if entry.ticket not in unclaimed]
            done = [
                (entry, result if result is not None else self._not_run(entry))
                for entry, result in zip(group, results, strict=True)
                if entry.ticket in claimed | unclaimed
            ]
            generations = []
            for _, (outcome, _) in done:
                self._generation += outcome.wrote
                generations.append(self._generation)
            reload = any(isinstance(entry.op, ReloadOp) for entry, _ in done) or not self._ready(
                self._clock()
            )
        if not done:
            return []
        snapshot = self._snapshot(done[-1][0], generations[-1]) if reload else None
        last = len(done) - 1
        return [
            *(Started(entry.ticket, entry.op) for entry, _ in done if entry.ticket in unclaimed),
            *(
                Finished(
                    entry.ticket,
                    entry.op,
                    outcome,
                    generation,
                    snapshot if index == last else None,
                    internal,
                )
                for index, ((entry, (outcome, internal)), generation) in enumerate(
                    zip(done, generations, strict=True)
                )
            ),
        ]

    def _not_run(self, entry: _Entry) -> _Result:
        error = RuntimeError("the executor neither ran this op nor reported why")
        self._log(entry, f"{error}\n")
        return _internal_outcome(error), error

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
            if path.exists() and path.stat().st_size >= ERROR_LOG_LIMIT:
                path.replace(path.with_name(path.name + ".1"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{stamp} {type(entry.op).__name__} ({entry.label})\n{trace}\n")
        except OSError:
            return
