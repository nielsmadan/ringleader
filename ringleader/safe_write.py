"""Editing a config file that belongs to something else — Claude Code's
`settings.json`, Codex's `hooks.json`. Show the change, keep a copy, and never leave
a half-written file behind."""

from __future__ import annotations

import difflib
import os
import shutil
import tempfile
from pathlib import Path


def diff(path: Path, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )


def backup_and_write(path: Path, text: str) -> Path | None:
    """Replace `path`'s contents with `text`, keeping the old bytes in `<name>.bak`.

    The new text goes to a temp file alongside the target and is then `os.replace`d
    into place, so a killed process or a full disk leaves the original file intact
    rather than a truncated config. Returns the backup path, or None when there was
    no file to back up."""
    backup: Path | None = path.with_name(path.name + ".bak")
    try:
        shutil.copy2(path, backup)
    except FileNotFoundError:
        backup = None

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return backup
