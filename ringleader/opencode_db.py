from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def default_opencode_db() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(os.environ.get("OPENCODE_DB") or Path(base) / "opencode" / "opencode.db")


def connect_read_only(path: str | Path, *, timeout: float = 0.1) -> sqlite3.Connection:
    uri = Path(path).absolute().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=timeout)
    connection.execute("PRAGMA query_only = ON")
    return connection
