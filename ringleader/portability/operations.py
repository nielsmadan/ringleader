from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from ..stateio import atomic_write_bytes
from .capabilities import (
    Compatibility,
    FormatProfile,
    NativeFile,
    NativeIndexIntent,
    NativeRoots,
    NativeStage,
)
from .compact import CompactReport
from .model import Conversation, Identity, PreservationError, unique_object, validate
from .serialization import _decode, _encode
from .snapshot import SourceSnapshot, TransformOptions


@dataclass(frozen=True)
class StoredRoots:
    home: str
    store: str
    cwd: str
    config: str
    cache: str
    data: str | None = None
    state: str | None = None
    temporary: str | None = None

    @classmethod
    def capture(cls, roots: NativeRoots) -> StoredRoots:
        return cls(
            str(roots.home.resolve()),
            str(roots.store.resolve()),
            str(roots.cwd.resolve()),
            str(roots.config.resolve()),
            str(roots.cache.resolve()),
            *(
                str(value.resolve()) if value is not None else None
                for value in (roots.data, roots.state, roots.temporary)
            ),
        )

    def native(self) -> NativeRoots:
        paths = tuple(
            Path(value) if (value := getattr(self, key)) is not None else None
            for key in self.__annotations__
        )
        if any(
            not path.is_absolute() or str(path.resolve()) != str(path)
            for path in paths
            if path is not None
        ):
            raise PreservationError("Operation roots changed or are not canonical absolute paths")
        return NativeRoots(
            Path(self.home),
            Path(self.store),
            Path(self.cwd),
            Path(self.config),
            Path(self.cache),
            *(
                Path(value) if value is not None else None
                for value in (self.data, self.state, self.temporary)
            ),
        )


@dataclass(frozen=True)
class StoredStage:
    identity: Identity
    profile: FormatProfile
    roots: StoredRoots
    files: tuple[NativeFile, ...]
    source_ref: str
    database_payload: bytes | None
    schema_identity: str | None

    @classmethod
    def capture(cls, stage: NativeStage) -> StoredStage:
        return cls(
            stage.identity,
            stage.profile,
            StoredRoots.capture(stage.roots),
            stage.files,
            stage.source_ref,
            stage.database_payload,
            stage.schema_identity,
        )

    def native(self) -> NativeStage:
        NativeFile(self.source_ref, b"")
        return NativeStage(
            self.identity,
            self.profile,
            self.roots.native(),
            self.files,
            self.source_ref,
            self.database_payload,
            self.schema_identity,
        )


@dataclass(frozen=True)
class Artifacts:
    version: int
    snapshot: SourceSnapshot
    conversation: Conversation
    stage: StoredStage
    compatibility: Compatibility
    index_intent: NativeIndexIntent | None
    options: TransformOptions
    kind: Literal["convert", "fork"]
    report: CompactReport | None

    def key(self) -> str:
        return digest(
            encode(
                (
                    self.snapshot.identity(self.options),
                    (self.stage.roots.store, self.stage.roots.cwd),
                    self.stage.profile,
                    self.compatibility.schema_identity,
                    self.compatibility.target_configuration,
                    self.conversation.title,
                )
            )
        )


@dataclass(frozen=True)
class Step:
    name: str
    phase: Literal["intent", "done"]
    device: int = 0
    inode: int = 0
    publisher_token: str = ""


@dataclass(frozen=True)
class Operation:
    version: int
    id: str
    key: str
    artifacts_digest: str
    source: Identity
    target: Identity
    created_at: str
    state: Literal["prepared", "running", "complete", "rolled_back"]
    steps: tuple[Step, ...] = ()
    last_error: str | None = None
    terminal_lifecycle: Literal["archived", "deleted"] | None = None
    abandoned_private: tuple[str, ...] = ()

    def step(self, name: str) -> Step | None:
        return next((step for step in self.steps if step.name == name), None)

    def with_step(self, step: Step) -> Operation:
        prior = self.step(step.name)
        steps = tuple(step if item.name == step.name else item for item in self.steps)
        return replace(self, state="running", steps=steps if prior else (*steps, step))


def encode(value: object) -> bytes:
    return json.dumps(_encode(value), sort_keys=True, separators=(",", ":")).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_directory(path: Path) -> None:
    if path.exists():
        return
    durable_directory(path.parent)
    with suppress(FileExistsError):
        path.mkdir(mode=0o700)
    fsync_directory(path.parent)


def decode[T](data: bytes, expected: type[T]) -> T:
    try:
        return cast(T, _decode(json.loads(data, object_pairs_hook=unique_object), expected))
    except (ValueError, TypeError, UnicodeError) as exc:
        raise PreservationError(f"Invalid conversion operation: {exc}") from exc


class OperationStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def directory(self, operation_id: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
            raise PreservationError("Invalid conversion operation ID")
        path = self.root / operation_id
        if path.is_symlink() or path.resolve() != path:
            raise PreservationError("Operation directory must not be a symlink")
        return path

    def _path(self, operation_id: str, name: str) -> Path:
        path = self.directory(operation_id) / name
        if path.is_symlink():
            raise PreservationError("Operation artifacts must not be symlinks")
        return path

    def ids(self) -> tuple[str, ...]:
        if not self.root.exists():
            return ()
        return tuple(
            sorted(
                path.name
                for path in self.root.iterdir()
                if re.fullmatch(r"[0-9a-f]{32}", path.name) and (path / "journal.json").exists()
            )
        )

    def load(self, operation_id: str) -> Operation:
        operation = decode(self._path(operation_id, "journal.json").read_bytes(), Operation)
        if operation.version != 1 or operation.id != operation_id:
            raise PreservationError("Unknown conversion journal version or identity")
        names = [step.name for step in operation.steps]
        if len(set(names)) != len(names):
            raise PreservationError("Duplicate conversion boundary")
        if any(
            re.fullmatch(r"index|database|registry|prepare:\d+|file:\d+", name) is None
            for name in names
        ):
            raise PreservationError("Unknown conversion boundary")
        return operation

    def artifacts(self, operation: Operation) -> Artifacts:
        data = self._path(operation.id, "artifacts.json").read_bytes()
        if digest(data) != operation.artifacts_digest:
            raise PreservationError("Conversion artifacts changed; refusing recovery")
        artifacts = decode(data, Artifacts)
        stage = artifacts.stage.native()
        validate(artifacts.snapshot.conversation)
        validate(artifacts.conversation)
        if (
            artifacts.version != 1
            or artifacts.key() != operation.key
            or artifacts.snapshot.conversation.source != operation.source
            or stage.identity != operation.target
        ):
            raise PreservationError("Conversion artifacts disagree with journal identity")
        self._validate_steps(operation, stage, has_index=artifacts.index_intent is not None)
        return artifacts

    def _validate_steps(self, operation: Operation, stage: NativeStage, *, has_index: bool) -> None:
        names = [step.name for step in operation.steps]
        file_names = [
            name
            for index in range(len(stage.files))
            for name in (f"prepare:{index}", f"file:{index}")
        ]
        expected = (["index"] if has_index else []) + file_names
        expected += (["database"] if stage.database_payload is not None else []) + ["registry"]
        if operation.terminal_lifecycle:
            native_steps = tuple(step for step in operation.steps if step.name != "registry")
            native_names = [step.name for step in native_steps]
            if native_names != expected[: len(native_names)] or any(
                step.phase != "done" for step in native_steps[:-1]
            ):
                raise PreservationError("Retired conversion has invalid native boundaries")
            if "registry" in names and names[-1] != "registry":
                raise PreservationError("Retired registry boundary must be last")
        elif names != expected[: len(names)] or any(
            step.phase != "done" for step in operation.steps[:-1]
        ):
            raise PreservationError("Conversion boundaries are incomplete or out of order")
        if operation.state == "prepared" and names:
            raise PreservationError("Prepared conversion has publication boundaries")
        if operation.state == "complete":
            complete = (
                operation.step("registry") == Step("registry", "done")
                if operation.terminal_lifecycle
                else names == expected and all(step.phase == "done" for step in operation.steps)
            )
            if not complete:
                raise PreservationError("Completed conversion lacks publication receipts")
        for step in operation.steps:
            if step.publisher_token and (
                not step.name.startswith("prepare:")
                or re.fullmatch(r"[0-9a-f]{32}", step.publisher_token) is None
            ):
                raise PreservationError("Invalid private publisher generation")
            if (
                step.name.startswith("file:")
                or (step.name.startswith("prepare:") and step.phase == "done")
            ) and (step.device < 0 or step.inode <= 0):
                raise PreservationError("Missing durable publisher inode evidence")

    def create(self, operation: Operation, artifacts: Artifacts) -> None:
        durable_directory(self.root)
        directory = self.directory(operation.id)
        directory.mkdir(mode=0o700)
        fsync_directory(self.root)
        self.save(operation)
        atomic_write_bytes(self._path(operation.id, "artifacts.json"), encode(artifacts))

    def save(self, operation: Operation) -> None:
        atomic_write_bytes(self._path(operation.id, "journal.json"), encode(operation))
