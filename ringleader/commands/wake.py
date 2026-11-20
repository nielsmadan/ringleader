from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from .. import guard_args, registry
from . import default_registry


def cmd_wake(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl session wake",
        description=(
            "Un-snooze a session: it returns to the triage inbox unless it is kept or harvested."
        ),
    )
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    guard_args.add_guard_args(ap)
    args = ap.parse_args(argv)
    if guard_args.guarded_requested(args):
        from .. import contract_io  # noqa: PLC0415 -- keeps the plain verb off the decision stack

        return contract_io.run_guarded(ap, args, action="wake", verb="wake", rejected=())

    reg = registry.load_registry(args.registry)
    try:
        woken = registry.wake_thread(reg, args.id, datetime.now(UTC).isoformat())
    except KeyError:
        print(f"rl-wake: unknown session {args.id}", file=sys.stderr)
        return 1
    if not woken:
        print(f"rl-wake: {args.id} is not snoozed", file=sys.stderr)
        return 1
    registry.commit_registry(args.registry, reg)
    print(f"woke {args.id}")
    return 0
