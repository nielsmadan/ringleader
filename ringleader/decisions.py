from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from . import (
    enrichment,
    export,
    harvesting,
    lifecycle,
    registry,
    revalidate,
    roundup,
    scanner,
    stateio,
)
from .batch import Txn, run_batch
from .codes import OutcomeCode
from .config import load_config
from .enrichment import Classified
from .memory_lifecycle import MemoryLifecycle, MemoryOutcome, RemovalReview, prune_memory_recovery
from .memory_types import MemoryRef
from .models.base import Model, ModelError
from .presence import PresenceUnavailable
from .progress import CancelCheck
from .publish_state import assign_memory_harvest
from .revalidate import Identity, PresenceProbe, Refusal, Seen, UnknownPresence
from .roundup import PhasesResult, RoundupPlan, RoundupProgress

type Decision = Literal["archive", "harvest", "keep_going", "snooze", "keep", "delete"]
type GuardedAction = Decision | Literal["unkeep", "wake"]


@dataclass(frozen=True)
class ArchiveOp:
    seen: Seen


@dataclass(frozen=True)
class DeleteOp:
    seen: Seen


@dataclass(frozen=True)
class KeepGoingOp:
    seen: Seen


@dataclass(frozen=True)
class SnoozeOp:
    seen: Seen
    until: str


@dataclass(frozen=True)
class KeepOp:
    seen: Seen


@dataclass(frozen=True)
class UnkeepOp:
    seen: Seen


@dataclass(frozen=True)
class WakeOp:
    seen: Seen


@dataclass(frozen=True)
class HarvestSaveOp:
    seen: Seen
    markdown: str | None


@dataclass(frozen=True)
class PruneOp:
    items: tuple[Seen, ...]


@dataclass(frozen=True)
class RoundupPhasesOp:
    plan: RoundupPlan


@dataclass(frozen=True)
class ClassifyOp:
    results: tuple[Classified, ...]


@dataclass(frozen=True)
class StampRoundupOp:
    now_iso: str


@dataclass(frozen=True)
class ReloadOp:
    pass


@dataclass(frozen=True)
class MemoryScanOp:
    projected: dict[str, Any]


@dataclass(frozen=True)
class MemoryDecisionOp:
    ref: MemoryRef
    decision: Literal["harvest", "discard"]
    markdown: str | None
    expected_token: str | None
    retention_days: int
    review: RemovalReview | None = None


@dataclass(frozen=True)
class MemoryRecoveryOp:
    operation_id: str
    restore: bool


@dataclass(frozen=True)
class MemoryPruneOp:
    pass


@dataclass(frozen=True)
class MemoryAssignOp:
    token: str
    project: Path


_MEMORY_OPS = (MemoryScanOp, MemoryDecisionOp, MemoryRecoveryOp, MemoryPruneOp, MemoryAssignOp)
type MemoryOp = MemoryScanOp | MemoryDecisionOp | MemoryRecoveryOp | MemoryPruneOp | MemoryAssignOp


type MarkOp = KeepGoingOp | SnoozeOp | KeepOp | UnkeepOp | WakeOp
type SingleOp = ArchiveOp | DeleteOp | MarkOp | HarvestSaveOp
type Op = SingleOp | PruneOp | RoundupPhasesOp | ClassifyOp | StampRoundupOp | ReloadOp | MemoryOp


@dataclass(frozen=True)
class OpContext:
    registry_path: str
    archive_root_path: str
    now_iso: Callable[[], str]
    now_epoch: Callable[[], float]
    presence: PresenceProbe
    unknown: UnknownPresence
    cancelled: CancelCheck
    progress: Callable[[RoundupProgress], None]
    memory: MemoryLifecycle = field(default_factory=MemoryLifecycle)


@dataclass(frozen=True)
class Outcome:
    ok: bool
    message: str
    wrote: bool
    refused: tuple[tuple[Seen, str], ...] = ()
    warnings: tuple[str, ...] = ()
    path: str | None = None
    phases: PhasesResult | None = None
    stale: int = 0
    code: OutcomeCode | None = None


