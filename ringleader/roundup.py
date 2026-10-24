from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import enrichment, lifecycle, overview, paths, prune, registry, stateio
from .batch import Txn, run_batch
from .enrichment import EnrichOutcome
from .models import describe
from .models.base import Model
from .progress import CancelCheck
from .revalidate import (
    Identity,
    PresenceProbe,
    Seen,
    UnknownPresence,
    quiet_reason,
    running_reason,
    seen,
)

type EnrichScope = Literal["all", "recent", "none"]
type RoundupPhase = Literal["sweep", "prune", "purge", "enrich", "stamp"]
type RoundupStep = Literal["enrich", "stamp", "cancelled"]

ENRICH_WARN_THRESHOLD = 25
RECENT_DAYS = 7
PRUNE_BATCH = prune.DELETE_BATCH

_HOURS_PER_DAY = 24
_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60

ROUNDUP_BUSY = "another roundup is running"


class RoundupBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class RoundupPlan:
    registry_path: str
    now_iso: str
    scan_note: str | None
    sweep_items: tuple[Seen, ...]
    prune_items: tuple[Seen, ...]
    purge_ids: tuple[Identity, ...]
    enrich_all_ids: tuple[Identity, ...]
    enrich_recent_ids: tuple[Identity, ...]
    engine: str | None
    model_label: str | None
    concurrency: int
    retention_days: int = 0
    read_only: bool = False
    handoffs_dir: str | None = None
    handoff_paths: tuple[str, ...] = ()

    @property
    def enrich_warning(self) -> bool:
        return self.engine is not None and len(self.enrich_all_ids) > ENRICH_WARN_THRESHOLD

    def enrich_ids(self, scope: EnrichScope) -> tuple[Identity, ...]:
        return {"all": self.enrich_all_ids, "recent": self.enrich_recent_ids, "none": ()}[scope]

    def estimated_seconds(self, scope: EnrichScope) -> float:
        return enrichment.estimate_seconds(
            len(self.enrich_ids(scope)), self.engine, self.concurrency
        )


@dataclass(frozen=True)
class RoundupProgress:
    phase: RoundupPhase
    done: int
    total: int
    detail: str | None = None


@dataclass(frozen=True)
class PhasesResult:
    swept: int
    deleted: int
    purged: int
    skipped: tuple[tuple[Seen, str], ...]
    errors: int
    cancelled: bool
    warnings: tuple[str, ...]
    handoffs_purged: int = 0


@dataclass(frozen=True)
class RoundupResult:
    phases: PhasesResult
    enrich: EnrichOutcome | None
    stamped: bool


def is_stale(reg: dict[str, Any], now_iso: str, stale_hours: int) -> bool:
    last = reg.get("lastRoundupAt")
    if not isinstance(last, str) or not last:
        return True
    try:
        return overview.age_days(last, now_iso) * _HOURS_PER_DAY >= stale_hours
    except ValueError:
        return True


def _identities(threads: Sequence[dict[str, Any]]) -> set[Identity]:
    return {registry.identity(t) for t in threads}


