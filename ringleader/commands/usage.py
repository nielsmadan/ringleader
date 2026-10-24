from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..paths import archive_root
from ..usage import aggregate
from ..usage.base import UsageReader, UsageRecord, WindowSnapshot, record_day, window_is_current
from ..usage.codex import CodexUsageReader

_KEYS = {
    "day": record_day,
    "project": lambda r: r.project or "(unknown)",
    "session": lambda r: f"{r.session_id[:8]}  {r.project or ''}".strip(),
    "model": lambda r: r.model or "(unknown)",
    "source": lambda r: r.source,
}


def _records_from(
    readers: Sequence[UsageReader], since: date | None = None, session_prefix: str | None = None
) -> list[UsageRecord]:
    return [
        r for reader in readers for r in reader.records(since=since, session_prefix=session_prefix)
    ]


def collect(
    sources: Iterable[str],
    dirs: dict[str, dict[str, Any]] | None = None,
    since: date | None = None,
    session_prefix: str | None = None,
) -> list[UsageRecord]:
    readers = aggregate.readers(sources, dirs)
    records = _records_from(readers, since, session_prefix)
    aggregate.flush_caches(readers)
    return records


def _window_start(latest: str | None, days: int) -> date | None:
    return date.fromisoformat(latest) - timedelta(days=days - 1) if latest else None


