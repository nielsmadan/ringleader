from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from urllib.parse import urlsplit

from .capabilities import NativeRoots, NativeStage
from .model import PreservationError, unique_object
from .snapshot import snapshot_files

SCHEMA_ID = "opencode-1.18.30:" + "4e1c29893bede253e37904f6e62e09cd5c82bc37da02c7c147f665c6bd1dbbf9"


def encode(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def decode(value: bytes | str) -> Any:
    try:
        return json.loads(value, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError) as exc:
        raise PreservationError(f"Invalid OpenCode JSON: {exc}") from exc


def database_path(roots: NativeRoots) -> Path:
    return roots.store / "opencode.db"


def schema_identity(connection: sqlite3.Connection) -> str:
    try:
        rows = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        schema = [[*row[:3], re.sub(r"\s+", " ", row[3]).strip()] for row in rows]
        migrations = [row[0] for row in connection.execute("SELECT id FROM migration ORDER BY id")]
        payload = json.dumps(
            {"schema": schema, "migrations": migrations}, separators=(",", ":")
        ).encode()
        identity = "opencode-1.18.30:" + hashlib.sha256(payload).hexdigest()
        if identity != SCHEMA_ID:
            raise PreservationError(f"Unsupported OpenCode SQLite schema/migrations: {identity}")
        return identity
    except sqlite3.Error as exc:
        raise PreservationError(f"Unsupported OpenCode SQLite schema: {exc}") from exc


@contextmanager
def database_copy(path: Path) -> Iterator[Path]:
    if not path.is_file():
        raise PreservationError("OpenCode opencode.db is missing; initialize the native CLI first")
    wal = path.with_name(path.name + "-wal")
    for _ in range(3):
        present = wal.exists()
        paths = (("state.db", path),) + ((("state.db-wal", wal),) if present else ())
        files = snapshot_files(paths, max_bytes=128 * 1024 * 1024)
        if present == wal.exists():
            break
    else:
        raise PreservationError("OpenCode database changed during preflight")
    with TemporaryDirectory(prefix="ringleader-opencode-schema-") as temporary:
        directory = Path(temporary)
        for item in files:
            (directory / item.name).write_bytes(item.data)
        yield directory / "state.db"


def inspect_database(roots: NativeRoots) -> str:
    with database_copy(database_path(roots)) as isolated:
        connection = sqlite3.connect(isolated)
        try:
            return schema_identity(connection)
        finally:
            connection.close()


def normalized_remote(value: str) -> str | None:
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme == "file":
        return None
    if parsed.hostname:
        host, name = parsed.hostname, parsed.path
        host = "[" + host + "]" if ":" in host else host.encode("idna").decode()
    elif parsed.scheme:
        if parsed.scheme in ("http", "https", "ftp"):
            raise PreservationError("OpenCode remote URL without authority is unsupported")
        return None
    else:
        match = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):(.+)", value)
        if not match:
            return None
        host, name = match.groups()
    name = re.sub(r"\.git/?$", "", name.lstrip("/")).rstrip("/")
    return f"{host.lower()}/{name}" if host and name else None


def _git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ("git", "-C", str(cwd), *args), capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreservationError(f"Cannot resolve native OpenCode project: {exc}") from exc
    return result.stdout.strip() if result.returncode == 0 else None


def resolve_project(cwd: Path) -> tuple[str, str, str | None]:
    cwd = cwd.resolve()
    if not cwd.is_dir():
        raise PreservationError("OpenCode destination cwd must exist")
    ancestor = next((p for p in (cwd, *cwd.parents) if (p / ".git").exists()), None)
    if ancestor is None:
        return "global", cwd.anchor, None
    common = _git(ancestor, "rev-parse", "--git-common-dir")
    git_dir = _git(ancestor, "rev-parse", "--git-dir")
    if common is None or git_dir is None:
        return "global", cwd.anchor, None
    worktree = _git(ancestor, "rev-parse", "--show-toplevel") or str(ancestor)
    remote = normalized_remote(_git(Path(worktree), "remote", "get-url", "origin") or "")
    if remote:
        identity = hashlib.sha1(("git-remote:" + remote).encode()).hexdigest()
    else:
        cached = ancestor / common / "opencode"
        try:
            identity = cached.read_text().strip()
        except OSError:
            identity = ""
        if not identity:
            roots = sorted(
                filter(
                    None,
                    (
                        _git(Path(worktree), "rev-list", "--max-parents=0", "HEAD") or ""
                    ).splitlines(),
                )
            )
            identity = roots[0] if roots else "global"
    return identity, str(Path(worktree).resolve()), "git"


