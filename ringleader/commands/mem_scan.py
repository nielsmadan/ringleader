from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from .. import memory, projects, registry
from . import default_registry


def _now() -> str:
    return datetime.now(UTC).isoformat()


def cmd_mem_scan(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl mem-scan")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--project", action="append", default=None)
    args = ap.parse_args(argv)

    reg = registry.load_registry(args.registry)
    scanned = memory.scan_memory(memory.projects_dir())
    reg = registry.reconcile_memory(reg, scanned, _now())
    registry.save_registry(args.registry, reg)

    rows = registry.memory_needing_review(reg)
    if args.project:
        rows = projects.filter_by_projects(rows, args.project)
    for e in rows:
        print(json.dumps(e, ensure_ascii=False))
    print(f"# {len(rows)} memory entries need review", file=sys.stderr)
    return 0