def plan_roundup(  # noqa: PLR0913 -- injection points the suite drives
    registry_path: str,
    reg: dict[str, Any],
    now_iso: str,
    *,
    scan_note: str | None,
    model: Model | None,
    now_epoch: float,
    retention_days: int,
    concurrency: int | None = None,
    unavailable_sources: frozenset[str] = frozenset(),
    read_only_model: Model | None = None,
    handoffs_dir: Path | None = None,
) -> RoundupPlan:
    handoffs = handoffs_dir if handoffs_dir is not None else paths.handoffs_dir()
    sweeping = lifecycle.due(reg, now_epoch)
    swept_ids = _identities(sweeping)
    pruning = [
        t for t in prune.prune_candidates(reg, now_iso) if registry.identity(t) not in swept_ids
    ]
    excluded = swept_ids | _identities(pruning)
    pending = sorted(
        (
            t
            for t in registry.sessions_needing_classification(reg)
            if registry.identity(t) not in excluded
            and registry.identity(t)[0] not in unavailable_sources
        ),
        key=lambda t: t.get("mtime") or "",
        reverse=True,
    )
    purging = prune.archive_purge_candidates(reg, now_iso, retention_days=retention_days)
    return RoundupPlan(
        registry_path=registry_path,
        now_iso=now_iso,
        scan_note=scan_note,
        sweep_items=tuple(seen(t) for t in sweeping),
        prune_items=tuple(seen(t) for t in pruning),
        purge_ids=tuple(registry.identity(ts) for ts in purging),
        enrich_all_ids=tuple(registry.identity(t) for t in pending),
        enrich_recent_ids=tuple(
            registry.identity(t)
            for t in pending
            if t.get("mtime") and overview.age_days(t["mtime"], now_iso) <= RECENT_DAYS
        ),
        engine=model.name if model is not None else None,
        model_label=describe(shown) if (shown := model or read_only_model) is not None else None,
        concurrency=(concurrency or enrichment.default_concurrency(model)) if model else 1,
        retention_days=retention_days,
        read_only=read_only_model is not None and model is None,
        handoffs_dir=str(handoffs),
        handoff_paths=tuple(
            str(path)
            for path in prune.handoff_purge_candidates(
                handoffs, now_epoch, retention_days=retention_days
            )
        ),
    )


def _still_planned(thread: dict[str, Any], item: Seen) -> bool:
    return (thread.get("mtime") or "") == item.mtime and thread.get(
        "fileFingerprint"
    ) == item.fingerprint


def _running_guard(unknown: UnknownPresence) -> prune.Guard:
    return lambda thread, live: running_reason(thread, live(), unknown)


def _collect_skips(
    found: dict[Identity, Seen], out: list[tuple[Seen, str]]
) -> Callable[[dict[str, Any], str], None]:
    def collect(thread: dict[str, Any], reason: str) -> None:
        out.append((found[registry.identity(thread)], reason))

    return collect


def _revalidate_batch(
    registry_path: str, batch: Sequence[Seen], now_iso: str
) -> tuple[registry.Registry, list[tuple[Seen, dict[str, Any]]], list[tuple[Seen, str]]]:
    loaded = registry.load_registry(registry_path)
    assert isinstance(loaded, registry.Registry)
    current = {registry.identity(t): t for t in prune.prune_candidates(loaded, now_iso)}
    planned = [(item, current.get(item.identity)) for item in batch]
    fresh = [(item, thread) for item, thread in planned if thread and _still_planned(thread, item)]
    kept = {item.identity for item, _ in fresh}
    changed = [(item, "changed since the plan") for item in batch if item.identity not in kept]
    return loaded, fresh, changed


def prune_planned(  # noqa: PLR0913 -- injection points the suite drives
    registry_path: str,
    items: Sequence[Seen],
    now_iso: str,
    *,
    presence: PresenceProbe,
    unknown: UnknownPresence,
    cancelled: CancelCheck,
    progress: Callable[[int, int], None],
    report: Callable[[str], None],
) -> tuple[int, tuple[tuple[Seen, str], ...]]:
    deleted = done = 0
    skipped: list[tuple[Seen, str]] = []
    for start in range(0, len(items), PRUNE_BATCH):
        if cancelled():
            break
        batch = items[start : start + PRUNE_BATCH]
        loaded, fresh, changed = _revalidate_batch(registry_path, batch, now_iso)
        skipped += changed
        deleted += prune.delete_sessions(
            loaded,
            [thread for _, thread in fresh],
            now_iso,
            report=report,
            guard=_running_guard(unknown),
            presence=presence,
            batch_size=PRUNE_BATCH,
            on_skip=_collect_skips({item.identity: item for item, _ in fresh}, skipped),
        )
        done += len(batch)
        progress(done, len(items))
    return deleted, tuple(skipped)


