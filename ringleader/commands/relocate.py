from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..relocation import relocate
from ..slug import normalize
from . import default_registry


def cmd_relocate(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl session relocate",
        description="Re-point ONE session at a new directory after you moved it on disk: "
        "its registry record, and the agent's own session storage, so the session stays "
        "resumable from the new path. Use `rl workspace retarget` for a whole directory.",
    )
    ap.add_argument("id")
    ap.add_argument("path", help="the session's new directory (absolute)")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument(
        "--registry-only",
        action="store_true",
        help="re-point the record only, leaving the agent's session storage alone",
    )
    args = ap.parse_args(argv)

    new_path = normalize(args.path)
    if not Path(new_path).is_dir():
        print(f"rl-relocate: {new_path} is not a directory", file=sys.stderr)
        return 1

    done = relocate(args.registry, args.id, new_path, registry_only=args.registry_only)
    for note in done.notes:
        print(note)
    for message in (*done.warnings, *done.errors):
        print(f"rl-relocate: {message}", file=sys.stderr)
    if not done.ok:
        return 1
    print(f"relocated {args.id} -> {done.where}")
    return 0
