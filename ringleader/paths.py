from __future__ import annotations

import os
import sys
from pathlib import Path


def exists_or_report(p: Path) -> bool:
    """Whether `p` exists, reporting a stat failure instead of raising it.

    `Path.exists()` swallows only ENOENT/ENOTDIR/EBADF/ELOOP, so an unreadable
    `~/.pi` raises EACCES straight out of an agent-detection probe and takes down
    whatever ran it (`rl doctor`, an unattended `rl update all --yes`)."""
    try:
        return p.exists()
    except OSError as e:
        print(f"{p}: unreadable ({e.strerror or e})", file=sys.stderr)
        return False


def state_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "ringleader"


def ensure_state_dir() -> Path:
    """The state dir, created if absent. Call this before writing anything beneath it:
    several writers live here (registry, archive, harvests, capture files) and each one
    remembering to mkdir independently is the same bug waiting to recur.

    Deliberately not folded into `state_dir()` — that is called on read paths too, and a
    plain accessor should not materialise directories as a side effect."""
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_registry() -> Path:
    return state_dir() / "registry.json"


def config_path() -> Path:
    return state_dir() / "config.json"


def publish_receipts_dir() -> Path:
    return state_dir() / "publish" / "receipts"


def archive_root() -> Path:
    return state_dir() / "archive"


def bundled_skills_dir() -> Path:
    """Where the ringleader-* skill folders live: the wheel-bundled copy when
    installed, else the repo's .agents/skills for source runs."""
    packaged = Path(__file__).resolve().parent / "_skills"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parent.parent / ".agents" / "skills"
