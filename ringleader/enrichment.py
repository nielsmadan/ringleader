from __future__ import annotations

import contextlib
import math
import queue
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from . import export, registry
from .models import with_cancel
from .models.base import Model, ModelError
from .progress import CancelCheck

type Identity = tuple[str, str]

STATUSES: tuple[str, ...] = ("active", "unfinished", "blocked", "finished")
ENRICH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": list(STATUSES)},
        "whatItIs": {"type": "string"},
        "suggestedName": {"type": "string"},
    },
    "required": ["status", "whatItIs", "suggestedName"],
}
TAIL_CHARS: int = 6000
ESTIMATED_SECONDS_PER_CALL: dict[str, float] = {"claude": 8.0, "codex": 15.0, "llm": 5.0}
BATCH_SIZE = 10
BREAKER_FAILURES = 5
_BREAKER_PREFIX = 60
_DRAIN_POLL_SECONDS = 0.5


@dataclass(frozen=True)
class Classification:
    status: str
    what_it_is: str
    suggested_name: str


@dataclass(frozen=True)
class Classified:
    identity: Identity
    observed_mtime: str
    classification: Classification


@dataclass(frozen=True)
class ApplyCounts:
    applied: int
    stale: int


@dataclass(frozen=True)
class EnrichOutcome:
    classified: int
    failed: int
    stale: int
    cancelled: bool
    aborted: str | None
    errors: tuple[str, ...]


type ApplyBatch = Callable[[tuple[Classified, ...]], ApplyCounts | None]


@dataclass(frozen=True)
class _Interrupted:
    pass


type _Outcome = Classified | BaseException | _Interrupted


def session_tail(thread: dict[str, Any], max_chars: int = TAIL_CHARS) -> str:
    return export.render_tail(thread, max_chars)


def enrich_prompt(thread: dict[str, Any], tail: str) -> str:
    return (
        "You are classifying a DORMANT coding-agent session for a curation tool.\n"
        f"project: {thread.get('project')}\n"
        f"git branch: {thread.get('gitBranch')}\n"
        f"last prompt: {thread.get('lastPrompt')}\n"
        f"message count: {thread.get('messageCount')}\n\n"
        "Recent transcript tail:\n"
        f"{tail}\n\n"
        "Return: status (active|unfinished|blocked|finished), whatItIs (one line), "
        "suggestedName (short kebab-case)."
    )


def _text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    return value.strip() if isinstance(value, str) else ""


def parse_classification(raw: dict[str, Any]) -> Classification:
    status, what_it_is, suggested_name = (
        _text(raw, key) for key in ("status", "whatItIs", "suggestedName")
    )
    if status not in STATUSES:
        raise ModelError(f"unknown status {raw.get('status')!r}")
    if not what_it_is or not suggested_name:
        raise ModelError("empty whatItIs or suggestedName")
    return Classification(status, what_it_is, suggested_name)


def classify(model: Model, thread: dict[str, Any], tail: str) -> Classification:
    return parse_classification(model.complete_json(enrich_prompt(thread, tail), ENRICH_SCHEMA))


def _fresh_thread(reg: dict[str, Any], result: Classified) -> dict[str, Any] | None:
    thread = _lookup(reg, result.identity)
    if (
        thread is None
        or thread.get("mtime") != result.observed_mtime
        or not registry.needs_classification(thread)
    ):
        return None
    return thread


def apply_classifications(reg: dict[str, Any], results: Sequence[Classified]) -> ApplyCounts:
    applied = 0
    for result in results:
        thread = _fresh_thread(reg, result)
        if thread is None:
            continue
        thread["status"] = result.classification.status
        thread["whatItIs"] = result.classification.what_it_is
        thread["suggestedName"] = result.classification.suggested_name
        thread["classifiedAtMtime"] = result.observed_mtime
        applied += 1
    return ApplyCounts(applied=applied, stale=len(results) - applied)


def apply_batch(registry_path: str, results: Sequence[Classified]) -> ApplyCounts:
    counts: list[ApplyCounts] = []
    registry.mutate_registry(
        registry_path, lambda reg: counts.append(apply_classifications(reg, results))
    )
    return counts[0]


def default_concurrency(model: Model) -> int:
    return 1 if model.name == "llm" else 4


def estimate_seconds(count: int, engine: str | None, concurrency: int) -> float:
    per_call = ESTIMATED_SECONDS_PER_CALL.get(
        engine or "", max(ESTIMATED_SECONDS_PER_CALL.values())
    )
    return math.ceil(count / max(concurrency, 1)) * per_call


