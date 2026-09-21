from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from .. import registry, scanner
from ..session_env import current_session
from . import default_registry


def cmd_finish(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl session finish",
        description="Mark a session finished: it will be archived once its transcript "
        "goes quiet (a later `rl session sweep` does the actual move). Refuses a "
        "session that has not been harvested, since archiving it would discard its "
        "knowledge silently.",
    )
    ap.add_argument("id", nargs="?")
    ap.add_argument("--current", action="store_true", help="the session rl is running inside")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--force", action="store_true", help="finish without a harvest")
    args = ap.parse_args(argv)

    session_id = args.id
    if args.current:
        found = current_session()
        if found is None:
            print("rl-finish: not inside a recognised agent session", file=sys.stderr)
            return 1
        session_id = f"{found[0]}:{found[1]}"
    if not session_id:
        print("rl-finish: need a session id, or --current", file=sys.stderr)
        return 1

    reg = registry.load_registry(args.registry)
    t = registry.find_session(reg, session_id)
    if t is None:
        print(f"rl-finish: unknown session {session_id}", file=sys.stderr)
        return 1
    try:
        scanner.filesystem_path(t, "finish")
    except RuntimeError as error:
        print(f"rl-finish: {error}", file=sys.stderr)
        return 1
    if t.get("decision") != "harvested" and not args.force:
        print(
            f"rl-finish: {session_id} has not been harvested — archiving now would "
            "discard it silently. Run `rl session harvest` first, or pass --force to "
            "finish anyway.",
            file=sys.stderr,
        )
        return 1

    registry.mark_finish_on_exit(reg, session_id, datetime.now(UTC).isoformat())
    registry.commit_registry(args.registry, reg)
    print(f"{session_id} will be archived once its transcript goes quiet")
    return 0
