from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from .. import registry
from . import default_registry


def cmd_mark_reviewed(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session mark reviewed")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    args = ap.parse_args(argv)

    reg = registry.load_registry(args.registry)
    t = next((x for x in reg["threads"] if x["sessionId"] == args.id), None)
    if t is None:
        print(f"rl-mark-reviewed: unknown session {args.id}", file=sys.stderr)
        return 1
    # Record the LIVE file mtime so a session just renamed (which bumped the file)
    # isn't re-surfaced on the next scan. Fall back to the registry mtime.
    live_mtime = None
    file_path = t.get("file")
    f = Path(file_path) if isinstance(file_path, str) and file_path else None
    if f is not None and f.exists():
        live_mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC).isoformat()
    try:
        registry.mark_reviewed(reg, args.id, live_mtime)
    except KeyError:
        print(f"rl-mark-reviewed: unknown session {args.id}", file=sys.stderr)
        return 1
    registry.save_registry(args.registry, reg)
    print(f"marked reviewed (open) {args.id}")
    return 0
