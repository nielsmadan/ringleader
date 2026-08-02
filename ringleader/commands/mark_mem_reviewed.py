from __future__ import annotations

import argparse
import sys

from .. import registry
from . import default_registry


def cmd_mark_mem_reviewed(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl mark-mem-reviewed")
    ap.add_argument("path")
    ap.add_argument("--registry", default=str(default_registry()))
    args = ap.parse_args(argv)
    reg = registry.load_registry(args.registry)
    try:
        registry.mark_memory_reviewed(reg, args.path)
    except KeyError:
        print(f"rl-mark-mem-reviewed: no memory entry at {args.path}", file=sys.stderr)
        return 1
    registry.save_registry(args.registry, reg)
    print(f"reviewed {args.path}")
    return 0
