from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from .. import lifecycle, registry
from ..paths import archive_root
from ..stateio import publish_lock
from . import default_registry


def cmd_archive(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session archive")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--archive-root", default=str(archive_root()))
    ap.add_argument(
        "--summary-file",
        default=None,
        help="markdown summary to store next to the archived transcript",
    )
    args = ap.parse_args(argv)

    with publish_lock():
        reg = registry.load_registry(args.registry)
        t = registry.find_session(reg, args.id)
        if t is None:
            print(f"rl-archive: unknown session {args.id}", file=sys.stderr)
            return 1

        try:
            result = lifecycle.archive_thread(
                reg,
                t,
                archive_root_path=args.archive_root,
                now_iso=datetime.now(UTC).isoformat(),
                summary_file=args.summary_file,
            )
        except RuntimeError as e:
            print(f"rl-archive: {e}", file=sys.stderr)
            return 1

        for warning in result.warnings:
            print(f"rl-archive: {warning}", file=sys.stderr)
        registry.commit_registry(args.registry, reg)
    dest = result.path
    summary_path = Path(dest).with_suffix(".md")
    extra = f" (+summary {summary_path.name})" if summary_path.exists() else ""
    print(f"archived {args.id} -> {dest}{extra}")
    return 0
