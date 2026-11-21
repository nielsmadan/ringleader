from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self, get_type_hints

from ..paths import ensure_state_dir, local_usage_path
from ..sources.base import stat_fingerprint
from .base import UsageRecord

_RECORD_TYPES = tuple(get_type_hints(UsageRecord).items())

VERSION = 2
MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_PENDING_FILES = 64
MAX_VARIANTS = 4
_APPLICATION_ID = 0x524C5547
DDL = (
    "CREATE TABLE IF NOT EXISTS entries ("
    "namespace TEXT, key TEXT, variant TEXT, signature TEXT, payload TEXT, checksum TEXT, "
    "PRIMARY KEY (namespace, key, variant))"
)


def decode_record(row: dict[str, Any]) -> UsageRecord:
    record = UsageRecord(**row)
    if any(not isinstance(getattr(record, key), kind) for key, kind in _RECORD_TYPES):
        raise TypeError("invalid usage record")
    return record


def _signature(paths: Iterable[Path]) -> str | None:
    values = []
    for path in paths:
        try:
            values.append(stat_fingerprint(path.stat()))
        except FileNotFoundError:
            values.append("missing")
        except OSError:
            return None
    return "|".join(values)


def _key(path: Path) -> str:
    return json.dumps(str(path.absolute()))


def _prepare(path: Path) -> Path:
    configured = ensure_state_dir().absolute()
    state = configured.resolve()
    for parent in {configured, *configured.parents, state, *state.parents}:
        found = parent.stat()
        writable = found.st_mode & 0o022
        if found.st_uid not in (0, os.getuid()) or (writable and not found.st_mode & stat.S_ISVTX):
            raise OSError("unsafe checkpoint ancestor")
    path = state / path.parent.name / path.name
    path.parent.mkdir(mode=0o700, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(directory).st_uid != os.getuid():
            raise OSError("unsafe checkpoint directory")
        os.fchmod(directory, 0o700)
        if not path.exists():
            _initialize(path)
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            found = os.fstat(fd)
            if (
                not stat.S_ISREG(found.st_mode)
                or found.st_nlink != 1
                or found.st_uid != os.getuid()
            ):
                raise OSError("unsafe checkpoint file")
            header = os.pread(fd, 100, 0)
            if (
                header[:16] != b"SQLite format 3\0"
                or int.from_bytes(header[68:72]) != _APPLICATION_ID
            ):
                raise OSError("unrecognized checkpoint database")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    return path


def _initialize(path: Path) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".local-usage-", dir=path.parent)
    os.close(fd)
    try:
        connection = sqlite3.connect(temporary, timeout=0.1)
        try:
            with connection:
                connection.execute("BEGIN")
                connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
                connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                connection.execute(DDL)
        finally:
            connection.close()
        with suppress(FileExistsError):
            os.link(temporary, path)
    finally:
        os.unlink(temporary)


@dataclass(frozen=True)
class _Write:
    key: str
    variant: str
    signature: str
    payload: str
    paths: tuple[Path, ...]


