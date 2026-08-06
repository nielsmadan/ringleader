from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "ringleader"


def default_registry() -> Path:
    return state_dir() / "registry.json"


def config_path() -> Path:
    return state_dir() / "config.json"


def archive_root() -> Path:
    return state_dir() / "archive"
