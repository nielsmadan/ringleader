from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..opencode_db import connect_read_only, default_opencode_db
from .base import UsageRecord, as_dict, as_float_or_none, as_int, total_of

_MESSAGE_SELECT = """
SELECT
    m.id AS record_id,
    m.session_id,
    p.worktree,
    s.directory,
    m.time_created,
    m.data
FROM message AS m
JOIN session AS s ON s.id = m.session_id
LEFT JOIN project AS p ON p.id = s.project_id
"""


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _timestamp(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return ""
    try:
        return datetime.fromtimestamp(value / 1000, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


@dataclass
class OpenCodeUsageReader:
    name: str = "opencode"
    db_path: Path = field(default_factory=default_opencode_db)

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path)

    def available(self) -> bool:
        try:
            if not self.db_path.is_file():
                return False
            connection = connect_read_only(self.db_path)
            try:
                connection.execute(_MESSAGE_SELECT + " LIMIT 0").fetchall()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            return False
        return True

    def records(self) -> Iterator[UsageRecord]:
        try:
            connection = connect_read_only(self.db_path)
            connection.row_factory = sqlite3.Row
            try:
                rows = connection.execute(
                    _MESSAGE_SELECT + " ORDER BY m.time_created, m.id"
                ).fetchall()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            return

        for row in rows:
            data = _json_object(row["data"])
            if data.get("role") != "assistant":
                continue
            tokens = as_dict(data.get("tokens"))
            if not tokens:
                continue
            cache = as_dict(tokens.get("cache"))
            inp = max(as_int(tokens.get("input")), 0)
            reasoning = max(as_int(tokens.get("reasoning")), 0)
            out = max(as_int(tokens.get("output")), 0) + reasoning
            cache_read = max(as_int(cache.get("read")), 0)
            cache_write = max(as_int(cache.get("write")), 0)
            total = total_of(
                input=inp,
                cache_read=cache_read,
                cache_write=cache_write,
                output=out,
            )
            if total <= 0:
                continue
            cost = as_float_or_none(data.get("cost"))
            session_id = row["session_id"]
            record_id = row["record_id"]
            if not isinstance(session_id, str) or not session_id:
                continue
            if not isinstance(record_id, str) or not record_id:
                continue
            yield UsageRecord(
                source="opencode",
                session_id=session_id,
                project=row["worktree"] or row["directory"],
                timestamp=_timestamp(row["time_created"]),
                provider=_text(data.get("providerID")),
                model=_text(data.get("modelID")),
                input=inp,
                output=out,
                cache_read=cache_read,
                cache_write=cache_write,
                reasoning=reasoning,
                total=total,
                cost=float(cost) if cost is not None else None,
                record_id=record_id,
            )