@dataclass(frozen=True)
class _SweepOutcome:
    swept: bool
    skipped: str | None = None
    error: str | None = None
    warnings: tuple[str, ...] = ()


def _sweep_item(
    txn: Txn,
    item: Seen,
    *,
    unknown: UnknownPresence,
    archive_root_path: str,
    now_epoch: float,
    now_iso: str,
) -> _SweepOutcome:
    try:
        thread = txn.index.find(":".join(item.identity))
    except registry.AmbiguousSession:
        thread = None
    if thread is None:
        return _SweepOutcome(False, skipped="no longer in the registry")
    if not thread.get("finishOnExit"):
        return _SweepOutcome(False, skipped="no longer marked finished")
    reason = quiet_reason(thread, now_epoch, lifecycle.GRACE_SECONDS) or running_reason(
        thread, txn.live(), unknown
    )
    if reason:
        return _SweepOutcome(False, skipped=reason)
    try:
        result = lifecycle.archive_thread(
            txn.reg, thread, archive_root_path=archive_root_path, now_iso=now_iso, index=txn.index
        )
    except (RuntimeError, OSError) as error:
        return _SweepOutcome(False, error=f"sweep {item.label}: {error}")
    return _SweepOutcome(True, warnings=result.warnings)


def _sweep_unrecorded(item: Seen, outcome: _SweepOutcome, error: Exception) -> _SweepOutcome:
    return _SweepOutcome(
        False,
        error=f"sweep {item.label}: archived on disk, but the registry was not updated: {error}",
        warnings=outcome.warnings,
    )


def _sweep_failed(item: Seen, error: Exception) -> _SweepOutcome:
    return _SweepOutcome(False, error=f"sweep {item.label}: {error}")


def _sweep_batch(  # noqa: PLR0913 -- injection points the suite drives
    registry_path: str,
    batch: Sequence[Seen],
    *,
    presence: PresenceProbe,
    unknown: UnknownPresence,
    archive_root_path: str,
    now_epoch: Callable[[], float],
    now_iso: str,
    cancelled: CancelCheck,
) -> list[tuple[Seen, _SweepOutcome]]:
    def sweep(txn: Txn, item: Seen) -> _SweepOutcome:
        return _sweep_item(
            txn,
            item,
            unknown=unknown,
            archive_root_path=archive_root_path,
            now_epoch=now_epoch(),
            now_iso=now_iso,
        )

    run = run_batch(
        registry_path, batch, sweep, presence=presence, cancelled=cancelled, publish=True
    )
    settled = run.settled(
        wrote=lambda outcome: outcome.swept, unrecorded=_sweep_unrecorded, failed=_sweep_failed
    )
    return [
        (item, _sweep_failed(item, result) if isinstance(result, Exception) else result)
        for item, result in settled
    ]


def _purge(plan: RoundupPlan, archive_root_path: str, report: Callable[[str], None]) -> int:
    reg = registry.load_registry(plan.registry_path)
    wanted = set(plan.purge_ids)
    candidates = [
        ts
        for ts in prune.archive_purge_candidates(
            reg, plan.now_iso, retention_days=plan.retention_days
        )
        if registry.identity(ts) in wanted
    ]
    purged = prune.purge_archived(
        reg, candidates, plan.now_iso, archive_root_path=archive_root_path, report=report
    )
    if candidates:
        registry.commit_registry(plan.registry_path, reg)
    return purged


