from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import registry
from . import default_registry

_SESSION_HEADING = re.compile(r"(?i)^session\s+[0-9a-f-]+\s*[—-]\s*(.+)$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def cmd_archive(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl archive")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--archive-root", default=str(default_registry().parent / "archive"))
    ap.add_argument("--summary-file", default=None,
                    help="markdown summary to store next to the archived transcript")
    args = ap.parse_args(argv)

    reg = registry.load_registry(args.registry)
    t = next((x for x in reg["threads"] if x["sessionId"] == args.id), None)
    if t is None:
        print(f"rl-archive: unknown session {args.id}", file=sys.stderr)
        return 1

    src = Path(t["file"])
    dest_dir = Path(args.archive_root) / src.parent.name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    sidecar = src.with_suffix("")
    sidecar_dest = dest_dir / sidecar.name
    if dest.exists() or sidecar_dest.exists():
        print(f"rl-archive: {dest_dir} already has {src.name} or its sidecar, aborting", file=sys.stderr)
        return 1

    if src.exists():
        shutil.move(str(src), str(dest))
    else:
        print(f"rl-archive: {src} already gone, recording archive anyway", file=sys.stderr)
    if sidecar.exists():
        shutil.move(str(sidecar), str(sidecar_dest))

    summary_path = None
    if args.summary_file and Path(args.summary_file).exists():
        summary_path = dest_dir / (src.stem + ".md")
        shutil.copyfile(args.summary_file, summary_path)

    summary = t.get("whatItIs") or _summary_from_file(args.summary_file)
    registry.archive_session(
        reg, args.id, str(dest), _now(),
        summary=summary,
        project=t.get("gitProjectLabel") or t.get("project"))
    registry.save_registry(args.registry, reg)
    extra = f" (+summary {summary_path.name})" if summary_path else ""
    print(f"archived {args.id} -> {dest}{extra}")
    return 0