_EXPECTED = (
    KeyError,
    registry.AmbiguousSession,
    RuntimeError,
    OSError,
    registry.RegistryConflict,
    ModelError,
    PresenceUnavailable,
    roundup.RoundupBusy,
)
_BULK_OPS = (PruneOp, RoundupPhasesOp, ClassifyOp, StampRoundupOp, ReloadOp)
_HARVEST_LISTS = ("gotchas", "decisions", "whatChanged")


def identities(op: Op) -> tuple[Identity, ...]:
    match op:
        case (
            ArchiveOp()
            | DeleteOp()
            | KeepGoingOp()
            | SnoozeOp()
            | KeepOp()
            | UnkeepOp()
            | WakeOp()
            | HarvestSaveOp()
        ):
            return (op.seen.identity,)
        case PruneOp():
            return tuple(item.identity for item in op.items)
        case RoundupPhasesOp():
            return tuple(item.identity for item in (*op.plan.sweep_items, *op.plan.prune_items))
        case _:
            return ()


def is_delete(op: Op) -> bool:
    return isinstance(op, (DeleteOp, MemoryDecisionOp, MemoryPruneOp)) or (
        isinstance(op, MemoryRecoveryOp) and not op.restore
    )


def lifecycle_reason(thread: dict[str, Any], decision: Decision) -> str | None:
    if decision not in ("archive", "delete", "harvest"):
        return None
    try:
        scanner.filesystem_path(thread, decision)
    except (RuntimeError, ValueError, KeyError) as error:
        return str(error)
    return None


def _selector(seen: Seen) -> str:
    return ":".join(seen.identity)


def _refused(seen: Seen, refusal: Refusal, message: str) -> Outcome:
    return Outcome(
        ok=False, message=message, wrote=False, refused=((seen, refusal.text),), code=refusal.code
    )


def _gone(op: Op) -> Outcome:
    return Outcome(ok=False, message=f"{_label(op)}: {revalidate.GONE}", wrote=False, code="gone")


def _revalidated(
    thread: dict[str, Any] | None,
    seen: Seen,
    live: PresenceProbe,
    unknown: UnknownPresence,
    *,
    require_stamp: bool = False,
) -> dict[str, Any] | Outcome:
    changed = revalidate.changed_refusal(thread, seen, require_stamp=require_stamp)
    if thread is None or changed is not None:
        refusal = changed or Refusal("gone", revalidate.GONE)
        return _refused(seen, refusal, f"{seen.label}: {refusal.text}")
    running = revalidate.running_refusal(thread, live(), unknown)
    return thread if running is None else _refused(seen, running, running.text)


def _revalidated_in(
    txn: Txn, seen: Seen, unknown: UnknownPresence, *, require_stamp: bool = False
) -> dict[str, Any] | Outcome:
    thread = txn.index.find(_selector(seen))
    return _revalidated(thread, seen, txn.live, unknown, require_stamp=require_stamp)


def _moved_unrecorded(
    seen: Seen, dest: str, error: BaseException, warnings: tuple[str, ...]
) -> Outcome:
    return Outcome(
        ok=False,
        message=f"archived {seen.label} on disk, but the registry was not updated: {error}",
        wrote=True,
        path=dest,
        warnings=warnings,
    )


def _archive_in(txn: Txn, op: ArchiveOp, ctx: OpContext) -> Outcome:
    thread = _revalidated_in(txn, op.seen, ctx.unknown)
    if isinstance(thread, Outcome):
        return thread
    result = lifecycle.archive_thread(
        txn.reg,
        thread,
        archive_root_path=ctx.archive_root_path,
        now_iso=ctx.now_iso(),
        index=txn.index,
    )
    return Outcome(
        ok=True,
        message=f"archived {op.seen.label}",
        wrote=True,
        path=result.path,
        warnings=result.warnings,
    )


def _archive_unrecorded(op: ArchiveOp, outcome: Outcome, error: Exception) -> Outcome:
    return _moved_unrecorded(op.seen, outcome.path or "", error, outcome.warnings)


