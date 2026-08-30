from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from .. import jsonout, overview, projects, refresh, registry
from .. import memory as memmod
from . import default_registry


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _memory_inbox(registry_path: str, projects_sel, no_scan: bool) -> int:
    reg = registry.load_registry(registry_path)
    reg = registry.reconcile_memory(reg, memmod.scan_memory(memmod.projects_dir()), _now())
    registry.save_registry(registry_path, reg)
    rows = registry.memory_needing_review(reg)
    if projects_sel:
        rows = projects.filter_by_projects(rows, projects_sel)
    groups: dict[str, list[dict]] = {}
    for e in rows:
        groups.setdefault(overview.group_key(e), []).append(e)
    if not groups:
        print("no memory awaiting review")
        return 0
    for _key, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        label = overview.group_label(items[0])
        obs = sum(1 for e in items if e.get("obsoleteSignals"))
        print(f"{label}: {len(items)} entries ({obs} obsolete-candidate)")
    return 0


def cmd_mem_triage(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl mem triage")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--project", action="append", default=None)
    ap.add_argument("--no-scan", action="store_true")
    a = ap.parse_args(argv)
    return _memory_inbox(a.registry, a.project, a.no_scan)


def cmd_triage(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl triage",
        description="List sessions awaiting your decision (not reviewed/harvested/parked).",
    )
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument(
        "--project",
        action="append",
        default=None,
        help="repo/dir selector; repeatable — scope the inbox",
    )
    ap.add_argument(
        "--no-scan",
        action="store_true",
        help="do not auto-scan disk first (report from the registry as-is)",
    )
    ap.add_argument(
        "--unclassified",
        "-u",
        action="store_true",
        help="list only never-classified sessions (never triaged), "
        "not the full awaiting-a-decision inbox",
    )
    ap.add_argument(
        "--source",
        action="append",
        default=None,
        help="filter to sessions from this source (repeatable)",
    )
    ap.add_argument(
        "--include-filtered",
        action="store_true",
        help="also show sessions hidden by a `rl config filter` name pattern",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="print the triage inbox as JSON instead of text; honors -u/--unclassified",
    )
    ap.add_argument(
        "--recent",
        type=int,
        default=None,
        metavar="DAYS",
        help="only sessions active within the last DAYS",
    )
    args = ap.parse_args(argv)

    now = _now()
    if args.no_scan:
        reg = registry.load_registry(args.registry)
    else:
        reg, note = refresh.auto_refresh(args.registry, now)
        if note:
            print(note + "\n")
    threads = projects.filter_by_projects(reg["threads"], args.project)
    if args.source:
        wanted = set(args.source)
        threads = [t for t in threads if (t.get("source") or "claude-code") in wanted]
    threads = overview.filter_recent(threads, now, args.recent)
    if not args.include_filtered:
        threads = [t for t in threads if not overview.is_filtered(t)]
    if args.json:
        print(
            json.dumps(
                jsonout.triage_json(threads, now, unclassified_only=args.unclassified), indent=2
            )
        )
        return 0
    print(overview.render_triage(threads, now, unclassified_only=args.unclassified))
    return 0
