from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tomllib
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, ClassVar, Literal

from ..memory_types import (
    MemoryObservation,
    MemoryRecord,
    MemoryRef,
    MemoryScan,
    MemoryStoreStatus,
    NativeMemoryTransaction,
)
from . import codex_mutation
from .base import NativePaths, check_config_depth, digest, file_identity, harness_status, memory_id
from .codex_rows import Revision
from .transactions import NativeBusy

MAX_SNAPSHOT = 256 * 1024 * 1024

FILES = {"v1": "memories_1.sqlite", "v2": "memories_v2_1.sqlite"}
SCHEMA_DIGEST = "d856ae02990ec8f90f222c821fcf876c2c278509ce4aacb45bba66a74305ac27"
MIGRATIONS = (
    (
        1,
        "memories",
        1,
        "a1af50da50775a70f98da680006b7df4501956753ae98511ab727aa295915d9ae6f4190b4eaa5abc4fd7891642f7d21d",
    ),
    (
        2,
        "consolidation progress",
        1,
        "18f0a8dd7fe9a847b30d719029066d7a78e0bc64310dc66e4f7708bad6f1a0a594c0dbd14fec1ccb410cd7ae75dd7b10",
    ),
)


def resolve_sqlite_home(
    codex_home: Path, env_root: str | None, *, configs: tuple[Path, ...], paths: NativePaths
) -> Path:
    paths.check(codex_home)
    try:
        profiles = tuple(
            path for path in codex_home.iterdir() if path.name.endswith(".config.toml")
        )
    except FileNotFoundError:
        profiles = ()
    user_config = codex_home / "config.toml"
    configured: Path | None = None
    alternatives: list[Any] = []
    for path in dict.fromkeys((user_config, *configs, *profiles)):
        try:
            data = tomllib.loads(paths.read(path)[0].decode())
        except FileNotFoundError:
            continue
        check_config_depth(data, path)
        legacy_profiles = data.get("profiles")
        if isinstance(legacy_profiles, dict):
            alternatives.extend(
                profile["sqlite_home"]
                for profile in legacy_profiles.values()
                if isinstance(profile, dict) and "sqlite_home" in profile
            )
        if "sqlite_home" in data:
            raw = data["sqlite_home"]
            if path != user_config:
                alternatives.append(raw)
            elif not isinstance(raw, str) or not Path(raw).is_absolute():
                raise ValueError(
                    "Codex SQLite configuration needs an explicit memory.codex_sqlite_home"
                )
            else:
                configured = Path(raw)
    result = configured or (Path(env_root.strip()) if env_root and env_root.strip() else codex_home)
    if any(not isinstance(raw, str) or Path(raw) != result for raw in alternatives):
        raise ValueError("Codex SQLite configuration needs an explicit memory.codex_sqlite_home")
    paths.check(result)
    return result


type _FileStamp = tuple[str, int, int, int]


class _DatabaseChanged(NativeBusy):
    pass


