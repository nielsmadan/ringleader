from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from ..opencode_db import connect_read_only, default_opencode_db
from .base import NormalizedSession, RawRef, SourceUnavailableError

_SCHEMA_CHECK = """
SELECT
    s.id,
    s.project_id,
    s.parent_id,
    s.slug,
    s.directory,
    s.title,
    s.time_created,
    s.time_updated,
    s.time_archived,
    prj.worktree,
    m.id,
    m.time_created,
    m.time_updated,
    m.data,
    pt.id,
    pt.time_created,
    pt.time_updated,
    pt.data
FROM session AS s
LEFT JOIN project AS prj ON prj.id = s.project_id
LEFT JOIN message AS m ON m.session_id = s.id
LEFT JOIN part AS pt ON pt.message_id = m.id
LIMIT 0
"""

_SESSION_SELECT = """
SELECT id, project_id, parent_id, slug, directory, title,
       time_created, time_updated, time_archived
FROM session
WHERE parent_id IS NULL AND time_archived IS NULL
"""

# octet_length() sizes a value from its record header without loading the payload
_PAYLOAD_BYTES = (
    "octet_length(data)"
    if sqlite3.sqlite_version_info >= (3, 43, 0)
    else "length(CAST(data AS BLOB))"
)

_SESSION_STATES = f"""
WITH message_state AS (
    SELECT
        session_id,
        COUNT(*) AS message_rows,
        SUM(time_created) AS message_created_sum,
        SUM(time_updated) AS message_updated_sum,
        MAX(CASE WHEN time_updated > time_created THEN time_updated ELSE time_created END)
            AS message_updated,
        SUM({_PAYLOAD_BYTES}) AS message_bytes
    FROM message
    GROUP BY session_id
),
part_state AS (
    SELECT
        session_id,
        COUNT(*) AS part_rows,
        SUM(time_created) AS part_created_sum,
        SUM(time_updated) AS part_updated_sum,
        MAX(CASE WHEN time_updated > time_created THEN time_updated ELSE time_created END)
            AS part_updated,
        SUM({_PAYLOAD_BYTES}) AS part_bytes
    FROM part
    GROUP BY session_id
)
SELECT
    s.id,
    s.project_id,
    s.parent_id,
    s.slug,
    s.directory,
    s.title,
    s.time_created,
    s.time_updated,
    s.time_archived,
    COALESCE(m.message_rows, 0) AS message_rows,
    COALESCE(m.message_created_sum, 0) AS message_created_sum,
    COALESCE(m.message_updated_sum, 0) AS message_updated_sum,
    COALESCE(m.message_updated, 0) AS message_updated,
    COALESCE(m.message_bytes, 0) AS message_bytes,
    COALESCE(p.part_rows, 0) AS part_rows,
    COALESCE(p.part_created_sum, 0) AS part_created_sum,
    COALESCE(p.part_updated_sum, 0) AS part_updated_sum,
    COALESCE(p.part_updated, 0) AS part_updated,
    COALESCE(p.part_bytes, 0) AS part_bytes
FROM session AS s
LEFT JOIN message_state AS m ON m.session_id = s.id
LEFT JOIN part_state AS p ON p.session_id = s.id
WHERE s.parent_id IS NULL AND s.time_archived IS NULL
ORDER BY s.time_updated, s.id
"""


class OpenCodeSourceError(SourceUnavailableError):
    pass


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return ""
    chunks = []
    for item in value:
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            chunks.append(item["text"])
    return "\n".join(chunk.strip() for chunk in chunks if chunk.strip())


def _message_text(data: dict[str, Any]) -> str:
    return _content_text(data.get("content") or data.get("text"))


def _part_text(data: dict[str, Any]) -> str:
    if data.get("type") not in (None, "text"):
        return ""
    return _content_text(data.get("text") or data.get("content"))


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(int(value), 0)


