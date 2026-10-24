from __future__ import annotations

import contextlib
import shutil
import stat
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import overview, registry, revalidate, scanner
from .agents import source_of
from .batch import Txn, apply_batch, no_presence, run_batch
from .progress import CancelCheck, never_cancelled
from .revalidate import PresenceProbe

DEFAULT_OLDER_THAN_DAYS = 7
DEFAULT_MAX_TURNS = 12
DELETE_BATCH = 25
_SECONDS_PER_DAY = 86_400

type SkipHandler = Callable[[dict[str, Any], str], None]
type Guard = Callable[[dict[str, Any], PresenceProbe], str | None]


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


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def _disk_reason(candidate: dict[str, Any], file: Path) -> str | None:
    stamp = revalidate.content_stamp(candidate.get("fileFingerprint"))
    if stamp is None:
        return revalidate.NO_STAMP
    try:
        st = file.stat()
    except FileNotFoundError:
        return None
    return None if (st.st_size, st.st_mtime_ns) == stamp else "session changed since selection"


def _delete_candidate(
    txn: Txn,
    candidate: dict[str, Any],
    now_iso: str,
    *,
    guard: Guard | None,
    skip: Callable[[str], bool],
) -> bool:
    thread = txn.index.find(registry.qualified_id(candidate))
    fields = ("decision", "file", "sourceRef", "mtime", "messageCount", "project")
    if thread is None:
        return skip("session changed since selection")
    try:
        file = scanner.filesystem_path(thread, "prune")
        if thread.get("decision") in {"keep", "harvested"} or any(
            thread.get(field) != candidate.get(field) for field in fields
        ):
            return skip("session changed since selection")
        if changed := _disk_reason(candidate, file):
            return skip(changed)
        reason = guard(thread, txn.live) if guard is not None else None
        if reason:
            return skip(reason)
        companions = scanner.companion_paths(thread.get("source"), file)
        scanner.hide_from_picker(thread)
        file.unlink(missing_ok=True)
        scanner.remove_companions(companions)
    except (RuntimeError, OSError) as error:
        return skip(str(error))
    registry.prune_session(txn.reg, registry.qualified_id(thread), now_iso, index=txn.index)
    return True


def _delete_batch(  # noqa: PLR0913 -- one batch's shared state, passed down
    reg: dict[str, Any],
    batch: Sequence[dict[str, Any]],
    now_iso: str,
    *,
    report: Callable[[str], None],
    guard: Guard | None,
    presence: PresenceProbe,
    on_skip: SkipHandler | None,
) -> int:
    def skip(candidate: dict[str, Any], reason: str) -> bool:
        if on_skip is not None:
            on_skip(candidate, reason)
        else:
            report(f"skipped {candidate.get('sessionId')}: {reason}")
        return False

    def act(txn: Txn, candidate: dict[str, Any]) -> bool:
        return _delete_candidate(
            txn, candidate, now_iso, guard=guard, skip=lambda reason: skip(candidate, reason)
        )

    if isinstance(reg, registry.Registry) and reg.path is not None:
        run = run_batch(reg.path, batch, act, presence=presence)
        if run.error is not None:
            raise run.error
        if run.committed is not None:
            reg.adopt(run.committed)
        ran, stopped = run.ran, None
    else:
        applied = apply_batch(reg, batch, act, presence=presence)
        ran, stopped = applied.ran, applied.stopped
    for candidate, result in ran:
        if isinstance(result, Exception):
            skip(candidate, str(result))
    if stopped is not None:
        raise stopped
    return sum(result is True for _, result in ran)


def delete_sessions(  # noqa: PLR0913 -- injection points the suite drives
    reg: dict[str, Any],
    candidates: Sequence[dict[str, Any]],
    now_iso: str,
    *,
    report: Callable[[str], None] = _warn,
    cancelled: CancelCheck = never_cancelled,
    progress: Callable[[int, int], None] | None = None,
    guard: Guard | None = None,
    presence: PresenceProbe = no_presence,
    batch_size: int = DELETE_BATCH,
    on_skip: SkipHandler | None = None,
) -> int:
    if batch_size < 1:
        raise ValueError(f"batch_size must be at least 1, not {batch_size}")
    deleted = done = 0
    for start in range(0, len(candidates), batch_size):
        if cancelled():
            break
        batch = candidates[start : start + batch_size]
        deleted += _delete_batch(
            reg,
            batch,
            now_iso,
            report=report,
            guard=guard,
            presence=presence,
            on_skip=on_skip,
        )
        done += len(batch)
        if progress is not None:
            progress(done, len(candidates))
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
    reg: dict[str, Any],
    candidates: list[dict[str, Any]],
    now_iso: str,
    *,
    archive_root_path: str,
    report: Callable[[str], None] = _warn,
) -> int:
    root = Path(archive_root_path).resolve()
    index = registry.SessionIndex(reg)
    purged = 0
    for ts in candidates:
        archive_path = ts.get("archivePath")
        if archive_path:
            lifecycle_thread = ts.get("thread") or {
                "sessionId": ts.get("sessionId"),
                "source": source_of(ts),
                "file": ts.get("originalPath") or archive_path,
            }
            try:
                scanner.filesystem_path(lifecycle_thread, "purge")
            except RuntimeError as error:
                report(f"skipped archived {ts['sessionId']}: {error}")
                continue
            rp = Path(archive_path).resolve()
            # Never delete outside the archive tree: registry.json is hand-editable,
            # and these deletes are irreversible last-copy data.
            if not rp.is_relative_to(root) or rp.suffix != ".jsonl":
                report(
                    f"skipped archived {ts['sessionId']}: {archive_path} is not an "
                    f"archive-root .jsonl transcript"
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
        registry.purge_archived_session(reg, registry.qualified_id(ts), now_iso, index=index)
        purged += 1
    return purged


def _expired_handoff(directory: Path, path: Path, now_epoch: float, *, retention_days: int) -> bool:
    if retention_days <= 0 or path.parent != directory or path.suffix != ".md":
        return False
    try:
        st = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_mtime < now_epoch - retention_days * _SECONDS_PER_DAY


def handoff_purge_candidates(
    directory: Path, now_epoch: float, *, retention_days: int
) -> list[Path]:
    if retention_days <= 0:
        return []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    return [
        path
        for path in entries
        if _expired_handoff(directory, path, now_epoch, retention_days=retention_days)
    ]


def purge_handoffs(
    directory: Path,
    candidates: Sequence[Path],
    now_epoch: float,
    *,
    retention_days: int,
    report: Callable[[str], None] = _warn,
) -> int:
    purged = 0
    for path in candidates:
        if not _expired_handoff(directory, path, now_epoch, retention_days=retention_days):
            continue
        try:
            path.unlink()
        except OSError as error:
            report(f"skipped handoff {path.name}: {error}")
            continue
        purged += 1
    return purged
