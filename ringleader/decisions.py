from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

from . import enrichment, export, registry, revalidate, roundup, scanner, stateio
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


type SingleOp = ArchiveOp | DeleteOp | KeepGoingOp | SnoozeOp | KeepOp | HarvestSaveOp
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


def _find(reg: dict[str, Any], seen: Seen) -> dict[str, Any] | None:
    return registry.find_session(reg, _selector(seen))


def _refused(seen: Seen, reason: str, message: str) -> Outcome:
    return Outcome(ok=False, message=message, wrote=False, refused=((seen, reason),))


def _revalidated(
    reg: dict[str, Any],
    seen: Seen,
    live: frozenset[Identity] | None,
    unknown: UnknownPresence,
    *,
    require_stamp: bool = False,
) -> dict[str, Any] | Outcome:
    thread = _find(reg, seen)
    changed = revalidate.changed_reason(thread, seen, require_stamp=require_stamp)
    if thread is None or changed is not None:
        reason = changed or "no longer in the registry"
        return _refused(seen, reason, f"{seen.label}: {reason}")
    running = revalidate.running_reason(thread, live, unknown)
    return thread if running is None else _refused(seen, running, running)


def _moved_unrecorded(seen: Seen, dest: str, error: BaseException) -> Outcome:
    return Outcome(
        ok=False,
        message=f"archived {seen.label} on disk, but the registry was not updated: {error}",
        wrote=True,
        path=dest,
    )


def _archive(op: ArchiveOp, ctx: OpContext) -> Outcome:
    live = ctx.presence()
    with stateio.publish_lock():
        reg = registry.load_registry(ctx.registry_path)
        thread = _revalidated(reg, op.seen, live, ctx.unknown)
        if isinstance(thread, Outcome):
            return thread
        dest = archive_cmd.archive_thread(reg, thread, archive_root_path=ctx.archive_root_path)
        try:
            registry.commit_registry(ctx.registry_path, reg)
        except (RuntimeError, OSError) as error:
            return _moved_unrecorded(op.seen, dest, error)
    return Outcome(ok=True, message=f"archived {op.seen.label}", wrote=True, path=dest)


def _delete_from(
    reg: dict[str, Any], seen: Seen, live: frozenset[Identity] | None, ctx: OpContext
) -> Outcome:
    thread = _revalidated(reg, seen, live, ctx.unknown, require_stamp=True)
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
    registry.prune_session(reg, registry.qualified_id(thread), ctx.now_iso(), reason="deleted")
    return Outcome(
        ok=True,
        message=f"deleted {seen.label} — nothing harvested",
        wrote=True,
        warnings=warnings,
    )


def _delete(op: DeleteOp, ctx: OpContext) -> Outcome:
    live = ctx.presence()
    outcomes: list[Outcome] = []
    try:
        registry.mutate_registry(
            ctx.registry_path, lambda reg: outcomes.append(_delete_from(reg, op.seen, live, ctx))
        )
    except (RuntimeError, OSError) as error:
        if outcomes and outcomes[0].wrote:
            message = f"deleted {op.seen.label} on disk, but the registry was not updated: {error}"
            return replace(outcomes[0], ok=False, message=message)
        raise
    return outcomes[0]


def _mark(seen: Seen, ctx: OpContext, apply: Callable[[dict[str, Any], str], object]) -> None:
    registry.mutate_registry(ctx.registry_path, lambda reg: apply(reg, _selector(seen)))


def _keep_going(op: KeepGoingOp, ctx: OpContext) -> Outcome:
    _mark(op.seen, ctx, lambda reg, sid: registry.mark_reviewed(reg, sid, mtime=op.seen.mtime))
    return Outcome(ok=True, message=f"kept going: {op.seen.label}", wrote=True)


def _snooze(op: SnoozeOp, ctx: OpContext) -> Outcome:
    when = ctx.now_iso()
    _mark(op.seen, ctx, lambda reg, sid: registry.snooze_thread(reg, sid, when, until=op.until))
    return Outcome(ok=True, message=f"snoozed {op.seen.label} until {op.until[:10]}", wrote=True)


def _keep(op: KeepOp, ctx: OpContext) -> Outcome:
    when = ctx.now_iso()
    _mark(op.seen, ctx, lambda reg, sid: registry.keep_thread(reg, sid, when))
    return Outcome(ok=True, message=f"kept long-term: {op.seen.label}", wrote=True)


def _archive_harvested(op: HarvestSaveOp, ctx: OpContext, saved: str | None) -> Outcome:
    reg = registry.load_registry(ctx.registry_path)
    thread = _find(reg, op.seen)
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
    live = ctx.presence()
    with stateio.publish_lock():
        reg = registry.load_registry(ctx.registry_path)
        thread = _revalidated(reg, op.seen, live, ctx.unknown)
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


def _dispatch(op: Op, ctx: OpContext) -> Outcome:  # noqa: PLR0911 -- one arm per op type
    match op:
        case ArchiveOp():
            return _archive(op, ctx)
        case DeleteOp():
            return _delete(op, ctx)
        case KeepGoingOp():
            return _keep_going(op, ctx)
        case SnoozeOp():
            return _snooze(op, ctx)
        case KeepOp():
            return _keep(op, ctx)
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


def _worth_keeping(data: dict[str, Any]) -> bool:
    summary = data.get("summary")
    if isinstance(summary, str) and summary.strip():
        return True
    return any(str(item).strip() for key in _HARVEST_LISTS for item in data.get(key) or [])


def harvest_markdown(thread: dict[str, Any], model: Model) -> str | None:
    text = export.render_conversation(thread)
    data = model.complete_json(harvest_cmd.build_prompt(thread, text), harvest_cmd.HARVEST_SCHEMA)
    return harvest_cmd.render_markdown(thread, data) if _worth_keeping(data) else None