def _delete_in(txn: Txn, op: DeleteOp, ctx: OpContext) -> Outcome:
    seen = op.seen
    thread = _revalidated_in(txn, seen, ctx.unknown, require_stamp=True)
    if isinstance(thread, Outcome):
        return thread
    try:
        transcript = scanner.filesystem_path(thread, "delete")
        companions = scanner.companion_paths(thread.get("source"), transcript)
        scanner.hide_from_picker(thread)
        transcript.unlink(missing_ok=True)
    except (RuntimeError, OSError) as error:
        return _refused(seen, Refusal("other", str(error)), f"{seen.label}: {error}")
    warnings: tuple[str, ...] = ()
    try:
        scanner.remove_companions(companions)
    except OSError as error:
        warnings = (f"{seen.label}: {error}",)
    registry.prune_session(
        txn.reg, registry.qualified_id(thread), ctx.now_iso(), reason="deleted", index=txn.index
    )
    return Outcome(
        ok=True,
        message=f"deleted {seen.label} — nothing harvested",
        wrote=True,
        warnings=warnings,
    )


def _delete_unrecorded(op: DeleteOp, outcome: Outcome, error: Exception) -> Outcome:
    message = f"deleted {op.seen.label} on disk, but the registry was not updated: {error}"
    return replace(outcome, ok=False, message=message)


def _apply_mark(reg: dict[str, Any], op: MarkOp, ctx: OpContext) -> Outcome:
    selector = _selector(op.seen)
    match op:
        case KeepGoingOp():
            registry.mark_reviewed(reg, selector, mtime=op.seen.mtime)
            return Outcome(ok=True, message=f"kept going: {op.seen.label}", wrote=True)
        case SnoozeOp():
            registry.snooze_thread(reg, selector, ctx.now_iso(), until=op.until)
            message = f"snoozed {op.seen.label} until {op.until[:10]}"
            return Outcome(ok=True, message=message, wrote=True)
        case KeepOp():
            registry.keep_thread(reg, selector, ctx.now_iso())
            return Outcome(ok=True, message=f"kept long-term: {op.seen.label}", wrote=True)
        case UnkeepOp():
            if not registry.unkeep_thread(reg, selector):
                return Outcome(ok=True, message=f"{op.seen.label} is not kept", wrote=False)
            message = f"unkept {op.seen.label} — back in the triage inbox"
            return Outcome(ok=True, message=message, wrote=True)
        case WakeOp():
            if not registry.wake_thread(reg, selector, ctx.now_iso()):
                return Outcome(ok=True, message=f"{op.seen.label} is not snoozed", wrote=False)
            return Outcome(ok=True, message=f"woke {op.seen.label}", wrote=True)


def _archive_harvested(op: HarvestSaveOp, ctx: OpContext, saved: str | None) -> Outcome:
    reg = registry.load_registry(ctx.registry_path)
    thread = registry.find_session(reg, _selector(op.seen))
    wrote = saved is not None
    if thread is None or thread.get("decision") != "harvested":
        reason = revalidate.GONE if thread is None else "the harvest was not recorded"
        prefix = "harvested, not archived" if wrote else op.seen.label
        code: OutcomeCode = "gone" if thread is None and not wrote else "failed"
        return Outcome(ok=False, message=f"{prefix}: {reason}", wrote=wrote, path=saved, code=code)
    try:
        result = lifecycle.archive_thread(
            reg, thread, archive_root_path=ctx.archive_root_path, now_iso=ctx.now_iso()
        )
    except (RuntimeError, OSError) as error:
        if not wrote:
            return Outcome(ok=False, message=f"{op.seen.label}: {error}", wrote=False)
        return Outcome(
            ok=False, message=f"harvested, not archived: {error}", wrote=True, path=saved
        )
    try:
        registry.commit_registry(ctx.registry_path, reg)
    except (RuntimeError, OSError) as error:
        return _moved_unrecorded(op.seen, result.path, error, result.warnings)
    if saved is None:
        return Outcome(
            ok=True,
            message=f"archived {op.seen.label}",
            wrote=True,
            path=result.path,
            warnings=result.warnings,
        )
    return Outcome(
        ok=True,
        message=f"harvested {op.seen.label} → {saved}",
        wrote=True,
        path=saved,
        warnings=result.warnings,
    )


