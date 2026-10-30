from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from .. import guard_args, registry
from . import default_registry


def cmd_snooze(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl session snooze",
        description="Set a session aside: keep it open/resumable, but hide it from the "
        "default rl overview until you `rl session wake` it.",
    )
    ap.add_argument("id")
    ap.add_argument("--until", help="ISO timestamp to auto-wake at")
    ap.add_argument("--for", dest="for_days", type=int, help="auto-wake after N days")
    ap.add_argument("--registry", default=str(default_registry()))
    guard_args.add_guard_args(ap)
    args = ap.parse_args(argv)
    if guard_args.guarded_requested(args):
        from .. import contract_io  # noqa: PLC0415 -- keeps the plain verb off the decision stack

        return contract_io.run_guarded(
            ap, args, action="snooze", verb="snooze", rejected=("for_days",)
        )

    reg = registry.load_registry(args.registry)
    when = datetime.now(UTC).isoformat()
    until = args.until
    if args.for_days is not None:
        until = (datetime.now(UTC) + timedelta(days=args.for_days)).isoformat()
    try:
        registry.snooze_thread(reg, args.id, when, until=until)
    except KeyError:
        print(f"rl-snooze: unknown session {args.id}", file=sys.stderr)
        return 1
    registry.commit_registry(args.registry, reg)
    if until:
        print(f"snoozed {args.id} (auto-wakes {until}, or `rl session wake {args.id[:8]}…` sooner)")
    else:
        print(f"snoozed {args.id} (hidden until `rl session wake {args.id[:8]}…`)")
    return 0
