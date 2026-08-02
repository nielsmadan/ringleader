from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

from .. import tokens
from ..paths import archive_root


def _window(args):
    """Return (since, until) as YYYY-MM-DD strings, or (None, None) for --all.
    Dates come from the transcript timestamps, so 'today' is the latest cell,
    not the wall clock — this stays correct in tests and offline."""
    if args.all:
        return None, None
    if args.since or args.until:
        return args.since, args.until
    # default: trailing --days window ending at the latest activity we can see
    return "__last_days__", None


def _resolve_window(cells, args):
    since, until = _window(args)
    if since == "__last_days__":
        latest = max((c.date for c in cells.values()), default=None)
        if latest is None:
            return None, None
        end = date.fromisoformat(latest)
        start = end - timedelta(days=args.days - 1)
        return start.isoformat(), None
    return since, until


def _in_window(d, since, until):
    if since and d < since:
        return False
    if until and d > until:
        return False
    return True


def cmd_tokens(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl tokens",
        description="Claude token spend per day/project/session, bucketed by real "
        "message date (fixes ccusage's resumed-session mis-attribution). "
        "This machine only; API-list-price estimate.",
    )
    ap.add_argument("--days", type=int, default=7, help="trailing window size (default 7)")
    ap.add_argument("--since", help="YYYY-MM-DD lower bound (inclusive)")
    ap.add_argument("--until", help="YYYY-MM-DD upper bound (inclusive)")
    ap.add_argument("--all", action="store_true", help="all time, ignore the window")
    ap.add_argument("--by", choices=["day", "project", "session"], default="project",
                    help="grouping for the ranked table (default project)")
    ap.add_argument("--archived", action="store_true",
                    help="also scan archive/ (sessions st has moved out of ~/.claude)")
    ap.add_argument("--limit", type=int, default=15, help="rows in the ranked table")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    extra = [str(archive_root())] if args.archived else []
    cells = tokens.collect(extra_roots=extra)
    since, until = _resolve_window(cells, args)

    scoped = [c for c in cells.values() if _in_window(c.date, since, until)]
    total = sum(c.b.cost for c in scoped)

    per_day = tokens.rollup({id(c): c for c in scoped}, key=lambda c: c.date)

    if args.by == "day":
        rows = per_day
    elif args.by == "session":
        rows = tokens.rollup(
            {id(c): c for c in scoped},
            key=lambda c: f"{c.session_id[:8]}  {c.project}")
    else:
        rows = tokens.rollup({id(c): c for c in scoped}, key=lambda c: c.project)

    if args.json:
        out = {
            "since": since,
            "until": until,
            "total_cost": round(total, 2),
            "per_day": {d: round(b.cost, 2) for d, b in sorted(per_day.items())},
            "by": args.by,
            "rows": [
                {"key": k, "cost": round(b.cost, 2), "tokens": b.total_tokens,
                 "output_tokens": b.output_tokens}
                for k, b in sorted(rows.items(), key=lambda kv: -kv[1].cost)
            ],
        }
        print(json.dumps(out, indent=2))
        return 0

    label = "all time" if args.all else f"{since} .. {until or 'latest'}"
    print(f"Claude spend — this machine — {label}")
    print(f"  estimate: ${total:,.2f}   (API list prices; relative ranking is exact)")
    print()
    print("  by day")
    for d, b in sorted(per_day.items()):
        print(f"    {d}  ${b.cost:8,.2f}")
    print()
    print(f"  by {args.by}")
    ranked = sorted(rows.items(), key=lambda kv: -kv[1].cost)[: args.limit]
    for k, b in ranked:
        print(f"    ${b.cost:8,.2f}  {k}")
    if len(rows) > args.limit:
        print(f"    … {len(rows) - args.limit} more")
    return 0
