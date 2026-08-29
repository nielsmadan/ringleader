"""Archive sessions marked `finishOnExit`, once their transcript stops changing.

A transcript cannot be moved while its agent is still running: agents open, append
and close per turn, so a move is silently undone by the next write recreating the
file. The sweep therefore waits for quiet before archiving.
"""

from __future__ import annotations

import argparse
import json
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

# Per the design's agent survey (docs/specs/2026-08-28-finish-session-design.md),
# Claude Code and Codex both put the id in a top-level "session_id" field, with a
# near-identical flat schema. "sessionId" is accepted defensively alongside it but
# is unverified against any real agent's payload. A nested "payload.session_id" is
# deliberately NOT accepted: ringleader/sources/codex.py notes that field names the
# *root* thread for a subagent rollout, not the exiting session — trusting it here
# could silently archive the wrong session.
_SESSION_ID_KEYS = ("session_id", "sessionId")


def _session_id_from_hook_payload(raw: str) -> str | None:
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in _SESSION_ID_KEYS:
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    return None


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
        # OSError too: the sweep runs unattended inside roundup, where a failed move
        # must not abort the prune and enrich phases that follow it
        except (RuntimeError, OSError) as e:
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
    ap.add_argument(
        "--from-hook",
        action="store_true",
        help="read a session-end hook payload as JSON on stdin and archive the session it names",
    )
    ap.add_argument("--current", action="store_true")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--grace", type=int, default=GRACE_SECONDS)
    args = ap.parse_args(argv)

    session_id = args.session
    if args.from_hook:
        session_id = _session_id_from_hook_payload(sys.stdin.read())
        if session_id is None:
            return 0
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
