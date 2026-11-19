from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from .. import jsonout, overview, projects, refresh, registry
from ..agents import source_of
from . import default_registry


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
        threads = [t for t in threads if (source_of(t)) in wanted]
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
