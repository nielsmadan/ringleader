"""Editing a config file that belongs to something else — Claude Code's
`settings.json`, Codex's `hooks.json`. Show the change, keep a copy, and never leave
a half-written file behind."""

from __future__ import annotations

import difflib
import os
import shutil
import stat
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


def _mode_for(path: Path) -> int:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask


def backup_and_write(path: Path, text: str) -> Path | None:
    """Replace `path`'s contents with `text`, keeping the old bytes in `<name>.bak`.

    The new text goes to a temp file alongside the target and is then `os.replace`d
    into place, so a killed process or a full disk leaves the original file intact
    rather than a truncated config. Returns the backup path, or None when there was
    no file to back up.

    A symlinked target is written through to its destination — `settings.json` pointing
    into a dotfiles repo stays a link — and an existing file keeps its own mode, which
    the statusLine script needs to stay executable."""
    path = Path(os.path.realpath(path))
    mode = _mode_for(path)

    backup_path = path.with_name(path.name + ".bak")
    try:
        shutil.copy2(path, backup_path)
    except FileNotFoundError:
        backup: Path | None = None
    else:
        backup = backup_path

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return backup