def _run_locked(  # noqa: PLR0913 -- injection points the suite drives
    plan: RoundupPlan,
    *,
    presence: PresenceProbe,
    unknown: UnknownPresence,
    progress: Callable[[RoundupProgress], None],
    cancelled: CancelCheck,
    archive_root_path: str,
    now_epoch: Callable[[], float],
) -> PhasesResult:
    warnings: list[str] = []
    skipped: list[tuple[Seen, str]] = []
    swept = deleted = purged = errors = 0

    sweep_total = len(plan.sweep_items)
    progress(RoundupProgress("sweep", 0, sweep_total))
    done = 0
    for start in range(0, sweep_total, PRUNE_BATCH):
        if cancelled():
            break
        for item, outcome in _sweep_batch(
            plan.registry_path,
            plan.sweep_items[start : start + PRUNE_BATCH],
            presence=presence,
            unknown=unknown,
            archive_root_path=archive_root_path,
            now_epoch=now_epoch,
            now_iso=plan.now_iso,
            cancelled=cancelled,
        ):
            swept += outcome.swept
            warnings.extend(outcome.warnings)
            if outcome.skipped is not None:
                skipped.append((item, outcome.skipped))
            if outcome.error is not None:
                errors += 1
                warnings.append(outcome.error)
            done += 1
            progress(RoundupProgress("sweep", done, sweep_total, item.label))
    progress(RoundupProgress("sweep", sweep_total, sweep_total, "done"))

    prune_total = len(plan.prune_items)
    if not cancelled():
        progress(RoundupProgress("prune", 0, prune_total))
        try:
            deleted, prune_skipped = prune_planned(
                plan.registry_path,
                plan.prune_items,
                plan.now_iso,
                presence=presence,
                unknown=unknown,
                cancelled=cancelled,
                progress=lambda done, total: progress(RoundupProgress("prune", done, total)),
                report=warnings.append,
            )
            skipped += prune_skipped
        except (RuntimeError, OSError) as error:
            errors += 1
            warnings.append(f"prune: {error}")
        progress(RoundupProgress("prune", prune_total, prune_total, "done"))

    purge_total = len(plan.purge_ids)
    if not cancelled():
        progress(RoundupProgress("purge", 0, purge_total))
        try:
            purged = _purge(plan, archive_root_path, warnings.append) if purge_total else 0
        except (RuntimeError, OSError) as error:
            errors += 1
            warnings.append(f"purge: {error}")
        progress(RoundupProgress("purge", purged, purge_total, "done"))

    handoffs_purged = 0
    if plan.handoff_paths and plan.handoffs_dir is not None and not cancelled():
        handoffs_purged = prune.purge_handoffs(
            Path(plan.handoffs_dir),
            [Path(path) for path in plan.handoff_paths],
            now_epoch(),
            retention_days=plan.retention_days,
            report=warnings.append,
        )

    return PhasesResult(
        swept=swept,
        deleted=deleted,
        purged=purged,
        skipped=tuple(skipped),
        errors=errors,
        cancelled=cancelled(),
        warnings=tuple(warnings),
        handoffs_purged=handoffs_purged,
    )


def next_step(
    finished: Literal["phases", "enrich"], *, cancelled: bool, can_enrich: bool = False
) -> RoundupStep:
    if cancelled:
        return "cancelled"
    return "enrich" if finished == "phases" and can_enrich else "stamp"


def hold_lock() -> ExitStack | None:
    stack = ExitStack()
    if stack.enter_context(stateio.roundup_lock()):
        return stack
    stack.close()
    return None


@contextmanager
def _exclusive() -> Iterator[None]:
    held = hold_lock()
    if held is None:
        raise RoundupBusy(ROUNDUP_BUSY)
    with held:
        yield


def run_phases(  # noqa: PLR0913 -- injection points the suite drives
    plan: RoundupPlan,
    *,
    presence: PresenceProbe,
    unknown: UnknownPresence,
    progress: Callable[[RoundupProgress], None],
    cancelled: CancelCheck,
    archive_root_path: str,
    now_epoch: Callable[[], float],
) -> PhasesResult:
    with _exclusive():
        return _run_locked(
            plan,
            presence=presence,
            unknown=unknown,
            progress=progress,
            cancelled=cancelled,
            archive_root_path=archive_root_path,
            now_epoch=now_epoch,
        )


