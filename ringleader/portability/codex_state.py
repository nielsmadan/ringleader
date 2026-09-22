from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from .capabilities import NativeRoots, NativeStage
from .model import PreservationError
from .snapshot import snapshot_files

SCHEMA_ID = "codex-state-5:8d92c37c04831d6053b51b591acf93fe65934081f226ff56c179222dcd3a8feb"


def database_path(roots: NativeRoots) -> Path:
    if os.environ.get("CODEX_SQLITE_HOME", "").strip():
        raise PreservationError("Codex CODEX_SQLITE_HOME override is not supported by this profile")
    configs = {
        roots.store / "config.toml",
        Path("/etc/codex/config.toml"),
        Path("/etc/codex/requirements.toml"),
    }
    configs.update(parent / ".codex" / "config.toml" for parent in (roots.cwd, *roots.cwd.parents))
    for path in configs:
        if path.is_file():
            try:
                config = tomllib.loads(path.read_text())
            except (OSError, ValueError) as exc:
                raise PreservationError(
                    f"Cannot verify Codex database configuration: {path}"
                ) from exc
            if _has_override(config):
                raise PreservationError(
                    f"Codex sqlite_home override requires another registration profile: {path}"
                )
    return roots.store / "state_5.sqlite"


def _has_override(value: Any) -> bool:
    return isinstance(value, dict) and (
        "sqlite_home" in value or any(_has_override(item) for item in value.values())
    )


def schema_identity(connection: sqlite3.Connection) -> str:
    try:
        schema = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        migrations = connection.execute(
            "SELECT version,description,success,hex(checksum) FROM _sqlx_migrations ORDER BY version"
        ).fetchall()
        payload = json.dumps(
            {"schema": schema, "migrations": migrations}, separators=(",", ":")
        ).encode()
        identity = "codex-state-5:" + hashlib.sha256(payload).hexdigest()
        if identity != SCHEMA_ID:
            raise PreservationError(f"Unsupported Codex SQLite schema/migrations: {identity}")
        if connection.execute("SELECT status FROM backfill_state WHERE id=1").fetchone() != (
            "complete",
        ):
            raise PreservationError(
                "Codex startup backfill must complete before importing a session"
            )
        return identity
    except sqlite3.Error as exc:
        raise PreservationError(f"Unsupported Codex SQLite schema: {exc}") from exc


def inspect_database(roots: NativeRoots) -> str:
    path = database_path(roots)
    if not path.is_file():
        raise PreservationError("Codex state_5.sqlite is missing; initialize the native CLI first")
    wal = path.with_name(path.name + "-wal")
    for _ in range(3):
        has_wal = wal.exists()
        paths = (("state.sqlite", path),) + ((("state.sqlite-wal", wal),) if has_wal else ())
        files = snapshot_files(paths, max_bytes=128 * 1024 * 1024)
        if wal.exists() == has_wal:
            break
    else:
        raise PreservationError("Codex schema changed during read-only preflight")
    with TemporaryDirectory(prefix="ringleader-codex-schema-") as temporary:
        directory = Path(temporary)
        for item in files:
            (directory / item.name).write_bytes(item.data)
        with sqlite3.connect(directory / "state.sqlite") as connection:
            return schema_identity(connection)


def registration(stage: NativeStage, *, recover: bool = False) -> bool:
    if stage.database_payload is None or stage.schema_identity != SCHEMA_ID:
        raise PreservationError("Missing verified Codex registration payload")
    payload = json.loads(stage.database_payload)
    if not isinstance(payload, dict) or payload.get("id") != stage.identity.native_id:
        raise PreservationError("Codex registration identity disagrees with staged session")
    path = database_path(stage.roots)
    if payload.get("rollout_path") != str(stage.roots.store / stage.source_ref):
        raise PreservationError("Codex registration rollout path disagrees with stage")
    transcript = next(
        (item for item in stage.files if item.relative_path == stage.source_ref), None
    )
    if transcript is None:
        raise PreservationError("Codex staged transcript is missing")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        schema_identity(connection)
        current = connection.execute(
            "SELECT rollout_path,cwd,source,created_at FROM threads WHERE id=?",
            (stage.identity.native_id,),
        ).fetchone()
        if current is not None:
            if (
                recover
                and Path(current[0]).resolve() == Path(payload["rollout_path"]).resolve()
                and Path(current[1]).resolve() == Path(payload["cwd"]).resolve()
                and current[2:] == (payload["source"], payload["created_at"])
                and (stage.roots.store / stage.source_ref).read_bytes().startswith(transcript.data)
            ):
                connection.rollback()
                return False
            raise PreservationError("Codex target session ID collision")
        if connection.execute(
            "SELECT id FROM threads WHERE rollout_path=?", (payload["rollout_path"],)
        ).fetchone():
            raise PreservationError("Codex target rollout path collision")
        data = (stage.roots.store / stage.source_ref).read_bytes()
        if not (
            data.startswith(transcript.data)
            if recover
            else hashlib.sha256(data).hexdigest() == transcript.digest
        ):
            raise PreservationError("Codex published rollout differs from staged bytes")
        allowed = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
        if not set(payload) <= allowed:
            raise PreservationError("Unknown Codex registration columns")
        columns = ",".join('"' + column + '"' for column in payload)
        connection.execute(
            f"INSERT INTO threads ({columns}) VALUES ({','.join('?' for _ in payload)})",
            tuple(payload.values()),
        )
        connection.commit()
        return True
    except sqlite3.Error as exc:
        raise PreservationError(f"Codex registration failed: {exc}") from exc
    finally:
        connection.rollback()
        connection.close()