def _harvest_save(op: HarvestSaveOp, ctx: OpContext) -> Outcome:
    with stateio.publish_lock():
        reg = registry.load_registry(ctx.registry_path)
        thread = _revalidated(
            registry.find_session(reg, _selector(op.seen)), op.seen, ctx.presence, ctx.unknown
        )
        if isinstance(thread, Outcome):
            return thread
        with stateio.sigterm_deferred():
            saved: str | None = None
            if thread.get("decision") != "harvested":
                if op.markdown is None:
                    reason = Refusal("other", "nothing harvested to save")
                    return _refused(op.seen, reason, f"{op.seen.label}: {reason.text}")
                try:
                    _, path = harvesting.save_harvest_unlocked(
                        _selector(op.seen), ctx.registry_path, op.markdown, now_iso=ctx.now_iso()
                    )
                except registry.RegistryConflict as error:
                    return Outcome(
                        ok=False,
                        message=f"harvest saved, but the registry was not updated: {error}",
                        wrote=True,
                    )
                saved = str(path)
            return _archive_harvested(op, ctx, saved)


def _prune(op: PruneOp, ctx: OpContext) -> Outcome:
    warnings: list[str] = []
    deleted, skipped = roundup.prune_planned(
        ctx.registry_path,
        op.items,
        ctx.now_iso(),
        presence=ctx.presence,
        unknown=ctx.unknown,
        cancelled=ctx.cancelled,
        progress=lambda done, total: ctx.progress(RoundupProgress("prune", done, total)),
        report=warnings.append,
    )
    noun = "session" if deleted == 1 else "sessions"
    return Outcome(
        ok=True,
        message=f"deleted {deleted} tiny {noun}",
        wrote=deleted > 0,
        refused=skipped,
        warnings=tuple(warnings),
        phases=PhasesResult(
            swept=0,
            deleted=deleted,
            purged=0,
            skipped=skipped,
            errors=0,
            cancelled=ctx.cancelled(),
            warnings=tuple(warnings),
        ),
    )


def _phases(op: RoundupPhasesOp, ctx: OpContext) -> Outcome:
    result = roundup.run_phases(
        op.plan,
        presence=ctx.presence,
        unknown=ctx.unknown,
        progress=ctx.progress,
        cancelled=ctx.cancelled,
        archive_root_path=ctx.archive_root_path,
        now_epoch=ctx.now_epoch,
    )
    return Outcome(
        ok=result.errors == 0,
        message=(
            f"roundup: archived {result.swept}, deleted {result.deleted}, purged {result.purged}"
        ),
        wrote=result.swept + result.deleted + result.purged > 0,
        refused=result.skipped,
        warnings=result.warnings,
        phases=result,
    )


def _classify(op: ClassifyOp, ctx: OpContext) -> Outcome:
    counts = enrichment.apply_batch(ctx.registry_path, op.results)
    return Outcome(
        ok=True,
        message=f"classified {counts.applied}, {counts.stale} stale",
        wrote=counts.applied > 0,
        stale=counts.stale,
    )


def _stamp(op: StampRoundupOp, ctx: OpContext) -> Outcome:
    roundup.stamp(ctx.registry_path, op.now_iso)
    return Outcome(ok=True, message="roundup done", wrote=True)


def _label(op: Op) -> str:
    match op:
        case (
            ArchiveOp()
            | DeleteOp()
            | KeepGoingOp()
            | SnoozeOp()
            | KeepOp()
            | UnkeepOp()
            | WakeOp()
            | HarvestSaveOp()
        ):
            return op.seen.label
        case MemoryDecisionOp():
            return f"{op.decision} memory"
        case MemoryRecoveryOp():
            return "restore memory" if op.restore else "recover memory"
        case _:
            return {
                PruneOp: "prune",
                RoundupPhasesOp: "roundup",
                StampRoundupOp: "roundup",
                ClassifyOp: "classify",
                ReloadOp: "reload",
                MemoryScanOp: "scan memories",
                MemoryPruneOp: "prune expired recovery",
                MemoryAssignOp: "assign memory harvest",
            }[type(op)]


def _memory_result(out: MemoryOutcome) -> Outcome:
    state = "restore deferred" if out.pending_action == "restore" else out.state
    message = f"{state}: {out.message}"
    if out.operation_id:
        message += f" · {out.operation_id}"
    if out.pending_action == "restore":
        message += " · retry restore when the store is quiet"
    return Outcome(out.state not in {"refused", "failed"}, message, bool(out.operation_id))


