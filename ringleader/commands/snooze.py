from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from .. import registry
from . import default_registry


def cmd_snooze(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl snooze",
        description="Set a session aside: keep it open/resumable, but hide it from the "
                    "default rl projects/overview until you `rl wake` it.")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    args = ap.parse_args(argv)

    reg = registry.load_registry(args.registry)
    when = datetime.now(timezone.utc).isoformat()
    try:
        registry.snooze_thread(reg, args.id, when)
    except KeyError:
        print(f"rl-snooze: unknown session {args.id}", file=sys.stderr)
        return 1
    registry.save_registry(args.registry, reg)
    print(f"snoozed {args.id} (hidden until `rl wake {args.id[:8]}…`)")
    return 0
