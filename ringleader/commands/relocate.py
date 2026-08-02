from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import gitinfo, registry
from . import default_registry


def cmd_relocate(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl session relocate",
        description="Re-point a session to a new directory after you moved it on disk. "
        "The transcript's baked-in cwd is stale; this pins the new location "
        "so scans don't revert it.",
    )
    ap.add_argument("id")
    ap.add_argument("path", help="the session's new directory (absolute)")
    ap.add_argument("--registry", default=str(default_registry()))
    args = ap.parse_args(argv)

    new_path = str(Path(args.path).expanduser())
    if not Path(new_path).is_dir():
        print(f"rl-relocate: {new_path} is not a directory", file=sys.stderr)
        return 1

    reg = registry.load_registry(args.registry)
    gp = gitinfo.resolve_git_project(new_path)
    git = {"key": gp.key, "label": gp.label, "root": gp.root, "remote": gp.remote} if gp else None
    try:
        registry.relocate_thread(reg, args.id, new_path, git)
    except KeyError:
        print(f"rl-relocate: unknown session {args.id}", file=sys.stderr)
        return 1
    registry.save_registry(args.registry, reg)
    where = f"{gp.label} ({new_path})" if gp else new_path
    print(f"relocated {args.id} -> {where}")
    return 0
