from __future__ import annotations

import argparse
from pathlib import Path

from ..migrate import migrate


def cmd_migrate(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl migrate",
        description="Move a legacy repo-root registry.json/archive/ into "
        "~/.local/share/ringleader (or $XDG_DATA_HOME/ringleader). No-op if state "
        "already lives there.")
    ap.add_argument("old_dir", nargs="?", default=".",
                    help="directory holding the legacy registry.json/archive/ (default: cwd)")
    args = ap.parse_args(argv)

    moved = migrate(Path(args.old_dir))
    if not moved:
        print("rl-migrate: nothing to move")
        return 0
    for path in moved:
        print(f"moved -> {path}")
    return 0
