from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import agents, config, decisions, models, paths, presence, registry, resume, revalidate
from . import gui as gui_api
from .decisions import ArchiveOp, DeleteOp, HarvestSaveOp, KeepGoingOp, KeepOp, OpContext, SnoozeOp
from .models import Model, ModelError, proc
from .revalidate import Seen

__all__ = ["decide", "proc"]

_STAMPED = ("archive", "delete", "harvest")
_GONE = "no longer in the registry"


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


def _run(op: decisions.Op, ctx: OpContext) -> gui_api.GuiOutcome:
    return gui_api.from_outcome(decisions.execute(op, ctx))


def _remove(
    action: decisions.Decision, thread: dict[str, Any] | None, seen: Seen, ctx: OpContext
) -> gui_api.GuiOutcome:
    blocked = None if thread is None else decisions.lifecycle_reason(thread, action)
    if blocked is not None:
        return gui_api.refused(blocked)
    return _run(ArchiveOp(seen) if action == "archive" else DeleteOp(seen), ctx)


def _snooze(
    seen: Seen, until: str, now: Callable[[], datetime], ctx: OpContext
) -> gui_api.GuiOutcome:
    try:
        when = gui_api.normalize_until(until, now())
    except ValueError as error:
        return gui_api.refused(str(error))
    return _run(SnoozeOp(seen, when), ctx)


def _harvest_gate(thread: dict[str, Any], seen: Seen, ctx: OpContext) -> gui_api.GuiOutcome | None:
    reason = (
        decisions.lifecycle_reason(thread, "harvest")
        or revalidate.changed_reason(thread, seen)
        or revalidate.running_reason(thread, ctx.presence(), "refuse")
    )
    return None if reason is None else gui_api.refused(reason)


def _harvest(thread: dict[str, Any] | None, seen: Seen, ctx: OpContext) -> gui_api.GuiOutcome:
    if thread is None:
        return gui_api.refused(_GONE)
    blocked = _harvest_gate(thread, seen, ctx)
    if blocked is not None:
        return blocked
    markdown: str | None = None
    if thread.get("decision") != "harvested":
        model = _harvest_model()
        if model is None:
            return gui_api.refused("no harvest model configured — run rl config")
        try:
            markdown = decisions.harvest_markdown(thread, model)
        except (ModelError, RuntimeError, OSError) as error:
            return gui_api.GuiOutcome("failed", None, f"harvest {seen.label} failed: {error}")
        if markdown is None:
            return gui_api.GuiOutcome("empty", None, "nothing worth harvesting")
    return _run(HarvestSaveOp(seen, markdown), ctx)


def _seen(qualified: str, thread: dict[str, Any] | None, args: argparse.Namespace) -> Seen:
    source, session_id = qualified.split(":", 1)
    return Seen(
        identity=(agents.canonical_agent(source), session_id),
        label=resume.label(thread) if thread is not None else qualified,
        mtime=args.mtime,
        fingerprint=args.fingerprint,
    )


def decide(args: argparse.Namespace, now: Callable[[], datetime]) -> gui_api.GuiOutcome:
    if args.action in _STAMPED and revalidate.content_stamp(args.fingerprint) is None:
        return gui_api.refused(revalidate.NO_STAMP)
    thread = registry.find_session(registry.load_registry(args.registry), args.id)
    seen = _seen(args.id, thread, args)
    ctx = _context(args.registry, now)
    match args.action:
        case "archive" | "delete":
            return _remove(args.action, thread, seen, ctx)
        case "keep_going":
            return _run(KeepGoingOp(seen), ctx)
        case "keep":
            return _run(KeepOp(seen), ctx)
        case "snooze":
            return _snooze(seen, args.until, now, ctx)
        case "harvest":
            return _harvest(thread, seen, ctx)
    raise ValueError(f"unknown decision {args.action!r}")
