from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from . import config, export, paths, presence, refresh, registry
from . import gui as gui_api

_EXPECTED = (OSError, ValueError, KeyError, RuntimeError)


def _now_dt() -> datetime:
    return datetime.now(UTC)


def _presence() -> gui_api.PresenceView:
    try:
        live = presence.PresenceClient().require_running_session_ids()
    except (presence.PresenceUnavailable, OSError) as error:
        return gui_api.PresenceView(None, str(error))
    return gui_api.PresenceView(live, None)


def _emit(doc: dict[str, Any]) -> None:
    sys.stdout.flush()
    sys.stdout.buffer.write(gui_api.encode_document(doc))
    sys.stdout.flush()


def _parser(name: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=f"rl gui {name}")
    ap.add_argument("--registry", default=str(paths.default_registry()))
    return ap


def _parse(ap: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace:
    with contextlib.redirect_stdout(sys.stderr):
        return ap.parse_args(argv)


def _run(work: Callable[[], dict[str, Any]]) -> int:
    try:
        with contextlib.redirect_stdout(sys.stderr):
            doc = work()
    except _EXPECTED as error:
        _emit(gui_api.error_payload(str(error)))
        return 1
    _emit(doc)
    return 0


def _list_doc(registry_path: str) -> dict[str, Any]:
    now_iso = _now_dt().isoformat()
    reg = registry.load_registry(registry_path)
    return gui_api.list_payload(
        reg,
        now_iso,
        registry_path=str(Path(registry_path).resolve()),
        presence=_presence(),
        stale_hours=config.load_config().roundup_stale_hours,
    )


def cmd_gui_list(argv: list[str]) -> int:
    args = _parse(_parser("list"), argv)
    return _run(lambda: _list_doc(args.registry))


def _scan_doc(registry_path: str) -> dict[str, Any]:
    now_iso = _now_dt().isoformat()
    before = {registry.identity(t) for t in registry.load_registry(registry_path)["threads"]}
    refreshed = refresh.refresh_registry(registry_path, now_iso)
    after = {registry.identity(t) for t in refreshed.reg["threads"]}
    return gui_api.scan_payload(now_iso, before, after, refreshed.unavailable_sources)


def cmd_gui_scan(argv: list[str]) -> int:
    args = _parse(_parser("scan"), argv)
    return _run(lambda: _scan_doc(args.registry))


def _tail_item(reg: dict[str, Any], qualified: str, max_chars: int) -> dict[str, Any]:
    try:
        thread = registry.find_session(reg, qualified)
    except registry.AmbiguousSession:
        return gui_api.tail_item(qualified, None, None, max_chars, "ambiguous session id")
    except ValueError:
        thread = None
    if thread is None:
        return gui_api.tail_item(qualified, None, None, max_chars, "no longer in the registry")
    try:
        text = export.render_tail(thread, max_chars)
    except FileNotFoundError:
        return gui_api.tail_item(qualified, thread, None, max_chars, "transcript not found")
    except Exception as error:  # noqa: BLE001 - one bad transcript must not fail the batch
        reason = f"could not read the transcript ({type(error).__name__})"
        return gui_api.tail_item(qualified, thread, None, max_chars, reason)
    return gui_api.tail_item(qualified, thread, text, max_chars, None)


def _tails_doc(registry_path: str, ids: list[str], max_chars: int) -> dict[str, Any]:
    reg = registry.load_registry(registry_path)
    return gui_api.tails_payload([_tail_item(reg, qualified, max_chars) for qualified in ids])


def cmd_gui_tail(argv: list[str]) -> int:
    ap = _parser("tail")
    ap.add_argument("ids", nargs="+", metavar="ID")
    ap.add_argument("--max-chars", type=int, default=gui_api.TAIL_DEFAULT_CHARS)
    args = _parse(ap, argv)
    if not gui_api.TAIL_MIN_CHARS <= args.max_chars <= gui_api.TAIL_MAX_CHARS:
        ap.error(
            f"--max-chars must be between {gui_api.TAIL_MIN_CHARS} and {gui_api.TAIL_MAX_CHARS}"
        )
    if len(args.ids) > gui_api.TAIL_MAX_IDS:
        ap.error(f"at most {gui_api.TAIL_MAX_IDS} ids per call")
    if len(set(args.ids)) != len(args.ids):
        ap.error("each id may appear only once")
    return _run(lambda: _tails_doc(args.registry, args.ids, args.max_chars))


GUI_COMMANDS: Final[dict[str, Callable[[list[str]], int]]] = {
    "list": cmd_gui_list,
    "scan": cmd_gui_scan,
    "tail": cmd_gui_tail,
}
