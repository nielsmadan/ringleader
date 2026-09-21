from __future__ import annotations

import contextlib
import shutil
import sys
from pathlib import Path
from typing import Any

from . import overview, registry, scanner

DEFAULT_OLDER_THAN_DAYS = 7
DEFAULT_MAX_TURNS = 12


def prune_candidates(
    reg: dict[str, Any],
    now_iso: str,
    *,
    older_than_days: int = DEFAULT_OLDER_THAN_DAYS,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in reg["threads"]:
        if t.get("decision") in ("keep", "harvested"):
            continue
        if overview.age_days(t["mtime"], now_iso) < older_than_days:
            continue
        # Tininess is message-count-driven, not byte-driven: sources differ wildly in
        # bytes-per-message (Codex transcripts embed full request/response payloads),
        # so byte size stays displayed but no longer gates eligibility.
        small = (t.get("messageCount") or 0) < max_turns
        if small:
            out.append(t)
    return out


def _delete_candidate(reg: dict[str, Any], candidate: dict[str, Any], now_iso: str) -> bool:
    thread = registry.find_session(reg, registry.qualified_id(candidate))
    fields = ("decision", "file", "sourceRef", "mtime", "messageCount", "project")
    if thread is None:
        print(f"skipped {candidate['sessionId']}: session changed since selection", file=sys.stderr)
        return False
    try:
        file = scanner.filesystem_path(thread, "prune")
        if thread.get("decision") in {"keep", "harvested"} or any(
            thread.get(field) != candidate.get(field) for field in fields
        ):
            print(
                f"skipped {candidate['sessionId']}: session changed since selection",
                file=sys.stderr,
            )
            return False
        companions = scanner.companion_paths(thread.get("source"), file)
        scanner.hide_from_picker(thread)
        file.unlink(missing_ok=True)
        scanner.remove_companions(companions)
    except (RuntimeError, OSError) as error:
        print(f"skipped {thread['sessionId']}: {error}", file=sys.stderr)
        return False
    registry.prune_session(reg, registry.qualified_id(thread), now_iso)
    return True


def delete_sessions(reg: dict[str, Any], candidates: list[dict[str, Any]], now_iso: str) -> int:
    deleted = 0
    for candidate in candidates:
        if isinstance(reg, registry.Registry) and reg.path is not None:

            def remove(current: dict[str, Any], candidate: dict[str, Any] = candidate) -> None:
                nonlocal deleted
                deleted += _delete_candidate(current, candidate, now_iso)

            current = registry.mutate_registry(reg.path, remove)
            reg.replace(current)
        else:
            deleted += _delete_candidate(reg, candidate, now_iso)
    return deleted


def archive_purge_candidates(
    reg: dict[str, Any], now_iso: str, *, retention_days: int
) -> list[dict[str, Any]]:
    if retention_days <= 0:
        return []
    out: list[dict[str, Any]] = []
    for ts in reg.get("archived", []):
        archived = ts.get("archivedAt")
        if not archived:
            continue
        try:
            aged_out = overview.age_days(archived, now_iso) >= retention_days
        except ValueError:
            continue  # unparseable timestamp -> never ages out (safe direction)
        if aged_out:
            out.append(ts)
    return out


def purge_archived(
    reg: dict[str, Any], candidates: list[dict[str, Any]], now_iso: str, *, archive_root_path: str
) -> int:
    root = Path(archive_root_path).resolve()
    purged = 0
    for ts in candidates:
        archive_path = ts.get("archivePath")
        if archive_path:
            lifecycle_thread = ts.get("thread") or {
                "sessionId": ts.get("sessionId"),
                "source": ts.get("source") or "claude-code",
                "file": ts.get("originalPath") or archive_path,
            }
            try:
                scanner.filesystem_path(lifecycle_thread, "purge")
            except RuntimeError as error:
                print(f"skipped archived {ts['sessionId']}: {error}", file=sys.stderr)
                continue
            rp = Path(archive_path).resolve()
            # Never delete outside the archive tree: registry.json is hand-editable,
            # and these deletes are irreversible last-copy data.
            if not rp.is_relative_to(root) or rp.suffix != ".jsonl":
                print(
                    f"skipped archived {ts['sessionId']}: {archive_path} is not an "
                    f"archive-root .jsonl transcript",
                    file=sys.stderr,
                )
                continue
            companions = scanner.companion_paths(lifecycle_thread.get("source"), rp)
            # Best-effort per artifact so one undeletable file can't strand the tombstone.
            for target in (rp, *companions, rp.with_suffix(".md")):
                with contextlib.suppress(OSError):
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target, ignore_errors=True)  # <id>/ sidecar dir
                    else:
                        target.unlink(missing_ok=True)  # <id>.jsonl / <id>.md
            with contextlib.suppress(OSError):
                rp.parent.rmdir()  # drop the slug dir only if it is now empty
        registry.purge_archived_session(reg, registry.qualified_id(ts), now_iso)
        purged += 1
    return purged