def name_intent(stage: NativeStage) -> bytes:
    if stage.database_payload is None:
        raise PreservationError("Missing Codex name intent")
    payload = json.loads(stage.database_payload)

    entry = {
        "id": stage.identity.native_id,
        "thread_name": payload["name"],
        "updated_at": datetime.fromtimestamp(payload["created_at_ms"] / 1000, UTC).isoformat(),
    }
    return (json.dumps(entry, separators=(",", ":")) + "\n").encode()


def _check_name_collision(
    stage: NativeStage, connection: sqlite3.Connection, *, recover: bool
) -> None:
    payload = json.loads(stage.database_payload or "null")
    if not isinstance(payload, dict) or payload.get("id") != stage.identity.native_id:
        raise PreservationError("Codex name identity disagrees with staged session")
    target = stage.roots.store / stage.source_ref
    transcript = next(item for item in stage.files if item.relative_path == stage.source_ref)
    owned = recover and target.is_file() and target.read_bytes().startswith(transcript.data)
    current = connection.execute(
        "SELECT rollout_path,cwd,source,created_at FROM threads WHERE id=?",
        (stage.identity.native_id,),
    ).fetchone()
    if current is not None and not (
        owned
        and Path(current[0]).resolve() == target.resolve()
        and Path(current[1]).resolve() == Path(payload["cwd"]).resolve()
        and current[2:] == (payload["source"], payload["created_at"])
    ):
        raise PreservationError("Codex target session ID collision before name publication")
    for directory in ("sessions", "archived_sessions"):
        for path in (stage.roots.store / directory).rglob(f"*{stage.identity.native_id}*.jsonl"):
            if not (owned and path.resolve() == target.resolve()):
                raise PreservationError(
                    "Codex rollout identity collision; publish name intent before the rollout"
                )


def append_name(stage: NativeStage) -> bool:
    connection = sqlite3.connect(
        database_path(stage.roots).resolve().as_uri() + "?mode=rw", uri=True
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        schema_identity(connection)
        return _append_name(stage, connection)
    finally:
        connection.rollback()
        connection.close()


def _append_name(stage: NativeStage, connection: sqlite3.Connection) -> bool:

    intended = name_intent(stage)
    path = stage.roots.store / "session_index.jsonl"
    prior = path.read_bytes() if path.exists() else b""
    _check_name_collision(stage, connection, recover=intended.rstrip(b"\n") in prior.splitlines())
    descriptor = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            existing = stream.read()
        if existing and not existing.endswith(b"\n"):
            raise PreservationError("Codex name index has an incomplete record")
        for line in existing.splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError as exc:
                raise PreservationError("Codex name index has malformed JSON") from exc
            if entry.get("id") == stage.identity.native_id:
                if json.loads(intended) == entry:
                    return False
                raise PreservationError(
                    "Codex name index identity collision or later rename; preserve existing title"
                )
        if (stage.roots.store / stage.source_ref).exists():
            raise PreservationError(
                "Publish Codex name intent before the rollout becomes discoverable"
            )
        if os.write(descriptor, intended) != len(intended):
            raise PreservationError("Incomplete Codex name append; inspect journal before recovery")
        os.fsync(descriptor)
        return True
    finally:
        os.close(descriptor)


def registration_status(stage: NativeStage) -> Literal["absent", "owned", "archived", "conflict"]:
    payload = json.loads(stage.database_payload or "null")
    if not isinstance(payload, dict) or payload.get("id") != stage.identity.native_id:
        raise PreservationError("Missing Codex registration evidence")
    connection = sqlite3.connect(
        database_path(stage.roots).resolve().as_uri() + "?mode=ro", uri=True
    )
    try:
        schema_identity(connection)
        row = connection.execute(
            "SELECT rollout_path,cwd,source,created_at,archived FROM threads WHERE id=?",
            (stage.identity.native_id,),
        ).fetchone()
        if row is None:
            return "absent"
        expected = (
            payload["rollout_path"],
            payload["cwd"],
            payload["source"],
            payload["created_at"],
        )
        if row[1:4] != expected[1:]:
            return "conflict"
        if row[4] and Path(row[0]).name == Path(expected[0]).name:
            return "archived"
        return "owned" if Path(row[0]).resolve() == Path(expected[0]).resolve() else "conflict"
    finally:
        connection.close()
