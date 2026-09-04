from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from .. import jsonout, registry, search, searchview, tty
from . import default_registry


def _limit(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be 1 or greater")
    return parsed


def cmd_search(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl session search",
        description="Find sessions by name. The query is a case-insensitive substring, "
        "or an anchored glob when it contains * or ?. Matches are ranked: the session's "
        "name first, then a session-id prefix, then the cached blurbs (ai title, "
        "what-it-is, summary, last prompt). Archived and name-filtered sessions are "
        "included and labelled. Reads the registry as it stands and never scans.",
    )
    ap.add_argument("query")
    ap.add_argument("--registry", default=str(default_registry()))
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--live", action="store_true", help="live sessions only")
    scope.add_argument("--archived", action="store_true", help="archived sessions only")
    ap.add_argument("--limit", type=_limit, default=50, help="rows to show (default 50)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = ap.parse_args(argv)

    if args.live:
        scope_choice = search.Scope.LIVE
    elif args.archived:
        scope_choice = search.Scope.ARCHIVED
    else:
        scope_choice = search.Scope.ALL

    now = datetime.now(UTC).isoformat()
    reg = registry.load_registry(args.registry)
    matches = search.search(reg, args.query, scope=scope_choice)

    if args.json:
        print(json.dumps(jsonout.search_json(matches, now, limit=args.limit), indent=2))
        return 0

    color = tty.want_color(args.color, sys.stdout, os.environ)
    print(searchview.render_search(matches, now, args.query, color=color, limit=args.limit))
    return 0