def collect_archived(sources: list[str] | None = None) -> list[UsageRecord]:
    return aggregate.collect_archived(
        sources if sources is not None else aggregate.READERS, archive_root()
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _pooled(
    args: argparse.Namespace, sources: list[str], records: list[UsageRecord]
) -> list[UsageRecord]:
    if args.archived:
        from ..usage import transfer  # noqa: PLC0415

        records = transfer.dedupe(records + collect_archived(sources))
    if args.merge:
        from ..usage import transfer  # noqa: PLC0415

        records = transfer.dedupe(records + transfer.load_exports(args.merge))
    return records


def _gather(
    args: argparse.Namespace, sources: list[str]
) -> tuple[list[UsageRecord], date | None, date | None]:
    pooled = bool(args.archived or args.merge)
    if args.all:
        return _pooled(args, sources, collect(sources)), None, None
    if args.since or args.until:
        records = collect(sources, since=None if pooled else args.since)
        return _pooled(args, sources, records), args.since, args.until
    if pooled:
        records = _pooled(args, sources, collect(sources))
        start = _window_start(max((record_day(r) for r in records), default=None), args.days)
    else:
        readers = aggregate.readers(sources)
        start = _window_start(aggregate.latest_day(readers), args.days)
        records = _records_from(readers, since=start) if start else []
        aggregate.flush_caches(readers)
    return (records, start, None) if start else ([], None, None)


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


def _row(key: str, b: aggregate.Bucket, breakdown: bool) -> dict[str, Any]:
    row = {"key": key, "tokens": b.total, "records": b.records}
    if b.priced_records:
        row["cost"] = round(b.cost, 2)
    if b.cost_partial:
        row["cost_partial"] = True
    if breakdown:
        row.update(
            input=b.input,
            output=b.output,
            cache_read=b.cache_read,
            cache_write=b.cache_write,
            reasoning=b.reasoning,
        )
    return row


def _cmd_session(argv: list[str], prog: str) -> int:
    ap = argparse.ArgumentParser(prog=f"{prog} session")
    ap.add_argument("id")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    from ..usage.session import analyze  # noqa: PLC0415 -- keeps the hot path light

    records = [
        r
        for r in collect(sorted(aggregate.READERS), session_prefix=args.id)
        if r.session_id.startswith(args.id)
    ]
    if not records:
        print(f"no usage data for session {args.id}")
        return 1
    matches = sorted({(r.source, r.session_id) for r in records})
    if len(matches) > 1:
        aggregate.print_ambiguous(args.id, matches, 9)
        return 1
    rep = analyze(records)

    if args.json:
        print(
            json.dumps(
                {
                    "session_id": rep.session_id,
                    "source": rep.source,
                    "turns": rep.turns,
                    "main_turns": rep.main_turns,
                    "tokens": rep.totals.total,
                    "input": rep.totals.input,
                    "output": rep.totals.output,
                    "cache_read": rep.totals.cache_read,
                    "cache_write": rep.totals.cache_write,
                    "reasoning": rep.totals.reasoning,
                    "cache_hit_rate": rep.cache_hit_rate,
                    "cost": rep.cost,
                    "top_turns": rep.top_turns,
                    "context_growth": rep.context_growth,
                },
                indent=2,
            )
        )
        return 0

    nested = rep.turns - rep.main_turns
    turns = (
        f"{rep.turns} turns: {rep.main_turns} main, {nested} subagent"
        if nested
        else f"{rep.turns} turns"
    )
    scope = "main-thread turns" if nested else "turns"
    print(f"session {rep.session_id}  ({rep.source}, {turns})")
    print(f"  {rep.totals.total:,} tokens" + (f"   ~${rep.cost:,.2f}" if rep.cost else ""))
    print(
        f"  in={rep.totals.input:,}  out={rep.totals.output:,}  "
        f"cache_read={rep.totals.cache_read:,}  cache_write={rep.totals.cache_write:,}  "
        f"reasoning={rep.totals.reasoning:,}"
    )
    print(f"  cache hit rate: {rep.cache_hit_rate:.0%}")
    print()
    print(f"  top {scope} by tokens")
    for i, tot in rep.top_turns[:5]:
        print(f"    turn {i:>3}  {tot:>12,}")
    if rep.context_growth:
        print()
        print(
            f"  context: {rep.context_growth[0]:,} -> {rep.context_growth[-1]:,} tokens "
            f"over {len(rep.context_growth)} {scope}"
        )
    return 0


def collect_windows() -> list[WindowSnapshot]:
    reader = CodexUsageReader()
    return reader.latest_windows()


def collect_claude_windows() -> list[WindowSnapshot]:
    from ..usage import claude_limits  # noqa: PLC0415

    return claude_limits.read_windows(claude_limits.default_capture_path(), _now())


def _window_json(w: WindowSnapshot) -> dict[str, Any]:
    return {
        "used_percent": w.used_percent,
        "window_minutes": w.window_minutes,
        "resets_at": w.resets_at,
        "plan_type": w.plan_type,
        "as_of": w.timestamp,
    }


def _partition_windows(
    snapshots: Iterable[WindowSnapshot], now: datetime
) -> tuple[dict[str, WindowSnapshot], dict[str, WindowSnapshot], dict[str, WindowSnapshot]]:
    latest: dict[str, WindowSnapshot] = {}
    for snapshot in snapshots:
        previous = latest.get(snapshot.window)
        if previous is None or snapshot.timestamp > previous.timestamp:
            latest[snapshot.window] = snapshot
    current = {
        key: snapshot for key, snapshot in latest.items() if window_is_current(snapshot, now)
    }
    expired = {key: snapshot for key, snapshot in latest.items() if key not in current}
    return latest, current, expired


def _cmd_windows(argv: list[str], prog: str) -> int:
    ap = argparse.ArgumentParser(prog=f"{prog} windows")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    now = _now()
    latest, current, expired = _partition_windows(collect_windows(), now)
    claude_latest, claude_current, claude_expired = _partition_windows(
        collect_claude_windows(), now
    )

    if args.json:
        print(
            json.dumps(
                {
                    "codex": {k: _window_json(w) for k, w in current.items()},
                    "codex_expired": {k: _window_json(w) for k, w in expired.items()},
                    "claude": {k: _window_json(w) for k, w in claude_current.items()},
                    "claude_expired": {k: _window_json(w) for k, w in claude_expired.items()},
                },
                indent=2,
            )
        )
        return 0

    if not latest:
        print("no codex window data on disk")
    else:
        plan = next(iter(latest.values())).plan_type or "?"
        print(f"codex subscription windows (plan: {plan})")
        for kind in ("5h", "weekly"):
            snap = current.get(kind)
            if snap is not None:
                print(
                    f"  {kind:<7} {snap.used_percent:>5.1f}% used   resets {snap.resets_at[:16]}"
                    f"   (as of {snap.timestamp[:16]})"
                )
                continue
            old = expired.get(kind)
            if old is not None:
                print(f"  {kind:<7} no current snapshot (last one expired {old.resets_at[:16]})")
    print()
    if not claude_latest:
        print("claude: no windows captured yet — run `rl install statusline` to start capturing")
    else:
        print("claude subscription windows")
        for kind in ("5h", "weekly"):
            snap = claude_current.get(kind)
            if snap is not None:
                print(
                    f"  {kind:<7} {snap.used_percent:>5.1f}% used   resets {snap.resets_at[:16]}"
                    f"   (as of {snap.timestamp[:16]})"
                )
                continue
            old = claude_expired.get(kind)
            if old is not None:
                print(f"  {kind:<7} no current snapshot (last one expired {old.resets_at[:16]})")
    return 0


def _cmd_export(argv: list[str], prog: str) -> int:
    from ..usage import transfer  # noqa: PLC0415

    ap = argparse.ArgumentParser(prog=f"{prog} export")
    ap.add_argument("--out", default=f"usage-{transfer.machine_id()}.json")
    args = ap.parse_args(argv)

    records = collect(sorted(aggregate.READERS))
    p = transfer.write_export(records, args.out)
    print(f"exported {len(records)} records -> {p}")
    return 0


# dispatched from, and enumerated by `commands.command_paths()`
_ACTIONS: dict[str, Callable[[list[str], str], int]] = {
    "session": _cmd_session,
    "windows": _cmd_windows,
    "export": _cmd_export,
}
SUBCOMMANDS: tuple[str, ...] = tuple(_ACTIONS)


def cmd_usage(argv: list[str], prog: str = "rl usage") -> int:
    if argv and argv[0] in _ACTIONS:
        return _ACTIONS[argv[0]](argv[1:], prog)
    ap = argparse.ArgumentParser(
        prog=prog,
        description="Token usage across coding-agent harnesses, read from local agent state.",
    )
    ap.add_argument("--source", action="append", choices=sorted(aggregate.READERS))
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--since", type=aggregate.parse_day)
    ap.add_argument("--until", type=aggregate.parse_day)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--archived", action="store_true")
    ap.add_argument("--by", choices=sorted(_KEYS), default="project")
    ap.add_argument("--breakdown", action="store_true")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--merge")
    args = ap.parse_args(argv)

    sources = args.source or sorted(aggregate.READERS)
    records, since, until = _gather(args, sources)
    scoped = (
        [r for r in records if aggregate.in_window(r, since, until)] if since or until else records
    )

    if not scoped:
        if args.json:
            print(
                json.dumps(
                    {
                        "since": _iso(since),
                        "until": _iso(until),
                        "sources": sources,
                        "total_tokens": 0,
                        "total_cost": None,
                        "cost_partial": False,
                        "by": args.by,
                        "per_day": {},
                        "rows": [],
                    },
                    indent=2,
                )
            )
        else:
            print("no usage data")
        return 0

    per_day = aggregate.rollup(scoped, key=record_day)
    rows = aggregate.rollup(scoped, key=_KEYS[args.by])
    total_tokens = sum(b.total for b in rows.values())
    total_cost = sum(b.cost for b in rows.values())
    priced = sum(b.priced_records for b in rows.values())
    unpriced = sum(b.unpriced_records for b in rows.values())
    ranked = sorted(rows.items(), key=lambda kv: -kv[1].total)

    if args.json:
        print(
            json.dumps(
                {
                    "since": _iso(since),
                    "until": _iso(until),
                    "sources": sources,
                    "total_tokens": total_tokens,
                    "total_cost": round(total_cost, 2) if priced else None,
                    "cost_partial": bool(priced and unpriced),
                    "by": args.by,
                    "per_day": {d: b.total for d, b in sorted(per_day.items())},
                    "rows": [_row(k, b, args.breakdown) for k, b in ranked],
                },
                indent=2,
            )
        )
        return 0

    label = "all time" if args.all else f"{since} .. {until or 'latest'}"
    cost_note = ""
    if priced:
        cost_note = f"   ~${total_cost:,.2f}"
        if unpriced:
            cost_note += f" (partial: {unpriced:,} of {priced + unpriced:,} records unpriced)"
    print(f"usage — {', '.join(sources)} — {label}")
    print(f"  {total_tokens:,} tokens{cost_note}")
    print()
    print("  by day")
    for d, b in sorted(per_day.items()):
        print(f"    {d}  {b.total:>12,}")
    print()
    print(f"  by {args.by}")
    for k, b in ranked[: args.limit]:
        line = f"    {b.total:>12,}  {k}"
        if args.breakdown:
            line += (
                f"   in={b.input:,} out={b.output:,} "
                f"cr={b.cache_read:,} cw={b.cache_write:,} reas={b.reasoning:,}"
            )
        print(line)
    if len(ranked) > args.limit:
        print(f"    … {len(ranked) - args.limit} more")
    return 0