def sqlite_busy(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _stamp(info: os.stat_result) -> _FileStamp:
    return file_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _snapshot_stamp(path: Path, paths: NativePaths) -> _FileStamp | None:
    paths.check(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"expected SQLite database file: {path}")
    return _stamp(info)


def _copy_snapshot(path: Path, dest: Path, expected: _FileStamp, paths: NativePaths) -> None:
    with (
        paths.open_file(path) as src,
        os.fdopen(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out,
    ):
        if _stamp(os.fstat(src.fileno())) != expected:
            raise _DatabaseChanged(f"SQLite file changed during scan: {path}")
        remaining = expected[1]
        while remaining:
            chunk = src.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise _DatabaseChanged(f"SQLite file changed during scan: {path}")
            out.write(chunk)
            remaining -= len(chunk)
        if _stamp(os.fstat(src.fileno())) != expected:
            raise _DatabaseChanged(f"SQLite file changed during scan: {path}")


def _protected_directory(root: Path, paths: NativePaths) -> os.stat_result | None:
    for directory in (root, *root.parents):
        try:
            if paths.boundary is not None:
                paths.check(directory)
            return directory.stat()
        except PermissionError:
            # Exclude the enclosing directory when its native subtree cannot be searched.
            continue
        except FileNotFoundError:
            return None
    raise ValueError(f"Cannot establish snapshot isolation from {root}")


def _temporary_snapshot_dir(
    native_roots: tuple[Path, ...], paths: NativePaths
) -> TemporaryDirectory[str]:
    protected = [
        info for root in native_roots if (info := _protected_directory(root, paths)) is not None
    ]
    candidates = (os.environ.get("TMPDIR"), os.environ.get("TEMP"), os.environ.get("TMP"))
    for candidate in (*candidates, "/tmp", "/var/tmp"):
        if not candidate:
            continue
        try:
            directory = Path(candidate).resolve(strict=True)
            if any(
                any(os.path.samestat(parent, native) for native in protected)
                for parent in map(Path.stat, (directory, *directory.parents))
            ):
                continue
            return TemporaryDirectory(dir=directory, prefix="ringleader-memory-")
        except OSError:
            continue
    raise ValueError("No temporary directory outside native Codex roots")


@contextmanager
def open_readonly(
    path: Path, paths: NativePaths, *, codex_home: Path | None = None
) -> Iterator[tuple[sqlite3.Connection, str]]:
    database = _snapshot_stamp(path, paths)
    if database is None:
        raise FileNotFoundError(path)
    wal = path.with_name(path.name + "-wal")
    journal = path.with_name(path.name + "-journal")
    wal_stamp = _snapshot_stamp(wal, paths)
    if database[1] + (wal_stamp[1] if wal_stamp else 0) > MAX_SNAPSHOT:
        raise ValueError(f"SQLite database and WAL exceed the {MAX_SNAPSHOT}-byte snapshot limit")
    if _snapshot_stamp(journal, paths) is not None:
        raise _DatabaseChanged(f"SQLite rollback journal present during scan: {journal}")

    def validate_snapshot() -> None:
        if (
            _snapshot_stamp(path, paths) != database
            or _snapshot_stamp(wal, paths) != wal_stamp
            or _snapshot_stamp(journal, paths) is not None
        ):
            raise _DatabaseChanged(f"SQLite store changed during scan: {path}")

    roots = (path.parent, codex_home) if codex_home else (path.parent,)
    with _temporary_snapshot_dir(roots, paths) as directory:
        snapshot = Path(directory) / path.name
        try:
            _copy_snapshot(path, snapshot, database, paths)
            if wal_stamp is not None:
                _copy_snapshot(wal, snapshot.with_name(wal.name), wal_stamp, paths)
        except FileNotFoundError as exc:
            raise _DatabaseChanged(f"SQLite store changed during scan: {path}") from exc
        validate_snapshot()
        with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True, timeout=0.25)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN")
            yield db, database[0]
            validate_snapshot()


def verified_schema(db: sqlite3.Connection) -> bool:
    rows = db.execute(
        "SELECT type, name, sql FROM sqlite_schema WHERE sql IS NOT NULL "
        "AND name NOT GLOB 'sqlite_*' AND name != '_sqlx_migrations' ORDER BY type, name"
    ).fetchall()
    encoded = json.dumps(rows, separators=(",", ":")).encode()
    if hashlib.sha256(encoded).hexdigest() != SCHEMA_DIGEST:
        return False
    migration_table = db.execute(
        "SELECT type FROM sqlite_schema WHERE name='_sqlx_migrations'"
    ).fetchone()
    if migration_table != ("table",):
        return False
    columns = [(row[1], row[2]) for row in db.execute("PRAGMA table_info(_sqlx_migrations)")]
    if columns != [
        ("version", "BIGINT"),
        ("description", "TEXT"),
        ("installed_on", "TIMESTAMP"),
        ("success", "BOOLEAN"),
        ("checksum", "BLOB"),
        ("execution_time", "BIGINT"),
    ]:
        return False
    try:
        migrations = db.execute(
            "SELECT version,description,success,lower(hex(checksum)) FROM _sqlx_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.Error:
        return False
    return migrations == list(MIGRATIONS)


def _record(
    row: Revision, db_path: Path, identity: str, workspace: str, reason: str | None
) -> MemoryRecord:
    store = str(db_path)
    ref = MemoryRef(
        "codex",
        memory_id("codex", store, row.thread_id),
        store,
        row.thread_id,
        digest(*row),
        workspace + ":" + digest(identity, row.thread_id, row.generated_at),
    )
    content = f"## Raw memory\n\n{row.raw_memory}\n\n## Rollout summary\n\n{row.rollout_summary}"
    return MemoryRecord(
        ref,
        row.rollout_slug or row.thread_id,
        None,
        None,
        "project",
        datetime.fromtimestamp(row.source_updated_at, UTC).isoformat(),
        "stage1",
        reason,
        content=content,
    )


@dataclass(frozen=True)
class CodexMemorySource:
    name: ClassVar[str] = "codex"
    codex_home: Path
    sqlite_home: Path | None
    projects: Mapping[str, str] = field(default_factory=dict)
    installed: bool = True
    paths: NativePaths = field(default_factory=NativePaths)
    root_error: str | None = None
    version_probe: Callable[[], str] | None = None

    def workspace(self, version: str) -> Path:
        return self.codex_home / ("memories" if version == "v1" else "memories_v2")

    def workspace_id(self, version: str) -> str:
        root = self.workspace(version)
        self.paths.check(root)
        identities = []
        for directory in (self.codex_home, root):
            try:
                info = directory.stat()
            except FileNotFoundError:
                identities.append("absent")
            else:
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError(f"Expected Codex workspace directory: {directory}")
                identities.append(file_identity(info))
        return digest(str(root.resolve()), *identities)

    def verify_workspace(self, ref: MemoryRef) -> None:
        if ":" not in ref.incarnation:
            raise ValueError(
                "This older approval lacks Codex artifact workspace evidence. For a present memory, "
                "refresh and review it again. For a removed memory, preserve the operation backup "
                "and verify the original artifact workspace manually before restoring."
            )
        for version, name in FILES.items():
            if self.sqlite_home and self.sqlite_home / name == Path(ref.store_id):
                if ref.incarnation.partition(":")[0] == self.workspace_id(version):
                    return
                raise ValueError(
                    "The reviewed Codex artifact workspace cannot be verified. Restore the original profile or refresh and review the memory again."
                )
        raise ValueError("Unrecognized Codex memory store")

    @property
    def hook_path(self) -> Path:
        return self.codex_home / "hooks.json"

    def protected_locations(self, ref: MemoryRef | None = None) -> tuple[Path, ...]:
        return (
            *(
                p
                for v in FILES
                for p in (self.workspace(v), self.workspace(v).with_suffix(".lock"))
            ),
            *(
                self.sqlite_home / (name + suffix)
                for name in FILES.values()
                if self.sqlite_home
                for suffix in ("", "-wal", "-shm", "-journal")
            ),
        )

    def instruction_config_roots(self) -> tuple[Path, ...]:
        return (self.codex_home,)

    def session_may_use_store(self, ref: MemoryRef, project: str | None, cwd: str | None) -> bool:
        return True

    def _projects(self, root: Path, keys: set[str]) -> tuple[dict[str, str], str | None]:
        result = dict(self.projects)
        try:
            with open_readonly(root / "state_5.sqlite", self.paths, codex_home=self.codex_home) as (
                db,
                _,
            ):
                primary_key = [
                    column[1] for column in db.execute("PRAGMA table_info(threads)") if column[5]
                ]
                if primary_key != ["id"]:
                    raise ValueError("Expected the native threads primary key on id")
                for batch in batched(sorted(keys), 256, strict=False):
                    params = ",".join("?" for _ in batch)
                    result.update(
                        (key, cwd)
                        for key, cwd in db.execute(
                            f"SELECT id, cwd FROM threads WHERE id IN ({params})", batch
                        )
                        if isinstance(cwd, str) and Path(cwd).is_absolute()
                    )
        except FileNotFoundError:
            pass
        except (OSError, ValueError, sqlite3.Error) as exc:
            return dict(self.projects), f"Codex project enrichment unavailable: {exc}"
        return result, None

    def _store(
        self, version: str, path: Path, *, selected: MemoryRef | None = None
    ) -> tuple[list[MemoryObservation], MemoryStoreStatus]:
        store = str(path)
        records: list[MemoryObservation] = []
        try:
            with open_readonly(path, self.paths, codex_home=self.codex_home) as (db, ident):
                verified = verified_schema(db)
                reason = None if verified else "Unverified Codex memory schema or SQLx migration"
                table = db.execute(
                    "SELECT type FROM sqlite_schema WHERE name='stage1_outputs'"
                ).fetchone()
                columns = {row[1] for row in db.execute("PRAGMA table_info(stage1_outputs)")}
                if table != ("table",) or not set(Revision._fields) <= columns:
                    return [], MemoryStoreStatus(
                        self.name,
                        store,
                        "unsupported",
                        "unavailable",
                        version,
                        "Unrecognized stage1_outputs table",
                    )
                errors = []
                try:
                    workspace = self.workspace_id(version)
                except (OSError, ValueError) as exc:
                    workspace = "unverified"
                    reason = reason or f"Codex artifact workspace unavailable: {exc}"
                query = f"SELECT {','.join(Revision._fields)} FROM stage1_outputs"
                query += " WHERE thread_id=?" if selected else " ORDER BY thread_id"
                for row in db.execute(query, (selected.source_ref,) if selected else ()):
                    try:
                        record = _record(Revision.decode(row), path, ident, workspace, reason)
                        records.append(record if selected else record.observation())
                    except (ValueError, OverflowError, OSError) as exc:
                        errors.append(str(exc))
                return records, MemoryStoreStatus(
                    self.name,
                    store,
                    "ready" if verified else "unsupported",
                    "partial" if errors or not verified else "complete",
                    version,
                    "; ".join(dict.fromkeys(errors)) or reason,
                )
        except FileNotFoundError:
            return [], MemoryStoreStatus(self.name, store, "absent", "complete", version, None)
        except _DatabaseChanged as exc:
            if selected:
                raise
            return [], MemoryStoreStatus(
                self.name, store, "unreadable", "partial", version, str(exc)
            )
        except (OSError, ValueError, sqlite3.Error) as exc:
            if selected and isinstance(exc, sqlite3.Error) and sqlite_busy(exc):
                raise NativeBusy("Native memory database is busy") from exc
            return [], MemoryStoreStatus(
                self.name, store, "unreadable", "unavailable", version, str(exc)
            )

    def read_selected(self, ref: MemoryRef) -> MemoryRecord:
        if self.sqlite_home is not None:
            for version, filename in FILES.items():
                path = self.sqlite_home / filename
                if str(path) == ref.store_id:
                    if not ref.incarnation.startswith("unverified:"):
                        self.verify_workspace(ref)
                    records, status = self._store(version, path, selected=ref)
                    if records and isinstance(records[0], MemoryRecord) and records[0].ref == ref:
                        return replace(records[0], project=self.projects.get(ref.source_ref))
                    if status.reason:
                        raise ValueError(status.reason)
        raise ValueError("Native memory disappeared or changed since review. Run `rl mem refresh`.")

    def mutation(self, ref: MemoryRef) -> AbstractContextManager[NativeMemoryTransaction]:
        return codex_mutation.mutation(self, ref)

    def cleanup_status(
        self, receipt: bytes
    ) -> Literal["pending", "completed", "failed", "unknown"]:
        return codex_mutation.cleanup_status(self, receipt)

    def scan(self) -> MemoryScan:
        records: list[MemoryObservation] = []
        stores: list[MemoryStoreStatus] = []
        if self.sqlite_home is None:
            stores.append(
                MemoryStoreStatus(
                    self.name,
                    str(self.codex_home),
                    "unsupported",
                    "unavailable",
                    None,
                    self.root_error,
                )
            )
        else:
            for version, filename in FILES.items():
                rows, status = self._store(version, self.sqlite_home / filename)
                records.extend(rows)
                stores.append(status)
            if records:
                projects, problem = self._projects(
                    self.sqlite_home, {r.ref.source_ref for r in records}
                )
                records = [replace(r, project=projects.get(r.ref.source_ref)) for r in records]
                if problem:
                    stores.append(
                        MemoryStoreStatus(
                            self.name,
                            str(self.sqlite_home / "state_5.sqlite"),
                            "unreadable",
                            "partial",
                            None,
                            problem,
                        )
                    )
        return MemoryScan(
            tuple(records),
            (harness_status(self.name, self.installed, stores),),
            tuple(stores),
            inventoried_sources=frozenset({self.name}) if self.sqlite_home else frozenset(),
        )