def _memory_write(op: MemoryOp, ctx: OpContext) -> Outcome:
    path = Path(ctx.registry_path)
    now = datetime.fromisoformat(ctx.now_iso())
    if ctx.cancelled():
        return Outcome(False, "Memory action cancelled", False)
    match op:
        case MemoryScanOp():
            registry.commit_registry(path, op.projected)
            return Outcome(True, "", True)
        case MemoryDecisionOp():
            return _memory_result(
                ctx.memory.decide(
                    op.ref,
                    decision=op.decision,
                    markdown=op.markdown,
                    expected_harvest_token=op.expected_token,
                    registry_path=path,
                    now=now,
                    retention_days=load_config().memory_recovery_retention_days
                    if op.review
                    else op.retention_days,
                    remove=True,
                    require_retention_match=op.review is None,
                    review=op.review,
                )
            )
        case MemoryRecoveryOp():
            return _memory_result(
                ctx.memory.recover(
                    op.operation_id,
                    registry_path=path,
                    now=now,
                    restore=op.restore,
                ),
            )
        case MemoryPruneOp():
            removed = prune_memory_recovery(registry_path=path, now=now, delete=True)
            return Outcome(True, f"Purged {len(removed)} expired recovery copies", bool(removed))
        case MemoryAssignOp():
            item = assign_memory_harvest(op.token, project=op.project, registry_path=path)
            return Outcome(
                True, f"Assigned harvest to {item.folder_path}", True, path=item.folder_path
            )


def _dispatch(op: Op, ctx: OpContext) -> Outcome:
    match op:
        case (
            MemoryScanOp()
            | MemoryDecisionOp()
            | MemoryRecoveryOp()
            | MemoryPruneOp()
            | MemoryAssignOp()
        ):
            return _memory_write(op, ctx)
        case (
            ArchiveOp() | DeleteOp() | KeepGoingOp() | SnoozeOp() | KeepOp() | UnkeepOp() | WakeOp()
        ):
            return _one(op, ctx)
        case HarvestSaveOp():
            return _harvest_save(op, ctx)
        case PruneOp():
            return _prune(op, ctx)
        case RoundupPhasesOp():
            return _phases(op, ctx)
        case ClassifyOp():
            return _classify(op, ctx)
        case StampRoundupOp():
            return _stamp(op, ctx)
        case ReloadOp():
            return Outcome(ok=True, message="", wrote=False)


def execute(op: Op, ctx: OpContext) -> Outcome:
    try:
        return _dispatch(op, ctx)
    except KeyError:
        if isinstance(op, (*_BULK_OPS, *_MEMORY_OPS)):
            raise
        return _gone(op)
    except (NotImplementedError, RecursionError):
        raise
    except _EXPECTED as error:
        return Outcome(ok=False, message=f"{_label(op)}: {error}", wrote=False)


type GroupResult = Outcome | Exception
type Claim = Callable[[int], bool]


@runtime_checkable
class Executor(Protocol):
    def execute(self, op: Op, ctx: OpContext) -> Outcome: ...

    def execute_group(
        self, ops: Sequence[Op], ctx: OpContext, claim: Claim
    ) -> Sequence[GroupResult | None]: ...


def group_key(op: Op) -> str | None:
    match op:
        case KeepGoingOp() | SnoozeOp() | KeepOp() | UnkeepOp() | WakeOp():
            return "mark"
        case ArchiveOp():
            return "archive"
        case DeleteOp():
            return "delete"
        case _:
            return None


def _contained(op: Op, run: Callable[[], Outcome]) -> GroupResult:
    try:
        return run()
    except KeyError:
        return _gone(op)
    except (NotImplementedError, RecursionError) as error:
        return error
    except _EXPECTED as error:
        return Outcome(ok=False, message=f"{_label(op)}: {error}", wrote=False)
    except Exception as error:  # noqa: BLE001 -- the writer reports it as this op's internal error
        return error


def _failed(op: Op, error: Exception) -> Outcome:
    return Outcome(ok=False, message=f"{_label(op)}: {error}", wrote=False)


def _wrote(result: GroupResult | None) -> bool:
    return isinstance(result, Outcome) and result.wrote


