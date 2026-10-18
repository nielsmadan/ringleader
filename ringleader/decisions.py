from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, Literal, Protocol, runtime_checkable

from . import enrichment, export, registry, revalidate, roundup, scanner, stateio
from .batch import Txn, run_batch
from .commands import archive as archive_cmd
from .commands import harvest as harvest_cmd
from .enrichment import Classified
from .models.base import Model, ModelError
from .presence import PresenceUnavailable
from .progress import CancelCheck
from .revalidate import Identity, PresenceProbe, Seen, UnknownPresence
from .roundup import PhasesResult, RoundupPlan, RoundupProgress

type Decision = Literal["archive", "harvest", "keep_going", "snooze", "keep", "delete"]


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


type MarkOp = KeepGoingOp | SnoozeOp | KeepOp
type SingleOp = ArchiveOp | DeleteOp | MarkOp | HarvestSaveOp
type Op = SingleOp | PruneOp | RoundupPhasesOp | ClassifyOp | StampRoundupOp | ReloadOp


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
        case ArchiveOp() | DeleteOp() | KeepGoingOp() | SnoozeOp() | KeepOp() | HarvestSaveOp():
            return (op.seen.identity,)
        case PruneOp():
            return tuple(item.identity for item in op.items)
        case RoundupPhasesOp():
            return tuple(item.identity for item in (*op.plan.sweep_items, *op.plan.prune_items))
        case ClassifyOp() | StampRoundupOp() | ReloadOp():
            return ()


def is_delete(op: Op) -> bool:
    return isinstance(op, DeleteOp)


def _selector(seen: Seen) -> str:
    return ":".join(seen.identity)


def _refused(seen: Seen, reason: str, message: str) -> Outcome:
    return Outcome(ok=False, message=message, wrote=False, refused=((seen, reason),))


def _revalidated(
    thread: dict[str, Any] | None,
    seen: Seen,
    live: PresenceProbe,
    unknown: UnknownPresence,
    *,
    require_stamp: bool = False,
) -> dict[str, Any] | Outcome:
    changed = revalidate.changed_reason(thread, seen, require_stamp=require_stamp)
    if thread is None or changed is not None:
        reason = changed or "no longer in the registry"
        return _refused(seen, reason, f"{seen.label}: {reason}")
    running = revalidate.running_reason(thread, live(), unknown)
    return thread if running is None else _refused(seen, running, running)


def _revalidated_in(
    txn: Txn, seen: Seen, unknown: UnknownPresence, *, require_stamp: bool = False
) -> dict[str, Any] | Outcome:
    thread = txn.index.find(_selector(seen))
    return _revalidated(thread, seen, txn.live, unknown, require_stamp=require_stamp)


def _moved_unrecorded(seen: Seen, dest: str, error: BaseException) -> Outcome:
    return Outcome(
        ok=False,
        message=f"archived {seen.label} on disk, but the registry was not updated: {error}",
        wrote=True,
        path=dest,
    )


def _archive_in(txn: Txn, op: ArchiveOp, ctx: OpContext) -> Outcome:
    thread = _revalidated_in(txn, op.seen, ctx.unknown)
    if isinstance(thread, Outcome):
        return thread
    dest = archive_cmd.archive_thread(
        txn.reg, thread, archive_root_path=ctx.archive_root_path, index=txn.index
    )
    return Outcome(ok=True, message=f"archived {op.seen.label}", wrote=True, path=dest)


def _archive_unrecorded(op: ArchiveOp, outcome: Outcome, error: Exception) -> Outcome:
    return _moved_unrecorded(op.seen, outcome.path or "", error)


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
        return _refused(seen, str(error), f"{seen.label}: {error}")
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


def _archive_harvested(op: HarvestSaveOp, ctx: OpContext, saved: str | None) -> Outcome:
    reg = registry.load_registry(ctx.registry_path)
    thread = registry.find_session(reg, _selector(op.seen))
    wrote = saved is not None
    if thread is None or thread.get("decision") != "harvested":
        reason = "no longer in the registry" if thread is None else "the harvest was not recorded"
        prefix = "harvested, not archived" if wrote else op.seen.label
        return Outcome(ok=False, message=f"{prefix}: {reason}", wrote=wrote, path=saved)
    try:
        dest = archive_cmd.archive_thread(reg, thread, archive_root_path=ctx.archive_root_path)
    except (RuntimeError, OSError) as error:
        if not wrote:
            return Outcome(ok=False, message=f"{op.seen.label}: {error}", wrote=False)
        return Outcome(
            ok=False, message=f"harvested, not archived: {error}", wrote=True, path=saved
        )
    try:
        registry.commit_registry(ctx.registry_path, reg)
    except (RuntimeError, OSError) as error:
        return _moved_unrecorded(op.seen, dest, error)
    if saved is None:
        return Outcome(ok=True, message=f"archived {op.seen.label}", wrote=True, path=dest)
    return Outcome(ok=True, message=f"harvested {op.seen.label} → {saved}", wrote=True, path=saved)


def _harvest_save(op: HarvestSaveOp, ctx: OpContext) -> Outcome:
    with stateio.publish_lock():
        reg = registry.load_registry(ctx.registry_path)
        thread = _revalidated(
            registry.find_session(reg, _selector(op.seen)), op.seen, ctx.presence, ctx.unknown
        )
        if isinstance(thread, Outcome):
            return thread
        saved: str | None = None
        if thread.get("decision") != "harvested":
            if op.markdown is None:
                reason = "nothing harvested to save"
                return _refused(op.seen, reason, f"{op.seen.label}: {reason}")
            try:
                _, path = harvest_cmd.save_harvest_unlocked(
                    _selector(op.seen), ctx.registry_path, op.markdown
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
        case ArchiveOp() | DeleteOp() | KeepGoingOp() | SnoozeOp() | KeepOp() | HarvestSaveOp():
            return op.seen.label
        case PruneOp():
            return "prune"
        case RoundupPhasesOp() | StampRoundupOp():
            return "roundup"
        case ClassifyOp():
            return "classify"
        case ReloadOp():
            return "reload"


def _dispatch(op: Op, ctx: OpContext) -> Outcome:
    match op:
        case ArchiveOp() | DeleteOp() | KeepGoingOp() | SnoozeOp() | KeepOp():
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
        if isinstance(op, _BULK_OPS):
            raise
        return Outcome(ok=False, message=f"{_label(op)}: no longer in the registry", wrote=False)
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
        case KeepGoingOp() | SnoozeOp() | KeepOp():
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
        return Outcome(ok=False, message=f"{_label(op)}: no longer in the registry", wrote=False)
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
    marks = [op for op in ops if isinstance(op, KeepGoingOp | SnoozeOp | KeepOp)]
    archives = [op for op in ops if isinstance(op, ArchiveOp)]
    deletes = [op for op in ops if isinstance(op, DeleteOp)]
    if len(marks) == len(ops):
        return _in_one_transaction(marks, ctx, claim, _mark_in, _mark_unrecorded)
    if len(archives) == len(ops):
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
    data = model.complete_json(harvest_cmd.build_prompt(thread, text), harvest_cmd.HARVEST_SCHEMA)
    return harvest_cmd.render_markdown(thread, data) if _worth_keeping(data) else None
