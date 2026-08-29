"""Archive sessions marked `finishOnExit`, once their transcript stops changing.

A transcript cannot be moved while its agent is still running: agents open, append
and close per turn, so a move is silently undone by the next write recreating the
file. The sweep therefore waits for quiet before archiving.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .. import registry
from ..paths import archive_root
from ..session_env import current_session
from . import archive as archive_cmd
from . import default_registry

GRACE_SECONDS = 120
QUIET_TIMEOUT = 3.0
QUIET_INTERVAL = 0.25


def _mtime(path: str) -> float | None:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def due(reg: dict, now: float, *, grace_seconds: int = GRACE_SECONDS) -> list[dict]:
    out = []
    for t in reg.get("threads", []):
        if not t.get("finishOnExit"):
            continue
        m = _mtime(t.get("file", ""))
        # a transcript already gone is still due: archive_thread records it either way
        if m is None or now - m >= grace_seconds:
            out.append(t)
    return out


def wait_until_quiet(path: str, *, timeout: float = QUIET_TIMEOUT) -> None:
    """Block until two consecutive mtime samples match, or `timeout` elapses."""
    deadline = time.monotonic() + timeout
    last = _mtime(path)
    while time.monotonic() < deadline:
        time.sleep(QUIET_INTERVAL)
        now = _mtime(path)
        if now == last:
            return
        last = now


def _archive(reg: dict, threads: list[dict], registry_path: str) -> int:
    failed = 0
    for t in threads:
        try:
            dest = archive_cmd.archive_thread(reg, t, archive_root_path=str(archive_root()))
        except RuntimeError as e:
            print(f"rl-sweep: {t['sessionId']}: {e}", file=sys.stderr)
            failed += 1
            continue
        print(f"archived {t['sessionId']} -> {dest}")
    if threads:
        registry.save_registry(registry_path, reg)
    return 1 if failed else 0


def cmd_sweep(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session sweep")
    ap.add_argument("--session", help="archive this session now (the session-end hook form)")
    ap.add_argument("--current", action="store_true")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--grace", type=int, default=GRACE_SECONDS)
    args = ap.parse_args(argv)

    session_id = args.session
    if args.current:
        found = current_session()
        if found is None:
            print("rl-sweep: not inside a recognised agent session", file=sys.stderr)
            return 1
        session_id = found[1]

    reg = registry.load_registry(args.registry)

    if session_id:
        t = next((x for x in reg["threads"] if x["sessionId"] == session_id), None)
        # not an error: the hook fires for every session, most of which aren't finished
        if t is None or not t.get("finishOnExit"):
            return 0
        wait_until_quiet(t.get("file", ""))
        return _archive(reg, [t], args.registry)

    return _archive(reg, due(reg, time.time(), grace_seconds=args.grace), args.registry)
