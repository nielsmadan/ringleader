from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .memory_sources.base import NativePaths
from .memory_sources.transactions import MAX_PAYLOAD, pack, unpack
from .paths import state_dir
from .stateio import sync_directory

HEADER_LIMIT = 128 * 1024
TITLE_PREVIEW = 256
OPERATION_ID = re.compile(r"[0-9a-f]{32}")
_BODY = re.compile(r"body-([1-9][0-9]*)\.json")
_PREFIX = b'{"version":1,"format":"split",'


class InvalidOperation(ValueError):
    pass


def root() -> Path:
    base = state_dir()
    path = base / "memory" / "operations"
    NativePaths().check(path)
    return path


def directory(operation_id: str) -> Path:
    if not OPERATION_ID.fullmatch(operation_id):
        raise ValueError("Invalid memory operation ID")
    result = root() / operation_id
    for path in (result, result / "journal.json"):
        if path.is_symlink():
            raise ValueError("Symlink in memory operation directory")
    return result


def directories() -> list[Path]:
    base = root()
    if not base.exists():
        return []
    return [directory(p.name) for p in sorted(base.iterdir()) if OPERATION_ID.fullmatch(p.name)]


@dataclass(frozen=True)
class Body:
    revision: int
    digest: str
    size: int
    modified_ns: int = 0

    @property
    def filename(self) -> str:
        return f"body-{self.revision}.json"

    @classmethod
    def parse(cls, data: object, revision: int) -> Body | None:
        if data is None:
            return None
        if not isinstance(data, dict):
            raise TypeError("Invalid body reference")
        body = cls(**data)
        if (
            type(body.revision) is not int
            or not 0 < body.revision <= revision
            or type(body.size) is not int
            or not 0 < body.size <= MAX_PAYLOAD
            or type(body.modified_ns) is not int
            or not re.fullmatch(r"[0-9a-f]{64}", body.digest)
        ):
            raise ValueError("Invalid body reference")
        return body


def read_header(path: Path) -> tuple[dict[str, Any], Body | None, bool]:
    journal = path / "journal.json"
    kind = "legacy or unreadable"
    try:
        with NativePaths().open_file(journal) as stream:
            prefix = stream.read(len(_PREFIX))
            limit = HEADER_LIMIT if prefix == _PREFIX else MAX_PAYLOAD
            size = os.fstat(stream.fileno()).st_size
            if size > limit:
                raise ValueError("Oversized journal")
            # Read the observed size, avoiding an allocation the size of the legacy limit.
            raw = prefix + stream.read(size - len(prefix) + 1)
            if len(raw) != size:
                raise ValueError("Journal changed while reading")
        envelope = unpack(raw)
        metadata = envelope["operation"]
        if not isinstance(metadata, dict) or not isinstance(metadata.get("registry_path"), str):
            raise TypeError("Invalid operation metadata")
        if "format" not in envelope:
            return metadata, None, True
        kind = "split"
        if envelope["format"] != "split" or len(raw) > HEADER_LIMIT:
            raise ValueError("Invalid operation header format or size")
        body = Body.parse(envelope["body"], metadata["revision"])
        if not isinstance(metadata.get("payload"), dict) or not all(
            value is True for value in metadata["payload"].values()
        ):
            raise ValueError("Invalid operation payload projection")
        if metadata["payload"] and body is None:
            raise ValueError("Missing operation body reference")
        return metadata, body, False
    except (KeyError, ValueError, TypeError, RecursionError) as exc:
        raise InvalidOperation(
            f"Invalid memory operation journal {journal} ({kind}); "
            f"restore the journal from backup to establish lineage: {exc}"
        ) from exc


def read_body(path: Path, body: Body) -> dict[str, Any]:
    filename = path / body.filename
    try:
        with NativePaths().open_file(filename) as stream:
            if os.fstat(stream.fileno()).st_size != body.size:
                raise ValueError("Recovery payload size changed")
            raw = stream.read(body.size + 1)
        if len(raw) != body.size or hashlib.sha256(raw).hexdigest() != body.digest:
            raise ValueError("Recovery payload digest changed")
        data = unpack(raw)
        if data["operationId"] != path.name or data["revision"] != body.revision:
            raise ValueError("Mismatched recovery payload identity")
        return data
    except (OSError, KeyError, ValueError, TypeError, RecursionError) as exc:
        raise InvalidOperation(f"Invalid memory operation payload {filename}: {exc}") from exc


