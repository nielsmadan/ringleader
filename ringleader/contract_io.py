from __future__ import annotations

import argparse
import contextlib
import signal
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any, Final, NoReturn

from . import config, contract, export, paths, presence, refresh, registry

_EXPECTED = (OSError, ValueError, KeyError, RuntimeError)
_ACTIONS = ("harvest", "archive", "delete", "snooze", "keep", "keep_going")
_SIGTERM_EXIT = 143


def _now_dt() -> datetime:
    return datetime.now(UTC)


def _presence() -> contract.PresenceView:
    try:
        live = presence.PresenceClient().require_running_session_ids()
    except (presence.PresenceUnavailable, OSError) as error:
        return contract.PresenceView(None, str(error))
    return contract.PresenceView(live, None)


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
    return contract.list_payload(
        reg,
        _now_dt().isoformat(),
        registry_path=str(Path(registry_path).resolve()),
        presence=_presence(),
        stale_hours=config.load_config().roundup_stale_hours,
        scan=summary,
    )


def _tail_item(reg: dict[str, Any], qualified: str, max_chars: int) -> dict[str, Any]:
    try:
        thread = registry.find_session(reg, qualified)
    except registry.AmbiguousSession:
        return contract.tail_item(qualified, None, None, max_chars, "ambiguous session id")
    except ValueError:
        thread = None
    if thread is None:
        return contract.tail_item(qualified, None, None, max_chars, "no longer in the registry")
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


def _parser(name: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=f"rl gui {name}")
    ap.add_argument("--registry", default=str(paths.default_registry()))
    return ap


def _parse(ap: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace:
    with contextlib.redirect_stdout(sys.stderr):
        return ap.parse_args(argv)


def _decide_args(argv: list[str]) -> argparse.Namespace:
    ap = _parser("decide")
    ap.add_argument("action", choices=_ACTIONS)
    ap.add_argument("id", metavar="ID")
    ap.add_argument("--mtime", required=True)
    ap.add_argument("--fingerprint", default=None)
    ap.add_argument("--until", default=None)
    args = _parse(ap, argv)
    if args.action == "snooze" and args.until is None:
        ap.error("snooze needs --until")
    if args.action != "snooze" and args.until is not None:
        ap.error(f"--until applies only to snooze, not {args.action}")
    if ":" not in args.id:
        ap.error("ID must be qualified as <source>:<sessionId>")
    return args


def _terminate(_signum: int, _frame: FrameType | None) -> NoReturn:
    raise SystemExit(_SIGTERM_EXIT)


def cmd_gui_decide(argv: list[str]) -> int:
    args = _decide_args(argv)
    from . import guarded  # noqa: PLC0415 -- keeps the json read paths fast

    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        return run_json(
            lambda: contract.outcome_payload(args.action, args.id, guarded.decide(args, _now_dt))
        )
    finally:
        guarded.proc.kill_all_children()
        signal.signal(signal.SIGTERM, previous)


GUI_COMMANDS: Final[dict[str, Callable[[list[str]], int]]] = {
    "decide": cmd_gui_decide,
}
