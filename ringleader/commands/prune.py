from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from .. import overview, projects, prune, registry, scanner
from ..config import load_config
from ..paths import archive_root
from . import default_registry


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _label(t: dict) -> str:
    return t.get("title") or t.get("suggestedName") or t["sessionId"][:8]


def stub_candidates(reg: dict, sessions) -> list:
    """Transcripts on disk whose session was already archived — recreated by an agent
    that wrote after the archive moved the file."""
    archived = {x["sessionId"] for x in reg.get("archived", [])}
    return [s for s in sessions if s.session_id in archived]


def _project_shim(s) -> dict:
    """`projects.filter_by_projects` matches against registry-shaped dicts; a stub
    candidate is a `NormalizedSession` from a live scan, so translate its identity
    fields into the same shape rather than duplicating `matches()`'s logic."""
    return {
        "project": s.cwd,
        "gitProjectLabel": s.git_project_label,
        "gitProjectKey": s.git_project_key,
        "gitRoot": s.git_root,
    }


def _scope_stubs(stubs: list, selectors) -> list:
    pairs = [(s, _project_shim(s)) for s in stubs]
    kept = {
        id(shim) for shim in projects.filter_by_projects([shim for _, shim in pairs], selectors)
    }
    return [s for s, shim in pairs if id(shim) in kept]


def _find_stubs(reg: dict, selectors=None) -> list:
    if not reg.get("archived"):
        return []
    scanned = scanner.scan(sources=scanner.active_sources(), cached_threads=reg["threads"])
    stubs = stub_candidates(reg, scanned)
    if selectors:
        stubs = _scope_stubs(stubs, selectors)
    return stubs


def _report_stubs(stubs: list) -> None:
    for s in stubs:
        print(f"  resurrected {s.session_id[:8]}  {s.file}  (transcript recreated after archive)")
    print(f"\n{len(stubs)} resurrected archived transcript(s)")


def _delete_stubs(stubs: list) -> int:
    removed = 0
    for s in stubs:
        thread = {"source": s.source, "sessionId": s.session_id, "file": s.file}
        try:
            f = scanner.filesystem_path(thread, "prune")
            companions = scanner.companion_paths(s.source, f)
            # same order as prune.delete_sessions: deleting first would strand a live
            # entry in the agent's picker, pointing at a transcript that is gone
            scanner.hide_from_picker(thread)
        except RuntimeError as e:
            print(f"skipped {s.session_id}: {e}", file=sys.stderr)
            continue
        try:
            f.unlink(missing_ok=True)
            scanner.remove_companions(companions)
        except OSError as e:
            print(f"skipped {s.session_id}: {e}", file=sys.stderr)
            continue
        removed += 1
    return removed


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

    stubs = _find_stubs(reg, args.project)

    if not cands and not arch and not stubs:
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
            archived_at = ts.get("archivedAt")
            if not isinstance(archived_at, str):
                continue
            age = round(overview.age_days(archived_at, now))
            print(
                f"  archived {archived_at[:10]}  {ts.get('project') or '(unknown)'}"
                f"  {ts['sessionId'][:8]}  ({age}d, past {retention}d retention)"
            )
        print(
            f"\n{len(arch)} archived session(s) past retention (archive.retention_days={retention})"
        )

    if stubs:
        _report_stubs(stubs)

    if not args.delete:
        print("(preview — re-run with --delete to remove these)")
        return 0

    parts = []
    if cands:
        parts.append(f"delete {len(cands)} sessions ({overview._human_size(total)})")
    if arch:
        parts.append(f"purge {len(arch)} archived")
    if stubs:
        parts.append(f"remove {len(stubs)} resurrected archived transcript(s)")
    try:
        resp = input(f"Proceed to {' and '.join(parts)}? [y/N] ").strip().lower()
    except EOFError:
        resp = ""
    if resp not in ("y", "yes"):
        print("aborted, nothing deleted")
        return 0

    deleted = prune.delete_sessions(reg, cands, now)
    purged = prune.purge_archived(reg, arch, now, archive_root_path=str(archive_root()))
    removed_stubs = _delete_stubs(stubs)
    skipped = len(cands) - deleted
    registry.save_registry(args.registry, reg)
    print(
        f"deleted {deleted} sessions, skipped {skipped}, purged {purged} archived, "
        f"removed {removed_stubs} resurrected"
    )
    return 0
