from __future__ import annotations

import argparse
from datetime import UTC, datetime

from .. import overview, projects, prune, registry
from ..config import load_config
from ..paths import archive_root
from . import default_registry


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _label(t: dict) -> str:
    return t.get("title") or t.get("suggestedName") or t["sessionId"][:8]


def cmd_prune(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl prune")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument(
        "--project",
        action="append",
        default=None,
        help="repo/dir selector; repeatable — scope the prune",
    )
    ap.add_argument(
        "--older-than",
        type=int,
        default=prune.DEFAULT_OLDER_THAN_DAYS,
        help="minimum age in days (default 7)",
    )
    ap.add_argument(
        "--max-turns",
        type=int,
        default=prune.DEFAULT_MAX_TURNS,
        help="'tiny' message-count ceiling (default 12)",
    )
    ap.add_argument(
        "--delete", action="store_true", help="actually delete (otherwise preview only)"
    )
    args = ap.parse_args(argv)

    now = _now()
    reg = registry.load_registry(args.registry)
    cands = prune.prune_candidates(
        reg,
        now,
        older_than_days=args.older_than,
        max_turns=args.max_turns,
    )
    if args.project:
        cands = projects.filter_by_projects(cands, args.project)

    retention = load_config().archive_retention_days
    arch = prune.archive_purge_candidates(reg, now, retention_days=retention)
    if args.project:
        arch = projects.filter_by_projects(arch, args.project)

    if not cands and not arch:
        print("nothing to prune")
        return 0

    total = sum(t.get("sizeBytes") or 0 for t in cands)
    if cands:
        cands.sort(key=lambda t: overview.age_days(t["mtime"], now), reverse=True)
        for t in cands:
            age = round(overview.age_days(t["mtime"], now))
            print(
                f"  [{overview.effective_status(t, now)}] {_label(t)} "
                f"({age}d, {overview._human_size(t.get('sizeBytes'))}, "
                f"{t.get('messageCount') or 0} msgs)  {t.get('project') or ''}"
            )
        print(f"\n{len(cands)} sessions, {overview._human_size(total)}")

    if arch:
        arch.sort(key=lambda ts: ts.get("archivedAt") or "")
        for ts in arch:
            age = round(overview.age_days(ts.get("archivedAt"), now))
            print(
                f"  archived {(ts.get('archivedAt') or '')[:10]}  {ts.get('project') or '(unknown)'}"
                f"  {ts['sessionId'][:8]}  ({age}d, past {retention}d retention)"
            )
        print(
            f"\n{len(arch)} archived session(s) past retention (archive.retention_days={retention})"
        )

    if not args.delete:
        print("(preview — re-run with --delete to remove these)")
        return 0

    parts = []
    if cands:
        parts.append(f"delete {len(cands)} sessions ({overview._human_size(total)})")
    if arch:
        parts.append(f"purge {len(arch)} archived")
    try:
        resp = input(f"Proceed to {' and '.join(parts)}? [y/N] ").strip().lower()
    except EOFError:
        resp = ""
    if resp not in ("y", "yes"):
        print("aborted, nothing deleted")
        return 0

    deleted = prune.delete_sessions(reg, cands, now)
    purged = prune.purge_archived(reg, arch, now, archive_root_path=str(archive_root()))
    skipped = len(cands) - deleted
    registry.save_registry(args.registry, reg)
    print(f"deleted {deleted} sessions, skipped {skipped}, purged {purged} archived")
    return 0
