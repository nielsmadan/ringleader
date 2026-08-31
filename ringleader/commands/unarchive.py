from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .. import registry, scanner
from ..sources.base import IndexUpdate
from . import default_registry


def _archived(reg: dict, session_id: str) -> dict | None:
    for ts in reg.get("archived", []):
        if ts["sessionId"] == session_id:
            return ts
    return None


def restore(reg: dict, ts: dict) -> tuple[str, IndexUpdate]:
    """Put an archived transcript back and clear its archived record.

    Ordering is deliberately the reverse of archiving: the agent's picker index still
    points at the ORIGINAL path and the update is pinned to it, so the file has to be
    back in place before the index is touched. Archiving has to do the opposite (index
    first — Codex refuses to archive a session whose rollout is already gone), so don't
    "harmonise" the two.

    Returns (restored path, picker outcome). Raises RuntimeError if it can't move."""
    session_id = ts["sessionId"]
    original = ts.get("originalPath")
    if not original:
        raise RuntimeError(
            f"{session_id} was archived before ringleader recorded its original path; "
            "move the transcript back by hand (README → Restoring an archived session)"
        )
    thread = ts.get("thread") or {
        "sessionId": session_id,
        "source": ts.get("source") or "claude-code",
        "file": original,
    }
    scanner.filesystem_path(thread, "unarchive")
    src = Path(ts["archivePath"])
    dest = Path(original)
    if dest.exists():
        raise RuntimeError(f"{dest} already exists — not overwriting")
    if not src.exists():
        raise RuntimeError(f"archived transcript is gone: {src}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    sidecar = src.with_suffix("")
    if sidecar.exists():
        shutil.move(str(sidecar), str(dest.with_suffix("")))

    shown = scanner.show_in_picker(thread, str(dest))
    # The workspace may have moved while this session sat in the archive, in which case
    # its registry project is already the new path but the agent's index is not.
    project = thread.get("project")
    if project:
        scanner.rebind_workspace(thread, str(dest), project)

    registry.restore_session(reg, session_id, str(dest))
    if ts.get("thread"):
        # belt and braces: `archive_session` strips the finish mark before storing, but
        # a record archived by an earlier build still carries it
        restored = registry.without_finish_mark(ts["thread"])
        restored["file"] = str(dest)
        reg["threads"].append(restored)
    return str(dest), shown


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
        dest, shown = restore(reg, ts)
    except RuntimeError as e:
        print(f"rl-unarchive: {e}", file=sys.stderr)
        return 1
    registry.save_registry(args.registry, reg)

    print(f"restored {args.id} -> {dest}")
    if shown is IndexUpdate.NO_ENTRY:
        # A half-restore that claims success is the worst outcome: the transcript is
        # back but the session stays invisible in the agent's own picker. Only NO_ENTRY
        # means that — a source with no index of its own has nothing to restore.
        print(
            "  note: restored on disk, but the agent's picker entry could not be "
            "updated (it may have been archived by the agent itself)",
            file=sys.stderr,
        )
    return 0
