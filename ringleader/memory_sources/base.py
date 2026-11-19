from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable

from ..memory_types import (
    MemoryRecord,
    MemoryRef,
    MemoryScan,
    MemorySourceStatus,
    MemoryStoreStatus,
)

MAX_NATIVE_BYTES = 8 * 1024 * 1024
MAX_CONFIG_DEPTH = 128


@runtime_checkable
class SelectedReader(Protocol):
    def read_selected(self, ref: MemoryRef) -> MemoryRecord: ...


class MemoryReader(Protocol):
    @property
    def name(self) -> str: ...
    def scan(self) -> MemoryScan: ...


def check_config_depth(data: object, path: Path) -> None:
    parents = [iter((data,))]
    while parents:
        try:
            value = next(parents[-1])
        except StopIteration:
            parents.pop()
            continue
        if isinstance(value, (dict, list)):
            if len(parents) > MAX_CONFIG_DEPTH:
                raise ValueError(f"Excessively nested native configuration: {path}")
            parents.append(iter(value.values() if isinstance(value, dict) else value))


def digest(*values: object) -> str:
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def memory_id(source: str, store: str, key: str) -> str:
    return digest(source, store, key)


def file_identity(info: os.stat_result) -> str:
    birth = getattr(info, "st_birthtime_ns", None)
    strength = "birth" if birth is not None else "inode"
    return f"{strength}:{digest(info.st_dev, info.st_ino, birth)}"


@dataclass(frozen=True)
class NativePaths:
    boundary: Path | None = None

    def check(self, path: Path) -> None:
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError(f"expected an absolute native path without traversal: {path}")
        if self.boundary is not None and not path.is_relative_to(self.boundary):
            raise ValueError(f"path outside the fixture root: {path}")
        for part in (*reversed(path.parents), path):
            if part.is_symlink():
                raise ValueError(f"symlink in native path: {part}")

    @contextmanager
    def open_file(self, path: Path) -> Iterator[BinaryIO]:
        self.check(path)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open(path.anchor, directory_flags)
        try:
            for part in path.parts[1:-1]:
                child = os.open(part, directory_flags, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(f"expected a regular file: {path}")
            yield stream

    def read(self, path: Path) -> tuple[bytes, os.stat_result]:
        with self.open_file(path) as stream:
            fd = stream.fileno()
            info = os.fstat(fd)
            if info.st_size > MAX_NATIVE_BYTES:
                raise ValueError(f"Native file exceeds the {MAX_NATIVE_BYTES}-byte limit: {path}")
            data = stream.read(info.st_size + 1)
            if len(data) > MAX_NATIVE_BYTES:
                raise ValueError(f"Native file exceeds the {MAX_NATIVE_BYTES}-byte limit: {path}")
            end = os.fstat(fd)
            if (info.st_mtime_ns, info.st_size) != (end.st_mtime_ns, end.st_size):
                raise ValueError(f"file changed during scan: {path}")
            return data, end


def harness_status(
    name: str, installed: bool, stores: list[MemoryStoreStatus]
) -> MemorySourceStatus:
    if not installed:
        return MemorySourceStatus(name, "not-installed", "CLI executable not found")
    if stores and all(store.state in {"unsupported", "unreadable"} for store in stores):
        return MemorySourceStatus(name, "unavailable", "No readable supported store")
    return MemorySourceStatus(name, "ready", None)
