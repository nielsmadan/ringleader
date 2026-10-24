from __future__ import annotations

import argparse
import sys
from typing import Any

from .. import lifecycle, registry
from ..sources.base import IndexUpdate
from . import default_registry


def _archived(reg: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    return registry.find_session(reg, session_id, bucket="archived")


def report_restored(session_id: str, dest: str, shown: IndexUpdate) -> None:
    print(f"restored {session_id} -> {dest}")
    if shown is IndexUpdate.NO_ENTRY:
        print(
            "  note: restored on disk, but the agent's picker entry could not be "
            "updated (it may have been archived by the agent itself)",
            file=sys.stderr,
        )


def cmd_unarchive(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl session unarchive",
        description="Undo `rl session archive`: move the transcript back where it was "
        "so the agent can resume it again, and drop the tombstone.",
    )
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    args = ap.parse_args(argv)

    reg = registry.load_registry(args.registry)
    ts = _archived(reg, args.id)
    if ts is None:
        print(f"rl-unarchive: no archived session {args.id}", file=sys.stderr)
        return 1
    try:
        dest, shown = lifecycle.restore(reg, ts)
    except RuntimeError as e:
        print(f"rl-unarchive: {e}", file=sys.stderr)
        return 1
    registry.commit_registry(args.registry, reg)

    report_restored(args.id, dest, shown)
    return 0
