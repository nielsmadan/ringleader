from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from ..memory_types import Progress
from .base import MAX_NATIVE_BYTES, NativePaths, file_identity

MAX_FILE = MAX_NATIVE_BYTES

FILE_PERMISSIONS = 0o777
MAX_PAYLOAD = 96 * 1024 * 1024


class NativeBusy(ValueError):
    pass


def pack(**fields: object) -> bytes:
    raw = json.dumps({"version": 1, **fields}, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > MAX_PAYLOAD:
        raise ValueError("Memory operation exceeds the recovery size limit")
    return raw


def unpack(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_PAYLOAD:
        raise ValueError("Oversized memory operation")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Unknown memory operation format")
    return data


def read_text(stream: BinaryIO) -> str:
    size = os.fstat(stream.fileno()).st_size
    if size > MAX_FILE:
        raise ValueError("Native memory file exceeds the recovery size limit")
    data = stream.read(size + 1)
    if len(data) != size:
        raise ValueError("Native memory file changed while reading")
    return data.decode()


@dataclass(frozen=True)
class FileImage:
    text: str | None
    identity: str | None
    mode: int = 0o600


def file_image(path: Path, paths: NativePaths) -> FileImage:
    try:
        with paths.open_file(path) as stream:
            info = os.fstat(stream.fileno())
            value = read_text(stream)
            end = os.fstat(stream.fileno())
            if info.st_ctime_ns != end.st_ctime_ns:
                raise ValueError(f"Native file changed during capture: {path.name}")
            return FileImage(value, file_identity(info), stat.S_IMODE(info.st_mode) & 0o777)
    except FileNotFoundError:
        return FileImage(None, None)


@dataclass(frozen=True)
class FileChange:
    before: FileImage
    after: str | None
    nonce: str
    mode: int

    @classmethod
    def create(cls, before: FileImage, after: str | None, *, mode: int | None = None) -> FileChange:
        return cls(before, after, uuid4().hex, before.mode if mode is None else mode)

    def data(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def parse(cls, data: dict[str, Any]) -> FileChange:
        before = FileImage(**data["before"])
        change = cls(before, data["after"], data["nonce"], data.get("mode", before.mode))
        if not re.fullmatch("[0-9a-f]{32}", change.nonce):
            raise ValueError("Invalid native staging identifier")
        for value in (change.before.text, change.after):
            if value is not None and (not isinstance(value, str) or len(value.encode()) > MAX_FILE):
                raise ValueError("Invalid native file image")
        if any(
            type(mode) is not int or not 0 <= mode <= FILE_PERMISSIONS
            for mode in (change.before.mode, change.mode)
        ):
            raise ValueError("Invalid native file permissions")
        return change


@contextmanager
def directory(path: Path, paths: NativePaths) -> Iterator[int]:
    paths.check(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _install(fd: int, path: Path, change: FileChange, paths: NativePaths) -> None:
    assert change.after is not None
    tmp = path.with_name(f".ringleader-{change.nonce}.new")
    expected = change.after.encode()
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(tmp.name, flags, 0o600, dir_fd=fd), "r+b") as out:
        info = os.fstat(out.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Unsafe staged replacement")
        prefix = out.read(len(expected) + 1)
        if prefix != expected:
            if info.st_nlink != 1 or not expected.startswith(prefix):
                raise ValueError("Incomplete or changed staged replacement")
            out.write(expected[len(prefix) :])
            out.flush()
        if info.st_nlink == 1:
            os.fchmod(out.fileno(), change.mode)
        os.fsync(out.fileno())
    os.fsync(fd)
    current = file_image(path, paths)
    if current.text is None:
        os.link(tmp.name, path.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.fsync(fd)
    elif current.identity != file_image(tmp, paths).identity:
        raise ValueError("Refusing to overwrite a recreated native file")


def _stage(fd: int, path: Path, staged: Path, before: FileImage, paths: NativePaths) -> None:
    os.rename(path.name, staged.name, src_dir_fd=fd, dst_dir_fd=fd)
    os.fsync(fd)
    if file_image(staged, paths) != before:
        os.link(staged.name, path.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.unlink(staged.name, dir_fd=fd)
        os.fsync(fd)
        raise ValueError("Native file changed while staging")


def apply_file(
    path: Path,
    paths: NativePaths,
    change: FileChange,
    component: str,
    *,
    progress: Progress,
    weak_guard: bool = False,
) -> None:
    staged = path.with_name(f".ringleader-{change.nonce}.stage")
    new = path.with_name(f".ringleader-{change.nonce}.new")
    current = file_image(path, paths)
    saved = file_image(staged, paths)
    if saved.text is not None and saved != change.before:
        raise ValueError(f"Staged native file differs from the reviewed file: {staged}")
    if component in progress.done:
        if current.text is None and change.before.text is None and change.after is None:
            return
        if current.text != change.after:
            raise ValueError(f"Completed native file has changed: {path.name}")
    elif current.text == change.before.text == change.after and saved.text is None:
        progress.checkpoint(component)
        return
    elif change.before.text is not None and saved.text is None:
        if current != change.before:
            raise ValueError(f"Native file changed or was recreated: {path.name}")
        if weak_guard and (current.identity or "").startswith("inode:"):
            raise ValueError("Native file continuity requires a fresh review")
    with directory(path.parent, paths) as fd:
        if component not in progress.done:
            if change.before.text is not None and saved.text is None:
                _stage(fd, path, staged, change.before, paths)
            if change.after is not None:
                _install(fd, path, change, paths)
            elif file_image(path, paths).text is not None:
                raise ValueError("A native file was recreated during removal")
            progress.checkpoint(component)
        saved = file_image(staged, paths)
        if saved.text is not None:
            if saved != change.before:
                raise ValueError(f"Staged native file differs from the reviewed file: {staged}")
            os.unlink(staged.name, dir_fd=fd)
            os.fsync(fd)
        if file_image(new, paths).text is not None:
            os.unlink(new.name, dir_fd=fd)
            os.fsync(fd)
