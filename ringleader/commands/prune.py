from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import overview, projects, prune, registry
from . import default_registry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _label(t: dict) -> str:
    return t.get("title") or t.get("suggestedName") or t["sessionId"][:8]


def cmd_prune(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl prune")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--project", action="append", default=None,
                    help="repo/dir selector; repeatable — scope the prune")
    ap.add_argument("--older-than", type=int, default=prune.DEFAULT_OLDER_THAN_DAYS,
                    help="minimum age in days (default 7)")
    ap.add_argument("--max-size", type=int, default=prune.DEFAULT_MAX_SIZE_BYTES // 1024,
                    help="'tiny' size ceiling in KB (default 50)")
    ap.add_argument("--max-turns", type=int, default=prune.DEFAULT_MAX_TURNS,
                    help="'tiny' message-count ceiling (default 12)")
    ap.add_argument("--delete", action="store_true",
                    help="actually delete (otherwise preview only)")
    args = ap.parse_args(argv)

    now = _now()
    reg = registry.load_registry(args.registry)
    cands = prune.prune_candidates(
        reg, now, older_than_days=args.older_than,
        max_size_bytes=args.max_size * 1024, max_turns=args.max_turns)
    if args.project:
        cands = projects.filter_by_projects(cands, args.project)

    if not cands:
        print("nothing to prune")
        return 0

    cands.sort(key=lambda t: overview.age_days(t["mtime"], now), reverse=True)
    total = sum(t.get("sizeBytes") or 0 for t in cands)
    for t in cands:
        age = round(overview.age_days(t["mtime"], now))
        print(f"  [{overview.effective_status(t, now)}] {_label(t)} "
              f"({age}d, {overview._human_size(t.get('sizeBytes'))}, "
              f"{t.get('messageCount') or 0} msgs)  {t.get('project') or ''}")
    print(f"\n{len(cands)} sessions, {overview._human_size(total)}")

    if not args.delete:
        print("(preview — re-run with --delete to remove these)")
        return 0

    try:
        resp = input(f"Delete {len(cands)} sessions "
                     f"({overview._human_size(total)})? [y/N] ").strip().lower()
    except EOFError:
        resp = ""
    if resp not in ("y", "yes"):
        print("aborted, nothing deleted")
        return 0

    deleted = skipped = 0
    for t in cands:
        f = Path(t["file"])
        sidecar = f.with_suffix("")        # <uuid>.jsonl -> <uuid>/ dir
        try:
            f.unlink(missing_ok=True)
            shutil.rmtree(sidecar, ignore_errors=True)
        except OSError as e:
            print(f"skipped {t['sessionId']}: {e}", file=sys.stderr)
            skipped += 1
            continue
        registry.prune_session(reg, t["sessionId"], now)
        deleted += 1
    registry.save_registry(args.registry, reg)
    print(f"deleted {deleted} sessions, skipped {skipped}")
    return 0
