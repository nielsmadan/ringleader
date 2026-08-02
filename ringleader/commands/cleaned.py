from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from .. import overview, projects, registry
from . import default_registry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cmd_cleaned(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl cleaned",
        description="Record that a project was just triaged (clean-project), so "
                    "rl projects can show what's done vs. needs another look.")
    ap.add_argument("selector", help="git-project label or a cwd path")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--at", default=None, help="ISO timestamp to record (default: now)")
    args = ap.parse_args(argv)

    reg = registry.load_registry(args.registry)
    matched = projects.filter_by_projects(reg["threads"], [args.selector])
    keys = sorted({overview.group_key(t) for t in matched})
    if not keys:
        print(f"rl-cleaned: no sessions match '{args.selector}' — nothing to track",
              file=sys.stderr)
        return 1

    when = args.at or _now()
    for k in keys:
        registry.mark_project_cleaned(reg, k, when)
    registry.save_registry(args.registry, reg)
    print(f"marked cleaned: {', '.join(keys)} @ {when[:10]}")
    return 0
