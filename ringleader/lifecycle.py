from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import registry, scanner
from .agents import source_of
from .sources.base import IndexUpdate

GRACE_SECONDS = 120


@dataclass(frozen=True)
class ArchiveResult:
    path: str
    warnings: tuple[str, ...] = ()


_SESSION_HEADING = re.compile(r"(?i)^session\s+[0-9a-f-]+\s*[—-]\s*(.+)$")


def _summary_from_file(path: str | None) -> str | None:
    """Derive a one-line tombstone summary from a markdown summary file: the first
    non-empty line, with markdown heading hashes and any 'Session <id> — ' prefix
    stripped. Used as a fallback when the thread has no classified whatItIs."""
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if not s:
            continue
        m = _SESSION_HEADING.match(s)
        return m.group(1).strip() if m else s
    return None


def archive_thread(
    reg: dict[str, Any],
    thread: dict[str, Any],
    *,
    archive_root_path: str | Path,
    now_iso: str,
    summary_file: str | None = None,
    index: registry.SessionIndex | None = None,
) -> ArchiveResult:
    """Move the transcript (+ sidecar/summary) into archive_root and record the tombstone.

    Returns the destination path and warnings. Raises RuntimeError on move-collision or path conflicts
    (the caller decides how to report that to the user)."""
    src = scanner.filesystem_path(thread, "archive")
    slug = (
        thread.get("gitProjectLabel")
        or (Path(thread["project"]).name if thread.get("project") else None)
        or thread.get("source")
        or "unknown"
    )
    dest_dir = Path(archive_root_path) / slug
    dest = dest_dir / src.name
    companions = list(scanner.companion_moves(thread.get("source"), src, dest))
    summary_source = Path(summary_file) if summary_file else None
    summary_dest = dest_dir / (src.stem + ".md") if summary_source else None
    summary_already_copied = False
    if not src.exists() and not dest.exists():
        raise RuntimeError(f"{src.name} is missing from both source and archive destination")
    if (src.exists() and dest.exists()) or any(c.exists() and d.exists() for c, d in companions):
        raise RuntimeError(f"{dest_dir} already has {src.name} or its sidecar, aborting")
    if summary_source and summary_source.exists() and summary_dest and summary_dest.exists():
        summary_already_copied = summary_source.read_bytes() == summary_dest.read_bytes()
        if not summary_already_copied:
            raise RuntimeError(f"{summary_dest} already exists, aborting")
    dest_dir.mkdir(parents=True, exist_ok=True)

    scanner.hide_from_picker(thread)

    warnings: tuple[str, ...] = ()
    if src.exists():
        shutil.move(str(src), str(dest))
    else:
        warnings = (f"{src} already gone, recording archive anyway",)
    for companion, companion_dest in companions:
        if companion.exists():
            shutil.move(str(companion), str(companion_dest))

    summary_path = summary_dest if summary_dest and summary_dest.exists() else None
    if summary_source and summary_source.exists() and summary_dest and not summary_already_copied:
        shutil.copyfile(summary_source, summary_dest)
        summary_path = summary_dest

    summary = thread.get("whatItIs") or _summary_from_file(
        str(summary_path) if summary_path else summary_file
    )
    registry.archive_session(
        reg,
        registry.qualified_id(thread),
        str(dest),
        now_iso,
        summary=summary,
        project=thread.get("gitProjectLabel") or thread.get("project"),
        index=index,
    )
    return ArchiveResult(str(dest), warnings)


def restore(reg: dict[str, Any], ts: dict[str, Any]) -> tuple[str, IndexUpdate]:
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
        "source": source_of(ts),
        "file": original,
    }
    scanner.filesystem_path(thread, "unarchive")
    src = Path(ts["archivePath"])
    dest = Path(original)
    if dest.exists():
        raise RuntimeError(f"{dest} already exists — not overwriting")
    if not src.exists():
        raise RuntimeError(f"archived transcript is gone: {src}")

    companions = scanner.companion_moves(thread.get("source"), src, dest)
    if blocked := next((d for c, d in companions if c.exists() and d.exists()), None):
        raise RuntimeError(f"{blocked} already exists — not overwriting")

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    for companion, companion_dest in companions:
        if companion.exists():
            companion_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(companion), str(companion_dest))

    shown = scanner.show_in_picker(thread, str(dest))
    # The workspace may have moved while this session sat in the archive, in which case
    # its registry project is already the new path but the agent's index is not.
    project = thread.get("project")
    if project:
        scanner.rebind_workspace(thread, str(dest), project)

    registry.restore_session(reg, registry.qualified_id(ts), str(dest))
    if ts.get("thread"):
        # belt and braces: `archive_session` strips the finish mark before storing, but
        # a record archived by an earlier build still carries it
        restored = registry.without_finish_mark(ts["thread"])
        restored["file"] = str(dest)
        reg["threads"].append(restored)
    return str(dest), shown


def mtime(path: str) -> float | None:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def due(
    reg: dict[str, Any], now: float, *, grace_seconds: int = GRACE_SECONDS
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in reg.get("threads", []):
        if not t.get("finishOnExit"):
            continue
        try:
            path = scanner.filesystem_path(t, "sweep")
        except RuntimeError:
            out.append(t)
            continue
        m = mtime(str(path))
        # a transcript already gone is still due: archive_thread records it either way
        if m is None or now - m >= grace_seconds:
            out.append(t)
    return out