def _start_time(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _iso_millis(value: object) -> str:
    milliseconds = _nonnegative_int(value)
    try:
        return datetime.fromtimestamp(milliseconds / 1000, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return datetime.fromtimestamp(0, UTC).isoformat()


def _feed(digest: hashlib._Hash, *values: object) -> None:
    for value in values:
        raw = str(value if value is not None else "").encode()
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)


def _ref_states(connection: sqlite3.Connection) -> list[tuple[sqlite3.Row, str, int, int]]:
    states = []
    for row in connection.execute(_SESSION_STATES):
        digest = hashlib.sha256()
        _feed(digest, *row)
        size_bytes = _nonnegative_int(row["message_bytes"]) + _nonnegative_int(row["part_bytes"])
        updated = max(
            _nonnegative_int(row["time_created"]),
            _nonnegative_int(row["time_updated"]),
            _nonnegative_int(row["message_updated"]),
            _nonnegative_int(row["part_updated"]),
        )
        states.append((row, digest.hexdigest(), size_bytes, updated))
    return states


def _conversation(
    connection: sqlite3.Connection, session_id: str
) -> tuple[list[tuple[str, str]], int]:
    messages: list[tuple[str, str, str]] = []
    texts: dict[str, list[str]] = {}
    for row in connection.execute(
        """
        SELECT id, data
        FROM message
        WHERE session_id = ?
        ORDER BY time_created, id
        """,
        (session_id,),
    ):
        data = _json_object(row["data"])
        role = data.get("role")
        if role not in ("user", "assistant"):
            continue
        message_id = row["id"]
        messages.append((message_id, role, _message_text(data)))
        texts[message_id] = []

    for row in connection.execute(
        """
        SELECT pt.message_id, pt.data
        FROM part AS pt
        JOIN message AS m ON m.id = pt.message_id
        WHERE pt.session_id = ?
        ORDER BY m.time_created, m.id, pt.time_created, pt.id
        """,
        (session_id,),
    ):
        text = _part_text(_json_object(row["data"]))
        if text and row["message_id"] in texts:
            texts[row["message_id"]].append(text)

    turns = []
    for message_id, role, fallback in messages:
        text = "\n".join(texts[message_id]).strip() or fallback
        if text:
            turns.append((role, text))
    return turns, len(messages)


def _render(turns: list[tuple[str, str]]) -> str:
    return "\n".join(f"## {role.upper()}\n\n{text}\n" for role, text in turns)


@dataclass
class OpenCodeSource:
    parser_version: ClassVar[int] = 2
    db_path: Path = field(default_factory=default_opencode_db)
    tail_turns: int = 30
    name: str = "opencode"
    capabilities: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"renders_conversation", "launches_sessions", "forks_sessions"}
        )
    )

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path)

    @property
    def root(self) -> Path:
        return self.db_path

    def resume_argv(self, session_id: str) -> tuple[str, ...]:
        return ("opencode", "--session", session_id)

    def fork_argv(self, session_id: str) -> tuple[str, ...]:
        return ("opencode", "--session", session_id, "--fork")

    def launch_argv(self, prompt: str | None = None) -> tuple[str, ...]:
        if prompt is None:
            return ("opencode",)
        return ("opencode", "--prompt", prompt)

    def available(self) -> bool:
        try:
            if not self.db_path.is_file():
                return False
            connection = connect_read_only(self.db_path)
            try:
                connection.execute(_SCHEMA_CHECK).fetchall()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            return False
        return True

    def discover(self) -> Iterator[RawRef]:
        try:
            connection = connect_read_only(self.db_path)
            connection.row_factory = sqlite3.Row
            try:
                states = _ref_states(connection)
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as error:
            raise OpenCodeSourceError(
                f"cannot read OpenCode store {self.db_path}: {error}"
            ) from error

        for row, fingerprint, size_bytes, updated in states:
            session_id = row["id"]
            if not isinstance(session_id, str) or not session_id:
                continue
            yield RawRef(
                source=self.name,
                source_ref=session_id,
                file=None,
                mtime=_iso_millis(updated),
                size_bytes=size_bytes,
                fingerprint=f"opencode:{fingerprint}",
            )

    def _read(self, source_ref: str) -> tuple[sqlite3.Row, list[tuple[str, str]], int]:
        try:
            connection = connect_read_only(self.db_path)
            connection.row_factory = sqlite3.Row
            try:
                row = connection.execute(
                    _SESSION_SELECT + " AND id = ?",
                    (source_ref,),
                ).fetchone()
                if row is None:
                    raise FileNotFoundError(f"OpenCode session {source_ref} disappeared")
                turns, message_count = _conversation(connection, source_ref)
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise OpenCodeSourceError(
                f"cannot read OpenCode store {self.db_path}: {error}"
            ) from error
        return row, turns, message_count

    def parse(self, ref: RawRef) -> NormalizedSession:
        source_ref = ref.identity
        if not source_ref:
            raise OpenCodeSourceError("OpenCode ref has no session identity")
        row, turns, message_count = self._read(source_ref)
        last_prompt = next((text for role, text in reversed(turns) if role == "user"), None)
        tail = "\n".join(f"{role.upper()}: {text}" for role, text in turns[-self.tail_turns :])
        title = row["title"].strip() if isinstance(row["title"], str) else ""
        slug = row["slug"].strip() if isinstance(row["slug"], str) else ""
        return NormalizedSession(
            source=self.name,
            source_ref=source_ref,
            session_id=source_ref,
            cwd=row["directory"],
            started_at=_start_time(row["time_created"]),
            mtime=ref.mtime,
            size_bytes=ref.size_bytes,
            custom_title=title or slug or None,
            summary=None,
            last_prompt=last_prompt[:500] if last_prompt else None,
            file=None,
            message_count=message_count,
            tail_text=tail[-6000:],
        )

    def render_conversation(self, source_ref: str) -> str:
        _row, turns, _message_count = self._read(source_ref)
        return _render(turns)
