from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import (
    agents,
    config,
    contract,
    decisions,
    models,
    paths,
    presence,
    registry,
    resume,
    revalidate,
)
from .decisions import ArchiveOp, DeleteOp, HarvestSaveOp, KeepGoingOp, KeepOp, OpContext, SnoozeOp
from .models import Model, ModelError
from .revalidate import Seen

_STAMPED = ("archive", "delete", "harvest")


def _harvest_model() -> Model | None:
    return models.active_model(config.load_config(), "harvest")


def _live() -> frozenset[revalidate.Identity]:
    return presence.PresenceClient().require_running_session_ids()


def _context(registry_path: str, now: Callable[[], datetime]) -> OpContext:
    return OpContext(
        registry_path=registry_path,
        archive_root_path=str(paths.archive_root()),
        now_iso=lambda: now().isoformat(),
        now_epoch=lambda: now().timestamp(),
        presence=revalidate.probe(_live),
        unknown="refuse",
        cancelled=lambda: False,
        progress=lambda _p: None,
    )


def _run(op: decisions.Op, ctx: OpContext) -> contract.GuiOutcome:
    return contract.from_outcome(decisions.execute(op, ctx))


def _remove(
    action: decisions.Decision, thread: dict[str, Any] | None, seen: Seen, ctx: OpContext
) -> contract.GuiOutcome:
    blocked = None if thread is None else decisions.lifecycle_reason(thread, action)
    if blocked is not None:
        return contract.refused(blocked, "lifecycle_blocked")
    return _run(ArchiveOp(seen) if action == "archive" else DeleteOp(seen), ctx)


def _snooze(
    seen: Seen, until: str, now: Callable[[], datetime], ctx: OpContext
) -> contract.GuiOutcome:
    try:
        when = contract.normalize_until(until, now())
    except contract.UntilError as error:
        return contract.refused(str(error), error.code)
    return _run(SnoozeOp(seen, when), ctx)


def _harvest_gate(thread: dict[str, Any], seen: Seen, ctx: OpContext) -> contract.GuiOutcome | None:
    blocked = decisions.lifecycle_reason(thread, "harvest")
    if blocked is not None:
        return contract.refused(blocked, "lifecycle_blocked")
    refusal = revalidate.changed_refusal(thread, seen) or revalidate.running_refusal(
        thread, ctx.presence(), "refuse"
    )
    return None if refusal is None else contract.refused(refusal.text, refusal.code)


def _harvest(thread: dict[str, Any] | None, seen: Seen, ctx: OpContext) -> contract.GuiOutcome:
    if thread is None:
        return contract.refused(revalidate.GONE, "gone")
    blocked = _harvest_gate(thread, seen, ctx)
    if blocked is not None:
        return blocked
    markdown: str | None = None
    if thread.get("decision") != "harvested":
        model = _harvest_model()
        if model is None:
            return contract.refused(
                "no harvest model configured — run rl config", "no_harvest_model"
            )
        try:
            markdown = decisions.harvest_markdown(thread, model)
        except (ModelError, RuntimeError, OSError) as error:
            return contract.GuiOutcome(
                "failed", None, f"harvest {seen.label} failed: {error}", code="harvest_failed"
            )
        if markdown is None:
            return contract.GuiOutcome("empty", None, "nothing worth harvesting")
    return _run(HarvestSaveOp(seen, markdown), ctx)


def _seen(
    qualified: str, thread: dict[str, Any] | None, mtime: str, fingerprint: str | None
) -> Seen:
    source, session_id = qualified.split(":", 1)
    return Seen(
        identity=(agents.canonical_agent(source), session_id),
        label=resume.label(thread) if thread is not None else qualified,
        mtime=mtime,
        fingerprint=fingerprint,
    )


def decide(  # noqa: PLR0913 -- the shared guarded-decision signature
    action: decisions.Decision,
    qualified: str,
    *,
    registry_path: str,
    mtime: str,
    fingerprint: str | None,
    until: str | None,
    now: Callable[[], datetime],
) -> contract.GuiOutcome:
    if action in _STAMPED and revalidate.content_stamp(fingerprint) is None:
        return contract.refused(revalidate.NO_STAMP, "no_fingerprint")
    thread = registry.find_session(registry.load_registry(registry_path), qualified)
    seen = _seen(qualified, thread, mtime, fingerprint)
    ctx = _context(registry_path, now)
    match action:
        case "archive" | "delete":
            return _remove(action, thread, seen, ctx)
        case "keep_going":
            return _run(KeepGoingOp(seen), ctx)
        case "keep":
            return _run(KeepOp(seen), ctx)
        case "snooze":
            if until is None:
                raise ValueError("snooze needs an until time")
            return _snooze(seen, until, now, ctx)
        case "harvest":
            return _harvest(thread, seen, ctx)
    raise ValueError(f"unknown decision {action!r}")
