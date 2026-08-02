from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from .. import overview, projects, registry
from . import default_registry


def cmd_mem_cleaned(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl mem-cleaned")
    ap.add_argument("selector")
    ap.add_argument("--registry", default=str(default_registry()))
    args = ap.parse_args(argv)
    reg = registry.load_registry(args.registry)
    matched = projects.filter_by_projects(reg["memoryEntries"], [args.selector])
    if not matched:
        print(
            f"rl-mem-cleaned: no memory matches '{args.selector}' — nothing to track",
            file=sys.stderr,
        )
        return 1

    keys = sorted({overview.group_key(e) for e in matched})
    when = datetime.now(UTC).isoformat()
    for k in keys:
        registry.mark_memory_project_cleaned(reg, k, when)
    registry.save_registry(args.registry, reg)
    print(f"marked memory cleaned: {', '.join(keys)}")
    return 0
