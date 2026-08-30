from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

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


def _filter_source_recent(threads, now: str, args) -> list[dict]:
    if args.source:
        wanted = set(args.source)
        threads = [t for t in threads if (t.get("source") or "claude-code") in wanted]
    return overview.filter_recent(threads, now, args.recent)


def _scope_to_cwd(reg: dict, now: str, args) -> dict:
    selector = str(Path.cwd().resolve())
    scoped = dict(reg)
    for key in ("threads", "archived"):
        items = projects.filter_by_projects(reg.get(key, []), [selector])
        scoped[key] = _filter_source_recent(items, now, args)
    return scoped


def _status_selection(args) -> overview.StatusSelection:
    statuses = tuple(args.status or ())
    if args.all or "all" in statuses:
        return overview.StatusSelection.all()
    if statuses:
        return overview.StatusSelection.filtered(statuses)
    return overview.StatusSelection.default()


def _json_view(reg: dict, now: str, args, selection: overview.StatusSelection) -> int:
    if args.snoozed:
        print(json.dumps(jsonout.snoozed_json(reg["threads"], now), indent=2))
        return 0
    if args.kept:
        print(json.dumps(jsonout.kept_json(reg["threads"], now), indent=2))
        return 0
    if args.filtered:
        print(json.dumps(jsonout.filtered_json(reg["threads"], now), indent=2))
        return 0
    if args.sessions:
        threads = _filter_source_recent(reg["threads"], now, args)
        threads = overview.filter_by_status(threads, now, selection)
        if selection.mode is overview.StatusMode.DEFAULT:
            threads = [t for t in threads if not overview.snooze_active(t, now)]
        print(json.dumps(jsonout.sessions_json(threads, now), indent=2))
        return 0
    print(json.dumps(jsonout.rollup_json(reg, now, selection=selection), indent=2))
    return 0


def _alt_view(reg: dict, now: str, args, selectors) -> str | None:
    """The alternate list views that replace the default rollup, in precedence order.
    Returns the rendered text, or None to fall through to the normal views.
    --archived honours a selector; --snoozed/--kept span all threads."""
    if args.archived:
        return overview.render_archived(reg, selectors[0] if selectors else None)
    if args.snoozed:
        return overview.render_snoozed(reg["threads"], now)
    if args.kept:
        return overview.render_kept(reg["threads"], now)
    if args.filtered:
        return overview.render_filtered(reg["threads"], now)
    return None


def _scope_filtered(reg: dict, args) -> dict:
    """Hide name-filtered sessions from every view built below this point. They stay in
    the registry (and on disk) — --filtered inspects them, --include-filtered unhides."""
    if args.include_filtered:
        return reg
    wanted = bool(args.filtered)
    scoped = dict(reg)
    scoped["threads"] = [t for t in reg["threads"] if overview.is_filtered(t) is wanted]
    return scoped


def _print_sessions_view(reg: dict, now: str, args, selection: overview.StatusSelection) -> None:
    threads = _filter_source_recent(reg["threads"], now, args)
    snoozed_total = sum(1 for t in threads if overview.snooze_active(t, now))
    scoped = dict(reg)
    if selection.mode is overview.StatusMode.DEFAULT:
        scoped["threads"] = [t for t in threads if not overview.snooze_active(t, now)]
    else:
        scoped["threads"] = threads
    print(overview.render(scoped, now, selection=selection))
    if selection.mode is overview.StatusMode.DEFAULT and snoozed_total:
        print(f"\n({snoozed_total} snoozed hidden — rl overview -s --status all)")


def _overview_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="rl overview",
        description="The session view: project rollup (default), --sessions list, "
        "a <selector> deep-dive, --archived, or --snoozed.",
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
        "--cwd",
        action="store_true",
        help="scope the report to the current directory and its descendants",
    )
    ap.add_argument("-s", "--sessions", action="store_true", help="flat per-session list")
    ap.add_argument(
        "--status",
        action="append",
        choices=(*overview.FILTERABLE_STATUSES, "all"),
        help="filter by effective status; repeatable (OR), or 'all' for every non-archived session",
    )
    ap.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="legacy alias for --status all",
    )
    ap.add_argument(
        "--archived", action="store_true", help="list archived sessions instead of live ones"
    )
    ap.add_argument(
        "--snoozed",
        action="store_true",
        help="list currently-snoozed sessions instead of live ones",
    )
    ap.add_argument(
        "--kept",
        action="store_true",
        help="list sessions kept long-term (out of triage for good)",
    )
    ap.add_argument(
        "--filtered",
        action="store_true",
        help="list sessions hidden by a `rl config filter` name pattern",
    )
    ap.add_argument(
        "--include-filtered",
        action="store_true",
        help="also show sessions hidden by a `rl config filter` name pattern",
    )
    ap.add_argument(
        "--checkouts", action="store_true", help="rollup only: also list each repo's checkout paths"
    )
    ap.add_argument(
        "--source",
        action="append",
        default=None,
        help="filter to sessions from this source (repeatable)",
    )
    ap.add_argument(
        "--no-scan",
        action="store_true",
        help="do not auto-scan disk first (report from the registry as-is)",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="print JSON instead of text: a session array with --sessions/--snoozed/--kept, "
        "otherwise the registry-wide rollup; ignores the selector and --archived",
    )
    ap.add_argument(
        "--recent",
        type=int,
        default=None,
        metavar="DAYS",
        help="only sessions active within the last DAYS",
    )
    return ap


def cmd_overview(argv: list[str]) -> int:
    parser = _overview_parser()
    args = parser.parse_args(argv)
    if args.cwd and (args.selector or args.project):
        parser.error("--cwd cannot be combined with a selector or --project")
    selection = _status_selection(args)

    now = _now()
    if args.no_scan:
        reg = registry.load_registry(args.registry)
    else:
        reg, note = refresh.auto_refresh(args.registry, now)
        if note:
            print(note + "\n")

    if args.cwd:
        reg = _scope_to_cwd(reg, now, args)
    reg = _scope_filtered(reg, args)

    if args.json:
        return _json_view(reg, now, args, selection)

    selectors = list(args.project or [])
    if args.selector:
        selectors.append(args.selector)

    alt = _alt_view(reg, now, args, selectors)
    if alt is not None:
        print(alt)
        return 0

    # triage bridge (top-level views only, not the selector deep-dive)
    if not selectors:
        n = overview.triage_count(reg["threads"], now)
        if n:
            print(f"⚠ {n} awaiting triage — rl triage\n")

    if args.cwd:
        _print_sessions_view(reg, now, args, selection)
    elif selectors:
        scoped = dict(reg)
        scoped["threads"] = projects.filter_by_projects(reg["threads"], selectors)
        print(overview.project_detail(scoped, now, selection=selection))
    elif args.sessions:
        _print_sessions_view(reg, now, args, selection)
    else:
        print(overview.render_rollup(reg, now, selection=selection, checkouts=args.checkouts))
    return 0
