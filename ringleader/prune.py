from __future__ import annotations

import contextlib
import shutil
import sys
from pathlib import Path

from . import overview, registry, scanner

DEFAULT_OLDER_THAN_DAYS = 7
DEFAULT_MAX_TURNS = 12


def prune_candidates(
    reg: dict,
    now_iso: str,
    *,
    older_than_days: int = DEFAULT_OLDER_THAN_DAYS,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> list[dict]:
    out = []
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


def delete_sessions(reg: dict, candidates: list[dict], now_iso: str) -> int:
    deleted = skipped = 0
    for t in candidates:
        try:
            f = scanner.filesystem_path(t, "prune")
            sidecar = f.with_suffix("")  # <uuid>.jsonl -> <uuid>/ dir
            scanner.hide_from_picker(t)
        except RuntimeError as e:
            print(f"skipped {t['sessionId']}: {e}", file=sys.stderr)
            skipped += 1
            continue
        try:
            f.unlink(missing_ok=True)
            shutil.rmtree(sidecar, ignore_errors=True)
        except OSError as e:
            print(f"skipped {t['sessionId']}: {e}", file=sys.stderr)
            skipped += 1
            continue
        registry.prune_session(reg, t["sessionId"], now_iso)
        deleted += 1
    return deleted


def archive_purge_candidates(reg: dict, now_iso: str, *, retention_days: int) -> list[dict]:
    if retention_days <= 0:
        return []
    out = []
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
    reg: dict, candidates: list[dict], now_iso: str, *, archive_root_path: str
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
            # Best-effort per artifact so one undeletable file can't strand the tombstone.
            for target in (rp, rp.with_suffix(""), rp.with_suffix(".md")):
                with contextlib.suppress(OSError):
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target, ignore_errors=True)  # <id>/ sidecar dir
                    else:
                        target.unlink(missing_ok=True)  # <id>.jsonl / <id>.md
            with contextlib.suppress(OSError):
                rp.parent.rmdir()  # drop the slug dir only if it is now empty
        registry.purge_archived_session(reg, ts["sessionId"], now_iso)
        purged += 1
    return purged
