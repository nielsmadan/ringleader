from __future__ import annotations

import os
import sys
from pathlib import Path


def exists_or_report(p: Path) -> bool:
    """Whether `p` exists, reporting a stat failure instead of raising or hiding it.

    Stats directly because `Path.exists()` is not stable across the supported
    Pythons — 3.13 re-raises EACCES, 3.14 answers False — so it would either crash
    a probe or erase the difference between "absent" and "unreadable". `os.stat`
    raises on both."""
    try:
        os.stat(p)
    except FileNotFoundError:
        return False
    except OSError as e:
        print(f"{p}: unreadable ({e.strerror or e})", file=sys.stderr)
        return False
    return True


def factory_home() -> Path:
    """The HOME Droid resolves its `.factory` dir against — `FACTORY_HOME_OVERRIDE`
    replaces HOME itself, not the `.factory` name."""
    override = os.environ.get("FACTORY_HOME_OVERRIDE")
    return Path(override) if override else Path.home()


def factory_dir() -> Path:
    return factory_home() / ".factory"


def factory_label() -> str:
    """How to name droid's config dir in a message: `~/.factory` normally, the real
    path once `FACTORY_HOME_OVERRIDE` moves it somewhere the reader can't guess."""
    return "~/.factory" if factory_home() == Path.home() else str(factory_dir())


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


def handoffs_dir() -> Path:
    return state_dir() / "handoffs"


def ensure_handoffs_dir() -> Path:
    """The handoffs dir, created owner-only: its listing names which sessions were handed
    off and when, which is as private as the transcripts it holds."""
    ensure_state_dir()
    d = handoffs_dir()
    d.mkdir(mode=0o700, exist_ok=True)
    d.chmod(0o700)
    return d


def archive_root() -> Path:
    return state_dir() / "archive"


def bundled_skills_dir() -> Path:
    """Where the ringleader-* skill folders live: the wheel-bundled copy when
    installed, else the repo's .agents/skills for source runs."""
    packaged = Path(__file__).resolve().parent / "_skills"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parent.parent / ".agents" / "skills"