def _lookup(reg: dict[str, Any], identity: Identity) -> dict[str, Any] | None:
    try:
        return registry.find_session(reg, ":".join(identity))
    except registry.AmbiguousSession:
        return None


class _Breaker:
    def __init__(self) -> None:
        self.prefix: str | None = None
        self.streak = 0
        self.tripped = threading.Event()
        self.reason: str | None = None
        self._lock = threading.Lock()

    def record(self, outcome: _Outcome) -> None:
        if isinstance(outcome, _Interrupted):
            return
        with self._lock:
            if isinstance(outcome, Classified):
                self.prefix, self.streak = None, 0
                return
            prefix = str(outcome)[:_BREAKER_PREFIX]
            self.streak = self.streak + 1 if prefix == self.prefix else 1
            self.prefix = prefix
            if self.streak >= BREAKER_FAILURES and not self.tripped.is_set():
                self.reason = f"{self.streak} calls failed in a row: {outcome}"
                self.tripped.set()


def _attempt(
    model: Model,
    stop: Callable[[], bool],
    tail_for: Callable[[dict[str, Any]], str],
    breaker: _Breaker,
    identity: Identity,
    thread: dict[str, Any],
) -> _Outcome:
    outcome: _Outcome = _Interrupted()
    judged = False
    try:
        tail = tail_for(thread)
        judged = True
        found = classify(with_cancel(model, stop), thread, tail)
        outcome = Classified(identity, thread["mtime"], found)
    except BaseException as error:  # noqa: BLE001 -- every taken id reports one result
        outcome = _Interrupted() if stop() else error
    finally:
        if judged:
            breaker.record(outcome)
    return outcome


def enrich_sessions(  # noqa: PLR0913 -- injection points the suite drives
    registry_path: str,
    ids: Sequence[Identity],
    *,
    model: Model,
    concurrency: int,
    progress: Callable[[int, int], None],
    cancelled: CancelCheck,
    apply: ApplyBatch | None = None,
    tail_for: Callable[[dict[str, Any]], str] = session_tail,
) -> EnrichOutcome:
    commit: ApplyBatch = apply or (lambda batch: apply_batch(registry_path, batch))
    total = len(ids)
    if not total:
        return EnrichOutcome(0, 0, 0, cancelled=False, aborted=None, errors=())
    reg = registry.load_registry(registry_path)
    resolved = [(identity, _lookup(reg, identity)) for identity in ids]
    errors = [f"{identity[1]}: no longer in the registry" for identity, t in resolved if t is None]
    jobs: queue.SimpleQueue[tuple[Identity, dict[str, Any]]] = queue.SimpleQueue()
    for identity, thread in resolved:
        if thread is not None:
            jobs.put((identity, thread))
    results: queue.SimpleQueue[tuple[Identity, _Outcome]] = queue.SimpleQueue()
    breaker = _Breaker()
    abort = threading.Event()

    def stop() -> bool:
        return breaker.tripped.is_set() or abort.is_set() or cancelled()

    def work() -> None:
        while not stop():
            try:
                identity, thread = jobs.get_nowait()
            except queue.Empty:
                return
            results.put((identity, _attempt(model, stop, tail_for, breaker, identity, thread)))

    workers = [
        threading.Thread(target=work, name="enrich", daemon=True)
        for _ in range(max(1, min(concurrency, total - len(errors))))
    ]
    for worker in workers:
        worker.start()

    done = len(errors)
    since_flush = 0
    batch: list[Classified] = []
    classified = stale = 0

    def flush() -> None:
        nonlocal classified, stale, batch
        if batch:
            counts = commit(tuple(batch))
            if counts is not None:
                classified += counts.applied
                stale += counts.stale
            batch = []
        progress(done, total)

    try:
        while any(worker.is_alive() for worker in workers) or not results.empty():
            try:
                identity, outcome = results.get(timeout=_DRAIN_POLL_SECONDS)
            except queue.Empty:
                continue
            if isinstance(outcome, _Interrupted):
                continue
            done += 1
            since_flush += 1
            if isinstance(outcome, Classified):
                batch.append(outcome)
            else:
                errors.append(f"{identity[1]}: {outcome}")
            if since_flush >= BATCH_SIZE:
                since_flush = 0
                flush()
        flush()
    except BaseException:
        abort.set()
        if batch:
            with contextlib.suppress(Exception):
                commit(tuple(batch))
        raise
    return EnrichOutcome(
        classified=classified,
        failed=len(errors),
        stale=stale,
        cancelled=breaker.reason is None and done < total and cancelled(),
        aborted=breaker.reason,
        errors=tuple(errors),
    )
