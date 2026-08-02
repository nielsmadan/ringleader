from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .. import memory, projects, registry
from . import default_registry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cmd_mem_prune(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl mem-prune")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--project", action="append", default=None)
    ap.add_argument("--delete", action="store_true")
    args = ap.parse_args(argv)

    reg = registry.load_registry(args.registry)
    scanned = memory.scan_memory(memory.projects_dir())
    reg = registry.reconcile_memory(reg, scanned, _now())
    registry.save_registry(args.registry, reg)

    cands = [e for e in reg["memoryEntries"] if e.get("obsoleteSignals")]
    if args.project:
        cands = projects.filter_by_projects(cands, args.project)
    if not cands:
        print("nothing to prune")
        return 0
    for e in cands:
        print(f"  [{','.join(e['obsoleteSignals'])}] {e.get('name') or e['path']}  "
              f"{e.get('project') or ''}")
    print(f"\n{len(cands)} obsolete memory entries")
    if not args.delete:
        print("(preview — re-run with --delete to remove these)")
        return 0
    try:
        resp = input(f"Delete {len(cands)} memory files? [y/N] ").strip().lower()
    except EOFError:
        resp = ""
    if resp not in ("y", "yes"):
        print("aborted, nothing deleted")
        return 0
    deleted = 0
    deleted_paths = {e["path"] for e in cands}
    for e in cands:
        try:
            Path(e["path"]).unlink(missing_ok=True)
            deleted += 1
        except OSError as err:
            print(f"skipped {e['path']}: {err}", file=sys.stderr)
    reg["memoryEntries"] = [e for e in reg["memoryEntries"] if e["path"] not in deleted_paths]
    registry.save_registry(args.registry, reg)

    by_dir = defaultdict(set)
    for p in deleted_paths:
        p = Path(p)
        by_dir[p.parent].add(p.name)
    for mem_dir, basenames in by_dir.items():
        index_path = mem_dir / memory.INDEX_NAME
        if not index_path.exists():
            continue
        index_text = index_path.read_text(encoding="utf-8")
        index_path.write_text(memory.rewrite_index_without(index_text, basenames),
                               encoding="utf-8")

    print(f"deleted {deleted} memory files")
    return 0
