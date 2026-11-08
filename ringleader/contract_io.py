from __future__ import annotations

import argparse
import contextlib
import shutil
import signal
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any, NoReturn

from . import (
    config,
    contract,
    export,
    handoff,
    harvests,
    presence,
    refresh,
    registry,
    resume,
    revalidate,
)

if TYPE_CHECKING:
    from .decisions import Decision

_EXPECTED = (OSError, ValueError, KeyError, RuntimeError)
_SIGTERM_EXIT = 143


def _now_dt() -> datetime:
    return datetime.now(UTC)


def _presence() -> contract.PresenceView:
    client = presence.PresenceClient()
    try:
        live = client.require_running_session_ids()
    except (presence.PresenceUnavailable, OSError) as error:
        code: contract.PresenceCode = "failed" if client.is_installed() else "not_installed"
        return contract.PresenceView.unknown(str(error), code)
    return contract.PresenceView.ok(live)


def _agents() -> tuple[contract.AgentView, ...]:
    return tuple(
        contract.AgentView(name, handoff.installed(name, shutil.which))
        for name in resume.launchable_agents()
    )


def emit(doc: dict[str, Any]) -> None:
    sys.stdout.flush()
    sys.stdout.buffer.write(contract.encode_document(doc))
    sys.stdout.flush()


def run_json(work: Callable[[], dict[str, Any]]) -> int:
    try:
        with contextlib.redirect_stdout(sys.stderr):
            doc = work()
    except _EXPECTED as error:
        emit(contract.error_payload(str(error)))
        return 1
    emit(doc)
    return 0


def _scan(registry_path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    scanned_at = _now_dt().isoformat()
    before = {registry.identity(t) for t in registry.load_registry(registry_path)["threads"]}
    refreshed = refresh.refresh_registry(registry_path, scanned_at)
    after = {registry.identity(t) for t in refreshed.reg["threads"]}
    summary = contract.scan_summary(scanned_at, before, after, refreshed.unavailable_sources)
    return refreshed.reg, summary


def list_doc(registry_path: str, *, scan: bool) -> dict[str, Any]:
    reg, summary = _scan(registry_path) if scan else (registry.load_registry(registry_path), None)
    surroundings = contract.Surroundings(
        presence=_presence(),
        stale_hours=config.load_config().roundup_stale_hours,
        pending_harvests=harvests.pending_count(),
        agents=_agents(),
    )
    return contract.list_payload(
        reg,
        _now_dt().isoformat(),
        registry_path=str(Path(registry_path).resolve()),
        scan=summary,
        surroundings=surroundings,
    )


def handoff_doc(plan: Callable[[str], handoff.HandoffPlan]) -> dict[str, Any]:
    return contract.handoff_payload(plan(_now_dt().isoformat()))


def _tail_item(reg: dict[str, Any], qualified: str, max_chars: int) -> dict[str, Any]:
    try:
        thread = registry.find_session(reg, qualified)
    except registry.AmbiguousSession:
        return contract.tail_item(qualified, None, None, max_chars, "ambiguous session id")
    if thread is None:
        return contract.tail_item(qualified, None, None, max_chars, revalidate.GONE)
    try:
        text = export.render_tail(thread, max_chars)
    except FileNotFoundError:
        return contract.tail_item(qualified, thread, None, max_chars, "transcript not found")
    except Exception as error:  # noqa: BLE001 - one bad transcript must not fail the batch
        reason = f"could not read the transcript ({type(error).__name__})"
        return contract.tail_item(qualified, thread, None, max_chars, reason)
    return contract.tail_item(qualified, thread, text, max_chars, None)


def tails_doc(registry_path: str, ids: list[str], max_chars: int) -> dict[str, Any]:
    reg = registry.load_registry(registry_path)
    return contract.tails_payload([_tail_item(reg, qualified, max_chars) for qualified in ids])


def _terminate(_signum: int, _frame: FrameType | None) -> NoReturn:
    raise SystemExit(_SIGTERM_EXIT)


def _flag(ap: argparse.ArgumentParser, dest: str) -> str:
    return next(a.option_strings[0] for a in ap._actions if a.dest == dest)


def _validate_guarded(
    ap: argparse.ArgumentParser,
    args: argparse.Namespace,
    action: Decision,
    rejected: tuple[str, ...],
) -> None:
    for dest in rejected:
        value = getattr(args, dest)
        if value is not None and value is not False:
            ap.error(f"{_flag(ap, dest)} cannot be combined with --json or --if-*")
    if args.if_mtime is None:
        given = (("--json", args.json), ("--if-fingerprint", args.if_fingerprint is not None))
        flags = [flag for flag, on in given if on]
        ap.error(f"{' and '.join(flags)} {'needs' if len(flags) == 1 else 'need'} --if-mtime")
    if not args.id:
        ap.error("the guarded path needs a session ID")
    if ":" not in args.id:
        ap.error("ID must be qualified as <source>:<sessionId>")
    if action == "harvest" and not args.archive:
        ap.error("a guarded harvest saves and archives, so it needs --archive")
    if action == "snooze" and args.until is None:
        ap.error("a guarded snooze needs --until")


def _run_human(verb: str, work: Callable[[], contract.GuiOutcome]) -> int:
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = work()
    except _EXPECTED as error:
        print(f"rl-{verb}: {error}", file=sys.stderr)
        return 1
    for warning in result.warnings:
        print(f"rl-{verb}: {warning}", file=sys.stderr)
    if result.status not in ("ok", "empty"):
        print(f"rl-{verb}: {result.message}", file=sys.stderr)
        if result.path:
            print(result.path, file=sys.stderr)
        return 1
    print(result.message)
    if result.path and result.path not in result.message:
        print(result.path)
    return 0


def run_guarded(
    ap: argparse.ArgumentParser,
    args: argparse.Namespace,
    *,
    action: Decision,
    verb: str,
    rejected: tuple[str, ...],
) -> int:
    _validate_guarded(ap, args, action, rejected)
    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        from . import guarded  # noqa: PLC0415 -- keeps inbox --json and export --json off the decision stack

        def work() -> contract.GuiOutcome:
            return guarded.decide(
                action,
                args.id,
                registry_path=args.registry,
                mtime=args.if_mtime,
                fingerprint=args.if_fingerprint,
                until=getattr(args, "until", None),
                now=_now_dt,
            )

        if args.json:
            return run_json(lambda: contract.outcome_payload(action, args.id, work()))
        return _run_human(verb, work)
    finally:
        try:
            if "ringleader.guarded" in sys.modules:
                from .models import proc  # noqa: PLC0415

                proc.kill_all_children()
        finally:
            signal.signal(signal.SIGTERM, previous)
