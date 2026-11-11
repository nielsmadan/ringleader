from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .. import gitinfo, registry, scanner
from ..agents import source_of
from ..slug import normalize
from ..sources.base import IndexUpdate, WorkspaceRebindingSource, WorkspaceRelocatingSource
from ..stateio import publish_lock
from . import default_registry


def _follow_storage(thread: dict[str, Any], new_path: str) -> tuple[str | None, bool]:
    """Take the agent's own session storage to `new_path`, the way `rl workspace
    retarget` does for a whole directory. Returns the session's new transcript path
    (None when the transcript did not move) and whether the registry may follow.

    A source with neither capability is not a failure: the registry record is still
    worth re-pointing, it just leaves the session where the agent's picker had it."""
    source = source_of(thread)
    sid = thread["sessionId"]
    if not thread.get("file"):
        print(f"registry only — no transcript recorded for {sid[:8]}")
        return None, True
    rebinding = scanner.sources_with("rebinds_workspace", WorkspaceRebindingSource)
    if source in rebinding:
        try:
            outcome = rebinding[source].rebind_workspace(sid, thread["file"], new_path)
        except (RuntimeError, OSError) as e:
            print(f"rl-relocate: {e}", file=sys.stderr)
            return None, False
        if outcome is IndexUpdate.NO_INDEX:
            # Refuse rather than pin: the index is usually missing only temporarily,
            # and a pinned thread is one `rl workspace retarget` can never find again.
            print(f"rl-relocate: {source} has no session index here", file=sys.stderr)
            return None, False
        if outcome is IndexUpdate.NO_ENTRY:
            print(
                f"rl-relocate: no {source} index entry for {thread['file']} "
                "— it stays resumable by id",
                file=sys.stderr,
            )
        return None, True

    relocating = scanner.sources_with("relocates_workspace", WorkspaceRelocatingSource)
    if source in relocating:
        src = relocating[source]
        # Already there: re-running after a partial failure is the recovery path, and
        # relocate_session refuses a destination that exists — including its own result.
        if src.storage_dir(thread["file"]) == src.storage_for(new_path):
            return None, True
        try:
            return src.relocate_session(thread["file"], new_path), True
        except (RuntimeError, OSError) as e:
            print(f"rl-relocate: {e}", file=sys.stderr)
            return None, False

    print(f"registry only — {source} storage cannot follow a session")
    return None, True


def cmd_relocate(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl session relocate",
        description="Re-point ONE session at a new directory after you moved it on disk: "
        "its registry record, and the agent's own session storage, so the session stays "
        "resumable from the new path. Use `rl workspace retarget` for a whole directory.",
    )
    ap.add_argument("id")
    ap.add_argument("path", help="the session's new directory (absolute)")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument(
        "--registry-only",
        action="store_true",
        help="re-point the record only, leaving the agent's session storage alone",
    )
    args = ap.parse_args(argv)

    new_path = normalize(args.path)
    if not Path(new_path).is_dir():
        print(f"rl-relocate: {new_path} is not a directory", file=sys.stderr)
        return 1

    # Resolved before the lock and before any storage move: it shells out to git, and a
    # failure there must not land between a moved transcript and the record naming it.
    gp = gitinfo.resolve_git_project(new_path)
    git = (
        {
            "key": gp.key,
            "label": gp.label,
            "root": gp.root,
            "remote": gp.remote,
            "clone_url": gp.clone_url,
        }
        if gp
        else None
    )

    with publish_lock():
        reg = registry.load_registry(args.registry)
        thread = registry.find_session(reg, args.id)
        if thread is None:
            print(f"rl-relocate: unknown session {args.id}", file=sys.stderr)
            return 1
        was = thread.get("project") or ""
        moved_to = None
        if not args.registry_only:
            moved_to, ok = _follow_storage(thread, new_path)
            if not ok:
                return 1
        registry.pin_thread_location(thread, new_path, git)
        registry.remap_cached_git_root(thread, was, new_path)
        if moved_to is not None:
            thread["file"] = moved_to
        registry.commit_registry(args.registry, reg)

    where = f"{gp.label} ({new_path})" if gp else new_path
    print(f"relocated {args.id} -> {where}")
    return 0
