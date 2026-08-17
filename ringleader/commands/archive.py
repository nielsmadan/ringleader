from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from .. import registry, scanner
from ..paths import archive_root
from . import default_registry

_SESSION_HEADING = re.compile(r"(?i)^session\s+[0-9a-f-]+\s*[—-]\s*(.+)$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
    reg: dict,
    thread: dict,
    *,
    archive_root_path,
    summary_file: str | None = None,
) -> str:
    """Move the transcript (+ sidecar/summary) into archive_root and record the tombstone.

    Returns the destination path. Raises RuntimeError on move-collision or path conflicts
    (the caller decides how to report that to the user)."""
    src = Path(thread["file"])
    slug = (
        thread.get("gitProjectLabel")
        or (Path(thread["project"]).name if thread.get("project") else None)
        or thread.get("source")
        or "unknown"
    )
    dest_dir = Path(archive_root_path) / slug
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    sidecar = src.with_suffix("")
    sidecar_dest = dest_dir / sidecar.name
    if dest.exists() or sidecar_dest.exists():
        raise RuntimeError(f"{dest_dir} already has {src.name} or its sidecar, aborting")

    scanner.hide_from_picker(thread)

    if src.exists():
        shutil.move(str(src), str(dest))
    else:
        print(f"rl-archive: {src} already gone, recording archive anyway", file=sys.stderr)
    if sidecar.exists():
        shutil.move(str(sidecar), str(sidecar_dest))

    summary_path = None
    if summary_file and Path(summary_file).exists():
        summary_path = dest_dir / (src.stem + ".md")
        shutil.copyfile(summary_file, summary_path)

    summary = thread.get("whatItIs") or _summary_from_file(summary_file)
    registry.archive_session(
        reg,
        thread["sessionId"],
        str(dest),
        _now(),
        summary=summary,
        project=thread.get("gitProjectLabel") or thread.get("project"),
    )
    return str(dest)


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

    reg = registry.load_registry(args.registry)
    t = next((x for x in reg["threads"] if x["sessionId"] == args.id), None)
    if t is None:
        print(f"rl-archive: unknown session {args.id}", file=sys.stderr)
        return 1

    try:
        dest = archive_thread(
            reg,
            t,
            archive_root_path=args.archive_root,
            summary_file=args.summary_file,
        )
    except RuntimeError as e:
        print(f"rl-archive: {e}", file=sys.stderr)
        return 1

    registry.save_registry(args.registry, reg)
    summary_path = Path(dest).with_suffix(".md")
    extra = f" (+summary {summary_path.name})" if summary_path.exists() else ""
    print(f"archived {args.id} -> {dest}{extra}")
    return 0
