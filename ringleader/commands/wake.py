from __future__ import annotations

import argparse
import sys

from .. import registry
from . import default_registry


def cmd_wake(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl session wake",
        description="Un-snooze a session so it shows in rl overview again.",
    )
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    args = ap.parse_args(argv)

    reg = registry.load_registry(args.registry)
    try:
        registry.wake_thread(reg, args.id)
    except KeyError:
        print(f"rl-wake: unknown session {args.id}", file=sys.stderr)
        return 1
    registry.commit_registry(args.registry, reg)
    print(f"woke {args.id}")
    return 0
