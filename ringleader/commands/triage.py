from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from .. import jsonout, overview, projects, refresh, registry
from . import default_registry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cmd_triage(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl triage",
        description="List sessions awaiting your decision (not reviewed/harvested/parked).")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--project", action="append", default=None,
                    help="repo/dir selector; repeatable — scope the inbox")
    ap.add_argument("--no-scan", action="store_true",
                    help="do not auto-scan disk first (report from the registry as-is)")
    ap.add_argument("--memory", "-m", action="store_true",
                    help="show the memory-review inbox instead of the session inbox")
    ap.add_argument("--unclassified", "-u", action="store_true",
                    help="list only never-classified sessions (never triaged), "
                         "not the full awaiting-a-decision inbox")
    ap.add_argument("--json", action="store_true",
                    help="print the triage inbox as JSON instead of text; honors -u/"
                         "--unclassified, not valid with --memory")
    args = ap.parse_args(argv)

    if args.memory and args.json:
        raise SystemExit("rl triage: --json is not supported with --memory")

    if args.memory:
        from .. import memory as memmod
        reg = registry.load_registry(args.registry)
        reg = registry.reconcile_memory(reg, memmod.scan_memory(memmod.projects_dir()), _now())
        registry.save_registry(args.registry, reg)
        rows = registry.memory_needing_review(reg)
        if args.project:
            rows = projects.filter_by_projects(rows, args.project)
        groups: dict[str, list[dict]] = {}
        for e in rows:
            groups.setdefault(overview.group_key(e), []).append(e)
        if not groups:
            print("no memory awaiting review")
            return 0
        for key, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            label = overview.group_label(items[0])
            obs = sum(1 for e in items if e.get("obsoleteSignals"))
            print(f"{label}: {len(items)} entries ({obs} obsolete-candidate)")
        return 0

    now = _now()
    if args.no_scan:
        reg = registry.load_registry(args.registry)
    else:
        reg, note = refresh.auto_refresh(args.registry, now)
        if note:
            print(note + "\n")
    threads = projects.filter_by_projects(reg["threads"], args.project)
    if args.json:
        print(json.dumps(
            jsonout.triage_json(threads, now, unclassified_only=args.unclassified),
            indent=2))
        return 0
    print(overview.render_triage(threads, now, unclassified_only=args.unclassified))
    return 0
