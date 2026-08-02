from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from .. import jsonout, overview, projects, refresh, registry
from . import default_registry


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _key_label(key: str) -> str:
    # a cleaned-to-zero project has no entry left to read gitProjectLabel from
    return key if key.startswith("/") else key.rsplit("/", 1)[-1]


def _memory_rollup(reg: dict, now: str, selectors: list[str]) -> int:
    entries = reg.get("memoryEntries", [])
    if selectors:
        entries = projects.filter_by_projects(entries, selectors)
        if not entries:
            print("no memory entries for that selector")
            return 0
        for e in sorted(entries, key=lambda e: (e.get("project") or "", e.get("name") or "")):
            obs = ", ".join(e.get("obsoleteSignals") or []) or "none"
            print(f"{e.get('name') or '(unnamed)'}  obsolete={obs}  project={e.get('project')}")
        return 0
    groups: dict[str, list[dict]] = {}
    for e in entries:
        groups.setdefault(overview.group_key(e), []).append(e)
    cleaned = reg.get("memoryProjectsCleanedAt", {})
    if not groups and not cleaned:
        print("no memory entries")
        return 0
    rows = []
    for key in set(groups) | set(cleaned):
        items = groups.get(key, [])
        label = overview.group_label(items[0]) if items else _key_label(key)
        obs = sum(1 for e in items if e.get("obsoleteSignals"))
        when = cleaned.get(key)
        status = f"cleaned {overview.age_human(when, now)}" if when else "uncleaned"
        rows.append((len(items), label, obs, status))
    for n, label, obs, status in sorted(rows, key=lambda r: (-r[0], r[1])):
        print(f"{label}: {n} entries ({obs} obsolete-candidate) [{status}]")
    return 0


def cmd_mem_overview(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl mem overview",
        description="Memory rollup (or <selector> deep-dive) over memory entries.",
    )
    ap.add_argument(
        "selector",
        nargs="?",
        default=None,
        help="repo/dir selector (git label or cwd path)",
    )
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--project", action="append", default=None)
    ap.add_argument("--no-scan", action="store_true")
    args = ap.parse_args(argv)

    now = _now()
    if args.no_scan:
        reg = registry.load_registry(args.registry)
    else:
        reg, note = refresh.auto_refresh(args.registry, now)
        if note:
            print(note + "\n")

    selectors = list(args.project or [])
    if args.selector:
        selectors.append(args.selector)
    return _memory_rollup(reg, now, selectors)


def cmd_overview(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl overview",
        description="The session view: project rollup (default), --sessions list, "
        "a <selector> deep-dive, or --archived.",
    )
    ap.add_argument(
        "selector",
        nargs="?",
        default=None,
        help="repo/dir selector (git label or cwd path) — deep single-repo report",
    )
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument(
        "--project",
        action="append",
        default=None,
        help="repo/dir selector; repeatable (alias for the positional selector)",
    )
    ap.add_argument(
        "-s", "--sessions", action="store_true", help="flat per-session list of live threads"
    )
    ap.add_argument(
        "-a", "--all", action="store_true", help="include parked/snoozed sessions and every status"
    )
    ap.add_argument(
        "--archived", action="store_true", help="list archived sessions instead of live ones"
    )
    ap.add_argument(
        "--checkouts", action="store_true", help="rollup only: also list each repo's checkout paths"
    )
    ap.add_argument(
        "--no-scan",
        action="store_true",
        help="do not auto-scan disk first (report from the registry as-is)",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="print the registry-wide rollup as JSON instead of text; "
        "ignores the selector and other view flags (-s/-a/--archived)",
    )
    args = ap.parse_args(argv)

    now = _now()
    if args.no_scan:
        reg = registry.load_registry(args.registry)
    else:
        reg, note = refresh.auto_refresh(args.registry, now)
        if note:
            print(note + "\n")

    if args.json:
        print(json.dumps(jsonout.rollup_json(reg, now), indent=2))
        return 0

    selectors = list(args.project or [])
    if args.selector:
        selectors.append(args.selector)

    # --archived wins the view.
    if args.archived:
        sel = selectors[0] if selectors else None
        print(overview.render_archived(reg, sel))
        return 0

    # triage bridge (top-level views only, not the selector deep-dive)
    if not selectors:
        n = overview.triage_count(reg["threads"], now)
        if n:
            print(f"⚠ {n} awaiting triage — rl triage\n")

    if selectors:
        scoped = dict(reg)
        scoped["threads"] = projects.filter_by_projects(reg["threads"], selectors)
        print(overview.project_detail(scoped, now, show_all=args.all))
    elif args.sessions:
        snoozed_total = sum(1 for t in reg["threads"] if t.get("snoozedAt"))
        if args.all:
            print(overview.render(reg, now, show_all=args.all))
        else:
            scoped = dict(reg)
            scoped["threads"] = [t for t in reg["threads"] if not t.get("snoozedAt")]
            print(overview.render(scoped, now, show_all=args.all))
            if snoozed_total:
                print(f"\n({snoozed_total} snoozed hidden — rl overview -s -a)")
    else:
        print(overview.render_rollup(reg, now, include_snoozed=args.all, checkouts=args.checkouts))
    return 0