class Checkpoint:
    def __init__(self, source: str, root: Path, files: Iterable[Path]) -> None:
        self._namespace = json.dumps([source, str(root.absolute()), str(root)])
        self._keys = {_key(path) for path in files}
        self._connection: sqlite3.Connection | None = None
        self._pending: list[_Write] = []
        self._pending_bytes = 0

    def __enter__(self) -> Self:
        try:
            path = _prepare(local_usage_path(VERSION))
            self._connection = sqlite3.connect(path, timeout=0.1)
        except (OSError, sqlite3.Error):
            self._close()
        return self

    def _close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _flush(self) -> None:
        if not self._pending:
            return
        try:
            if self._connection is not None:
                with self._connection:
                    self._connection.execute("BEGIN IMMEDIATE")
                    for item in self._pending:
                        if item.signature != _signature(item.paths):
                            continue
                        self._connection.execute(
                            "INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                self._namespace,
                                item.key,
                                item.variant,
                                item.signature,
                                item.payload,
                                hashlib.sha256(item.payload.encode()).hexdigest(),
                            ),
                        )
                        self._connection.execute(
                            "DELETE FROM entries WHERE namespace=? AND key=? AND rowid NOT IN "
                            "(SELECT rowid FROM entries WHERE namespace=? AND key=? ORDER BY rowid DESC LIMIT ?)",
                            (self._namespace, item.key, self._namespace, item.key, MAX_VARIANTS),
                        )
        except (sqlite3.Error, UnicodeError):
            self._close()
        finally:
            self._pending.clear()
            self._pending_bytes = 0

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self._flush()
            if self._connection is not None and exc_type is None:
                candidates = [
                    key
                    for (key,) in self._connection.execute(
                        "SELECT DISTINCT key FROM entries WHERE namespace=?", (self._namespace,)
                    )
                    if key not in self._keys
                ]
                if candidates:
                    with self._connection:
                        self._connection.execute("BEGIN IMMEDIATE")
                        for key in candidates:
                            try:
                                Path(json.loads(key)).stat()
                            except FileNotFoundError:
                                self._connection.execute(
                                    "DELETE FROM entries WHERE namespace=? AND key=?",
                                    (self._namespace, key),
                                )
                            except (OSError, ValueError, TypeError):
                                continue
                if self._connection.execute("PRAGMA freelist_count").fetchone()[0]:
                    self._connection.execute("PRAGMA incremental_vacuum(128)").fetchall()
        except (sqlite3.Error, UnicodeError):
            pass
        finally:
            self._close()

    def load[T](
        self,
        path: Path,
        read: Callable[[], Iterable[T]],
        *,
        encode: Callable[[T], dict[str, Any]],
        decode: Callable[[list[dict[str, Any]]], Iterable[T]],
        companions: tuple[Path, ...] = (),
        variant: str = "",
    ) -> Iterator[T]:
        paths = (path, *companions)
        signature = _signature(paths)
        key = _key(path)
        if signature is None or self._connection is None:
            yield from read()
            return
        try:
            hit = self._connection.execute(
                "SELECT payload, checksum FROM entries WHERE namespace=? AND key=? AND variant=? "
                "AND signature=? AND length(CAST(payload AS BLOB))<=?",
                (self._namespace, key, variant, signature, MAX_PAYLOAD_BYTES),
            ).fetchone()
            if (
                hit is not None
                and isinstance(hit[0], str)
                and hashlib.sha256(hit[0].encode()).hexdigest() == hit[1]
            ):
                rows = json.loads(hit[0])
                if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
                    cached = list(decode(rows))
                    yield from cached
                    return
        except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError, AttributeError):
            pass
        parts: list[str] | None = []
        size = 2
        for item in read():
            if parts is not None:
                try:
                    encoded = json.dumps(encode(item), separators=(",", ":"))
                    size += len(encoded) + 1
                    if size > MAX_PAYLOAD_BYTES:
                        parts = None
                    else:
                        parts.append(encoded)
                except (TypeError, ValueError):
                    parts = None
            yield item
        if parts is not None and signature == _signature(paths):
            payload = "[" + ",".join(parts) + "]"
            self._pending.append(_Write(key, variant, signature, payload, paths))
            self._pending_bytes += len(payload)
            if self._pending_bytes >= MAX_PAYLOAD_BYTES or len(self._pending) >= MAX_PENDING_FILES:
                self._flush()

    def rows(
        self,
        path: Path,
        read: Callable[[], Iterable[dict[str, Any]]],
        *,
        companions: tuple[Path, ...] = (),
        variant: str = "",
    ) -> Iterator[dict[str, Any]]:
        yield from self.load(
            path,
            read,
            encode=lambda row: row,
            decode=lambda rows: rows,
            companions=companions,
            variant=variant,
        )

    def records(
        self,
        path: Path,
        read: Callable[[], Iterable[UsageRecord]],
        *,
        companions: tuple[Path, ...] = (),
        variant: str = "",
    ) -> Iterator[UsageRecord]:
        try:
            yield from self.load(
                path,
                read,
                encode=asdict,
                decode=lambda rows: (decode_record(row) for row in rows),
                companions=companions,
                variant=variant,
            )
        except (OSError, sqlite3.Error):
            return
