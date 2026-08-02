from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import naming, overview, projects, registry
from . import default_registry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_title(file_path: str, line: str) -> None:
    p = Path(file_path)
    if p.stat().st_size == 0:
        lead = b""
    else:
        with p.open("rb") as fh:
            fh.seek(-1, 2)
            lead = b"" if fh.read(1) == b"\n" else b"\n"
    with p.open("ab") as fh:
        fh.write(lead + line.encode("utf-8") + b"\n")


def cmd_name(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl name")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--project", action="append", default=None,
                    help="repo/dir selector; repeatable")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--active-window", type=float, default=naming.ACTIVE_WINDOW_MINUTES)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    now = _now()
    reg = registry.load_registry(args.registry)
    selected = naming.select_eligible(reg, now, active_window_minutes=args.active_window)

    if args.project:
        selected = [s for s in selected
                    if any(projects.matches(s[0], sel) for sel in args.project)]
    if args.limit is not None:
        selected = selected[:args.limit]

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
        try:
            _append_title(t["file"], naming.title_line(t["sessionId"], name))
        except OSError as e:
            print(f"skipped {t['sessionId']}: {e}", file=sys.stderr)
            skipped += 1
            continue
        registry.apply_name(reg, t["sessionId"], name)
        if name != t.get("suggestedName"):
            suffixed += 1
        if kind == "new":
            new_n += 1
        else:
            ren_n += 1
    registry.save_registry(args.registry, reg)
    print(f"named {new_n}, renamed {ren_n}, skipped {skipped}, suffixed {suffixed}")
    return 0
