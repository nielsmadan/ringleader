from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import Any

from .. import naming, overview, projects, registry, scanner
from ..sources.base import NameApplyingSource
from . import default_registry


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _can_name(t: dict[str, Any]) -> bool:
    src = scanner.source_by_name(t.get("source") or "claude-code")
    return bool(src and "applies_names" in src.capabilities and isinstance(src, NameApplyingSource))


def cmd_name(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session name")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument(
        "--project", action="append", default=None, help="repo/dir selector; repeatable"
    )
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--active-window", type=float, default=naming.ACTIVE_WINDOW_MINUTES)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    now = _now()
    reg = registry.load_registry(args.registry)
    selected = naming.select_eligible(reg, now, active_window_minutes=args.active_window)

    cap_skipped = [s for s in selected if not _can_name(s[0])]
    selected = [s for s in selected if _can_name(s[0])]

    if args.project:
        selected = [s for s in selected if any(projects.matches(s[0], sel) for sel in args.project)]
    if args.limit is not None:
        selected = selected[: args.limit]

    if args.dry_run:
        for t, name, kind in selected:
            label = overview.group_label(t)
            if kind == "new":
                print(f"new:    {name}  ({label}) — {t.get('whatItIs') or ''}")
            else:
                print(f"rename: {t['title']} -> {name}  ({label})")
        print(f"({len(selected)} to apply)")
        return 0

    new_n = ren_n = skipped = suffixed = 0
    for t, name, kind in selected:
        src = scanner.source_by_name(t.get("source") or "claude-code")
        try:
            if not isinstance(src, NameApplyingSource):
                raise OSError("unknown session source")
            src.apply_name(t["sessionId"], t["file"], name)
        except (OSError, ValueError) as e:
            print(f"skipped {t['sessionId']}: {e}", file=sys.stderr)
            skipped += 1
            continue
        registry.apply_name(reg, t["sessionId"], name, source=t.get("source") or "claude-code")
        if name != t.get("suggestedName"):
            suffixed += 1
        if kind == "new":
            new_n += 1
        else:
            ren_n += 1
    registry.commit_registry(args.registry, reg)
    print(
        f"named {new_n}, renamed {ren_n}, "
        f"skipped {skipped} ({len(cap_skipped)} no-name-support), suffixed {suffixed}"
    )
    return 0