def _in_one_transaction[O: Op](
    ops: Sequence[O],
    ctx: OpContext,
    claim: Claim,
    act: Callable[[Txn, O, OpContext], Outcome],
    unrecorded: Callable[[O, Outcome, Exception], Outcome],
    *,
    publish: bool = False,
) -> list[GroupResult | None]:
    def run(txn: Txn, item: tuple[int, O]) -> GroupResult | None:
        position, op = item
        return _contained(op, partial(act, txn, op, ctx)) if claim(position) else None

    def on_disk(item: tuple[int, O], result: GroupResult | None, error: Exception) -> Outcome:
        assert isinstance(result, Outcome)
        return unrecorded(item[1], result, error)

    batch = run_batch(
        ctx.registry_path, list(enumerate(ops)), run, presence=ctx.presence, publish=publish
    )
    settled = dict(
        batch.settled(
            wrote=_wrote, unrecorded=on_disk, failed=lambda item, error: _failed(item[1], error)
        )
    )
    return [settled.get((position, op)) for position, op in enumerate(ops)]


def _mark_in(txn: Txn, op: MarkOp, ctx: OpContext) -> Outcome:
    return _apply_mark(txn.reg, op, ctx)


def _mark_unrecorded(op: MarkOp, outcome: Outcome, error: Exception) -> Outcome:
    return _failed(op, error)


def _claim_any(position: int) -> bool:
    return True


def execute_group(
    ops: Sequence[Op], ctx: OpContext, claim: Claim = _claim_any
) -> list[GroupResult | None]:
    marks = [
        op for op in ops if isinstance(op, KeepGoingOp | SnoozeOp | KeepOp | UnkeepOp | WakeOp)
    ]
    archives = [op for op in ops if isinstance(op, ArchiveOp)]
    deletes = [op for op in ops if isinstance(op, DeleteOp)]
    if len(marks) == len(ops):
        return _in_one_transaction(marks, ctx, claim, _mark_in, _mark_unrecorded)
    if len(archives) == len(ops):
        with stateio.sigterm_deferred():
            return _in_one_transaction(
                archives, ctx, claim, _archive_in, _archive_unrecorded, publish=True
            )
    if len(deletes) == len(ops):
        return _in_one_transaction(deletes, ctx, claim, _delete_in, _delete_unrecorded)
    results: list[GroupResult | None] = []
    for position, op in enumerate(ops):
        results.append(_contained(op, partial(_dispatch, op, ctx)) if claim(position) else None)
    return results


def _one(op: SingleOp, ctx: OpContext) -> Outcome:
    [result] = execute_group([op], ctx)
    if isinstance(result, Exception):
        raise result
    assert result is not None
    return result


class _Decisions:
    def execute(self, op: Op, ctx: OpContext) -> Outcome:
        return execute(op, ctx)

    def execute_group(
        self, ops: Sequence[Op], ctx: OpContext, claim: Claim
    ) -> list[GroupResult | None]:
        return execute_group(ops, ctx, claim)


EXECUTOR: Executor = _Decisions()


@dataclass(frozen=True)
class OneByOne:
    run: Callable[[Op, OpContext], Outcome]

    def execute(self, op: Op, ctx: OpContext) -> Outcome:
        return self.run(op, ctx)

    def execute_group(
        self, ops: Sequence[Op], ctx: OpContext, claim: Claim
    ) -> list[GroupResult | None]:
        results: list[GroupResult | None] = []
        for position, op in enumerate(ops):
            results.append(self.run(op, ctx) if claim(position) else None)
        return results


def as_executor(execute: Callable[[Op, OpContext], Outcome] | Executor | None) -> Executor:
    if execute is None:
        return EXECUTOR
    return execute if isinstance(execute, Executor) else OneByOne(execute)


def _worth_keeping(data: dict[str, Any]) -> bool:
    summary = data.get("summary")
    if isinstance(summary, str) and summary.strip():
        return True
    return any(str(item).strip() for key in _HARVEST_LISTS for item in data.get(key) or [])


def harvest_markdown(thread: dict[str, Any], model: Model) -> str | None:
    text = export.render_conversation(thread)
    data = model.complete_json(harvesting.build_prompt(thread, text), harvesting.HARVEST_SCHEMA)
    return harvesting.render_markdown(thread, data) if _worth_keeping(data) else None
