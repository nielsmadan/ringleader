from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .model import Conversation, PreservationError
from .serialization import dumps


@dataclass(frozen=True)
class SnapshotFile:
    name: str
    data: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class TransformOptions:
    compact: bool = False
    version: int = 1
    portable_version: int = 0


@dataclass(frozen=True)
class SourceSnapshot:
    conversation: Conversation
    files: tuple[SnapshotFile, ...]
    format_profile: str
    metadata: tuple[tuple[str, str], ...] = ()

    def identity(self, options: TransformOptions | None = None) -> str:
        options = options or TransformOptions()
        if options.version != 1:
            raise PreservationError("Unsupported transform version")
        names = [item.name for item in self.files]
        keys = [key for key, _ in self.metadata]
        if len(set(names)) != len(names) or len(set(keys)) != len(keys):
            raise PreservationError("Duplicate source snapshot file or metadata identity")
        payload = {
            "version": 1,
            "canonical": hashlib.sha256(dumps(self.conversation)).hexdigest(),
            "files": sorted((item.name, item.digest) for item in self.files),
            "profile": self.format_profile,
            "metadata": sorted(self.metadata),
            "transform": {
                "compact": options.compact,
                "version": options.version,
                **({"portable": options.portable_version} if options.portable_version else {}),
            },
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def _identity(stat: os.stat_result) -> tuple[int, ...]:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def snapshot_files(
    paths: tuple[tuple[str, Path], ...], *, max_bytes: int, attempts: int = 3
) -> tuple[SnapshotFile, ...]:
    if attempts < 1 or max_bytes < 1:
        raise ValueError("Snapshot requires positive byte and retry bounds")
    if len({name for name, _ in paths}) != len(paths):
        raise PreservationError("Duplicate snapshot path label")
    failure: Exception | None = None
    for _ in range(attempts):
        try:
            before = {name: _identity(path.stat()) for name, path in paths}
            result = []
            remaining = max_bytes
            for name, path in paths:
                with path.open("rb") as stream:
                    if _identity(os.fstat(stream.fileno())) != before[name]:
                        raise PreservationError("Source changed before reading")
                    data = stream.read(remaining + 1)
                    if len(data) > remaining:
                        raise PreservationError("Source exceeds snapshot byte limit")
                    remaining -= len(data)
                    if _identity(os.fstat(stream.fileno())) != before[name]:
                        raise PreservationError("Source changed while reading")
                result.append(SnapshotFile(name, data))
            if any(_identity(path.stat()) != before[name] for name, path in paths):
                raise PreservationError("Source bundle changed while reading")
            return tuple(result)
        except (OSError, PreservationError) as exc:
            failure = exc
    raise PreservationError(
        f"Cannot obtain a complete stable source snapshot after {attempts} attempts: {failure}"
    ) from failure


@contextmanager
def database_snapshot(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        connection.execute("SELECT name FROM sqlite_schema").fetchall()
        yield connection
    finally:
        connection.rollback()
        connection.close()
