from __future__ import annotations

import argparse
import sys

from .. import registry
from . import default_registry


def cmd_mark_harvested(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session mark harvested")
    ap.add_argument("id")
    ap.add_argument("--paths", default="", help="comma-separated doc paths harvested into")
    ap.add_argument("--registry", default=str(default_registry()))
    args = ap.parse_args(argv)

    paths = [p.strip() for p in args.paths.split(",") if p.strip()]
    reg = registry.load_registry(args.registry)
    try:
        registry.mark_harvested(reg, args.id, paths)
    except KeyError:
        print(f"rl-mark-harvested: unknown session {args.id}", file=sys.stderr)
        return 1
    registry.commit_registry(args.registry, reg)
    print(f"marked harvested {args.id} ({len(paths)} docs)")
    return 0
