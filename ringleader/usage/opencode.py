from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ..opencode_db import connect_read_only, default_opencode_db
from .base import (
    UsageRecord,
    as_dict,
    as_float_or_none,
    as_int,
    record_day,
    total_of,
)
from .checkpoint import Checkpoint

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

    def records(
        self, since: date | None = None, session_prefix: str | None = None
    ) -> Iterator[UsageRecord]:
        variant = json.dumps([since.isoformat() if since else None, session_prefix])
        try:
            target = self.db_path.resolve()
        except (OSError, RuntimeError):
            return
        namespace = f"{self.name}-session" if session_prefix else self.name
        with Checkpoint(namespace, self.db_path, [target]) as checkpoint:
            yield from checkpoint.records(
                target,
                lambda: self._records(since, session_prefix, target),
                companions=(_wal(target),),
                variant=variant,
            )

    def _records(
        self, since: date | None, session_prefix: str | None, target: Path | None = None
    ) -> Iterator[UsageRecord]:
        conditions: list[str] = []
        params: list[object] = []
        if since is not None:
            conditions.append("m.time_created >= ?")
            params.append(
                int(datetime(since.year, since.month, since.day, tzinfo=UTC).timestamp() * 1000)
            )
        if session_prefix:
            conditions.append("substr(m.session_id, 1, ?) = ?")
            params.extend([len(session_prefix), session_prefix])
        for row in self._rows(conditions, params, "ASC", target):
            record = _record(row)
            if record is not None:
                yield record

    def _latest_records(self, target: Path) -> list[UsageRecord]:
        for row in self._rows([], [], "DESC", target):
            record = _record(row)
            if record is not None:
                return [record]
        return []

    def latest_day(self) -> str | None:
        try:
            target = self.db_path.resolve()
        except (OSError, RuntimeError):
            return None
        with Checkpoint("opencode-latest", self.db_path, [target]) as checkpoint:
            records = list(
                checkpoint.records(
                    target,
                    lambda: self._latest_records(target),
                    companions=(_wal(target),),
                )
            )
        return (record_day(records[0]) or None) if records else None

    def _rows(
        self,
        conditions: list[str],
        params: list[object],
        direction: str,
        target: Path | None = None,
    ) -> Iterator[sqlite3.Row]:
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"{_MESSAGE_SELECT}{where} ORDER BY m.time_created {direction}, m.id {direction}"
        connection = connect_read_only(target or self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield from connection.execute(query, params)
        finally:
            connection.close()


def _wal(path: Path) -> Path:
    return path.with_name(path.name + "-wal")


def _record(row: sqlite3.Row) -> UsageRecord | None:
    data = _json_object(row["data"])
    if data.get("role") != "assistant":
        return None
    tokens = as_dict(data.get("tokens"))
    if not tokens:
        return None
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
        return None
    cost = as_float_or_none(data.get("cost"))
    session_id = row["session_id"]
    record_id = row["record_id"]
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(record_id, str) or not record_id:
        return None
    return UsageRecord(
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