def _set_last_roundup(reg: dict[str, Any], now_iso: str) -> None:
    reg["lastRoundupAt"] = now_iso


def stamp(registry_path: str, now_iso: str) -> None:
    registry.mutate_registry(registry_path, lambda reg: _set_last_roundup(reg, now_iso))


def run_roundup(  # noqa: PLR0913 -- injection points the suite drives
    plan: RoundupPlan,
    scope: EnrichScope,
    *,
    model: Model | None,
    presence: PresenceProbe,
    unknown: UnknownPresence,
    progress: Callable[[RoundupProgress], None],
    cancelled: CancelCheck,
    archive_root_path: str,
) -> RoundupResult:
    with _exclusive():
        phases = _run_locked(
            plan,
            presence=presence,
            unknown=unknown,
            progress=progress,
            cancelled=cancelled,
            archive_root_path=archive_root_path,
            now_epoch=time.time,
        )
        enriched: EnrichOutcome | None = None
        ids = plan.enrich_ids(scope)
        step = next_step(
            "phases",
            cancelled=phases.cancelled or cancelled(),
            can_enrich=model is not None and bool(ids),
        )
        if step == "enrich" and model is not None:
            progress(RoundupProgress("enrich", 0, len(ids)))
            enriched = enrichment.enrich_sessions(
                plan.registry_path,
                ids,
                model=model,
                concurrency=plan.concurrency,
                progress=lambda done, total: progress(RoundupProgress("enrich", done, total)),
                cancelled=cancelled,
            )
            step = next_step("enrich", cancelled=cancelled())
        if step != "stamp":
            return RoundupResult(phases=phases, enrich=enriched, stamped=False)
        progress(RoundupProgress("stamp", 0, 1))
        stamp(plan.registry_path, plan.now_iso)
        progress(RoundupProgress("stamp", 1, 1))
        return RoundupResult(phases=phases, enrich=enriched, stamped=True)


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def duration(seconds: float) -> str:
    minutes = max(1, round(seconds / _SECONDS_PER_MINUTE)) if seconds > 0 else 0
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes} min"
    hours, rest = divmod(minutes, _MINUTES_PER_HOUR)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def _scan_line(note: str | None) -> str:
    if not note:
        return "scanned — no changes"
    return "scanned — " + note.removeprefix("(scanning… ").removesuffix(")")


def _enrich_line(plan: RoundupPlan) -> str:
    if plan.read_only and plan.model_label is not None:
        return f"enrich: read-only — skipped (would use {plan.model_label})"
    if plan.model_label is None:
        return "enrich: no model configured — skipped (rl config)"
    if not plan.enrich_all_ids:
        return f"enrich: nothing pending ({plan.model_label})"
    line = (
        f"enrich {_plural(len(plan.enrich_all_ids), 'session')} with {plan.model_label}"
        f" — about {duration(plan.estimated_seconds('all'))}"
    )
    if not plan.enrich_warning:
        return line
    return (
        f"{line}; {len(plan.enrich_recent_ids):,} active in the last {RECENT_DAYS} days"
        f" — about {duration(plan.estimated_seconds('recent'))}"
    )


def _purge_line(plan: RoundupPlan) -> str:
    line = f"purge {_plural(len(plan.purge_ids), 'archived session')} past retention"
    if not plan.handoff_paths:
        return line
    return f"{line}, and {_plural(len(plan.handoff_paths), 'handoff export')}"


def describe_plan(plan: RoundupPlan) -> list[str]:
    return [
        _scan_line(plan.scan_note),
        f"archive {_plural(len(plan.sweep_items), 'finished session')} (sweep)",
        (
            f"delete {_plural(len(plan.prune_items), 'tiny session')} older than "
            f"{prune.DEFAULT_OLDER_THAN_DAYS} days (prune)"
        ),
        _purge_line(plan),
        _enrich_line(plan),
    ]