def body_problem(path: Path, body: Body) -> str | None:
    filename = path / body.filename
    try:
        stamp = filename.lstat()
        if not stat.S_ISREG(stamp.st_mode) or stamp.st_size != body.size:
            raise ValueError("Recovery payload size or type changed")
        if stamp.st_mtime_ns != body.modified_ns:
            with NativePaths().open_file(filename) as stream:
                checksum = hashlib.sha256()
                remaining = body.size
                while remaining:
                    chunk = stream.read(min(remaining, 65536))
                    if not chunk:
                        break
                    checksum.update(chunk)
                    remaining -= len(chunk)
                if remaining or stream.read(1) or checksum.hexdigest() != body.digest:
                    raise ValueError("Recovery payload digest changed")
    except (OSError, ValueError) as exc:
        return f"Invalid memory operation payload {filename}: {exc}"
    return None


def _replace(path: Path, pending: str, destination: str, raw: bytes) -> None:
    tmp = path / pending
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "wb") as out:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError("Invalid operation checkpoint file")
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        out.write(raw)
        out.flush()
        os.fsync(fd)
    tmp.replace(path / destination)
    sync_directory(path)


def discard_stranded(path: Path, keep: Body | None) -> None:
    removed = False
    for item in path.iterdir():
        if item.name in ("journal.pending", "body.pending") or (
            _BODY.fullmatch(item.name) and (keep is None or item.name != keep.filename)
        ):
            item.unlink()
            removed = True
    if removed:
        sync_directory(path)
    marker = path / "initial.pending"
    if marker.exists():
        marker.unlink()
        sync_directory(path)


def _private_directory(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise InvalidOperation(f"Memory operation requires a private state directory: {path}")


def _initial_files(path: Path, *, staging: bool = False) -> set[str]:
    _private_directory(path)
    allowed = {"initial.pending", "journal.pending"}
    if not staging:
        allowed.update(("body-1.json", "body.pending"))
    names = set()
    for item in path.iterdir():
        info = item.lstat()
        if (
            item.name not in allowed
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or ((staging or item.name == "initial.pending") and info.st_size != 0)
        ):
            raise InvalidOperation(f"Unrecognized initial memory checkpoint: {item}")
        names.add(item.name)
    return names


def check_headerless(path: Path) -> None:
    if not (path / "initial.pending").exists():
        raise InvalidOperation(
            f"Missing memory operation journal {path}; restore its header from backup to establish ownership"
        )
    _initial_files(path)


def discard_uncommitted(path: Path) -> None:
    abandoned = path.with_name(f".abandoned-{path.name}")
    if abandoned.exists() or abandoned.is_symlink():
        raise InvalidOperation(
            f"Run bulk memory recovery to retire the earlier checkpoint: {abandoned}"
        )
    path.rename(abandoned)
    sync_directory(path.parent)
    discard_stranded(abandoned, None)
    abandoned.rmdir()
    sync_directory(path.parent)


def cleanup_abandoned() -> None:
    base = root()
    if not base.exists():
        return
    _private_directory(base.parent)
    _private_directory(base)
    for path in base.iterdir():
        prefix, _, operation_id = path.name.partition("-")
        if prefix not in {".initial", ".abandoned"} or not OPERATION_ID.fullmatch(operation_id):
            continue
        names = _initial_files(path, staging=prefix == ".initial")
        if prefix == ".abandoned" and names and "initial.pending" not in names:
            raise InvalidOperation(f"Unrecognized abandoned memory checkpoint: {path}")
        discard_stranded(path, None)
        path.rmdir()
        sync_directory(base)


def write(
    path: Path, metadata: dict[str, Any], content: dict[str, Any] | None, previous: Body | None
) -> Body | None:
    if path != directory(path.name):
        raise InvalidOperation(f"Invalid memory operation path: {path}")
    for item in (path.parent.parent, path.parent):
        if not item.exists():
            item.mkdir(mode=0o700)
            sync_directory(item.parent)
        _private_directory(item)
    if not path.exists():
        if metadata["revision"] != 1:
            raise InvalidOperation(f"Missing memory operation directory: {path}")
        stage = path.with_name(f".initial-{path.name}")
        stage.mkdir(mode=0o700)
        _replace(stage, "journal.pending", "initial.pending", b"")
        stage.rename(path)
        sync_directory(path.parent)
    else:
        _private_directory(path)
        if not (path / "journal.json").exists():
            check_headerless(path)
    body = previous
    raw = b""
    if content is not None:
        body = None
        if content:
            revision = metadata["revision"]
            raw = pack(operationId=path.name, revision=revision, **content)
            body = Body(revision, hashlib.sha256(raw).hexdigest(), len(raw))
    if raw and body:
        _replace(path, "body.pending", body.filename, raw)
        stamp = (path / body.filename).stat()
        body = Body(body.revision, body.digest, body.size, stamp.st_mtime_ns)
    descriptor = asdict(body) if body else None
    header = pack(format="split", operation=metadata, body=descriptor)
    if len(header) > HEADER_LIMIT:
        raise ValueError("Memory operation metadata exceeds the 128 KiB header limit")
    _replace(path, "journal.pending", "journal.json", header)
    discard_stranded(path, body)
    return body