def insert(connection: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    if table not in ("project", "session", "message", "part"):
        raise PreservationError("Invalid OpenCode insertion table")
    allowed = {r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')}
    if not row or not set(row) <= allowed:
        raise PreservationError(f"Unknown OpenCode {table} columns")
    columns = ",".join('"' + name + '"' for name in row)
    connection.execute(
        f'INSERT INTO "{table}" ({columns}) VALUES ({",".join("?" for _ in row)})',
        tuple(row.values()),
    )


def _owned(current: dict[str, Any], expected: dict[str, Any]) -> bool:
    try:
        marker = decode(current["metadata"] or "{}").get("ringleader", {}).get("registration")
        intended = decode(expected["metadata"])["ringleader"]["registration"]
    except (KeyError, TypeError, AttributeError, PreservationError):
        return False
    return marker == intended and all(
        current[key] == expected[key] for key in ("id", "project_id", "directory", "time_created")
    )


def registration(stage: NativeStage, *, recover: bool = False) -> bool:
    if stage.database_payload is None or stage.schema_identity != SCHEMA_ID:
        raise PreservationError("Missing verified OpenCode registration payload")
    payload = decode(stage.database_payload)
    session = payload["session"]
    if session["id"] != stage.identity.native_id:
        raise PreservationError("OpenCode registration identity disagrees with stage")
    project_id, worktree, vcs = resolve_project(stage.roots.cwd)
    if (project_id, worktree, vcs) != tuple(payload["project_identity"]):
        raise PreservationError("OpenCode native project identity changed since staging")
    if (
        session["project_id"] != project_id
        or session["directory"] != str(stage.roots.cwd.resolve())
        or payload["project"]["id"] != project_id
        or payload["project"]["worktree"] != worktree
        or payload["project"]["vcs"] != vcs
    ):
        raise PreservationError("OpenCode staged graph disagrees with native project identity")
    path = database_path(stage.roots)
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        schema_identity(connection)
        row = connection.execute("SELECT * FROM session WHERE id=?", (session["id"],)).fetchone()
        if row is not None:
            if recover and _owned(dict(row), session):
                return False
            raise PreservationError("OpenCode target session ID collision or unverifiable recovery")
        existing_project = connection.execute(
            "SELECT * FROM project WHERE id=?", (project_id,)
        ).fetchone()
        if existing_project is None:
            insert(connection, "project", payload["project"])
        elif existing_project["vcs"] != vcs:
            raise PreservationError("OpenCode project identity conflicts with native metadata")
        insert(connection, "session", session)
        for table in ("message", "part"):
            for item in payload[table]:
                insert(connection, table, item)
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise PreservationError("OpenCode database foreign key integrity check failed")
        connection.commit()
        return True
    except sqlite3.Error as exc:
        raise PreservationError(f"OpenCode registration failed: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.rollback()
            connection.close()


def registration_status(stage: NativeStage) -> Literal["absent", "owned", "archived", "conflict"]:
    payload = decode(stage.database_payload or b"null")
    if (
        not isinstance(payload, dict)
        or payload.get("session", {}).get("id") != stage.identity.native_id
    ):
        raise PreservationError("Missing OpenCode registration evidence")
    connection = sqlite3.connect(
        database_path(stage.roots).resolve().as_uri() + "?mode=ro", uri=True
    )
    try:
        connection.row_factory = sqlite3.Row
        schema_identity(connection)
        row = connection.execute(
            "SELECT * FROM session WHERE id=?", (stage.identity.native_id,)
        ).fetchone()
        if row is None:
            return "absent"
        if not _owned(dict(row), payload["session"]):
            return "conflict"
        return "archived" if row["time_archived"] is not None else "owned"
    finally:
        connection.close()
