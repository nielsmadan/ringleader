from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .compare import Normalization
from .model import Conversation, Identity, PreservationError
from .snapshot import SourceSnapshot

CONFIGURATION_PAIR_LENGTH = 2


@dataclass(frozen=True)
class NativeRoots:
    home: Path
    store: Path
    cwd: Path
    config: Path
    cache: Path


@dataclass(frozen=True)
class RunRequest:
    argv: tuple[str, ...]
    cwd: Path
    env: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class Runner(Protocol):
    def run(self, request: RunRequest) -> RunResult: ...


@dataclass(frozen=True)
class FormatProfile:
    agent: str
    name: str
    format_version: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Compatibility:
    supported: bool
    profile: FormatProfile | None
    diagnostics: tuple[str, ...]
    runtime_version: str | None = None
    schema_identity: str | None = None
    target_configuration: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _validate_configuration(self.target_configuration)

    def require(self) -> FormatProfile:
        if not self.supported or self.profile is None:
            raise PreservationError("Native compatibility: " + "; ".join(self.diagnostics))
        return self.profile


@dataclass(frozen=True)
class SourceRequest:
    identity: Identity
    roots: NativeRoots
    source_ref: str
    branch_id: str | None = None


@dataclass(frozen=True)
class NativeFile:
    relative_path: str
    data: bytes
    visibility: bool = False

    def __post_init__(self) -> None:
        path = Path(self.relative_path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or str(path) != self.relative_path
        ):
            raise PreservationError("Staged files require a safe relative path")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class NativeStage:
    identity: Identity
    profile: FormatProfile
    roots: NativeRoots
    files: tuple[NativeFile, ...]
    source_ref: str
    database_payload: bytes | None = None
    schema_identity: str | None = None

    def __post_init__(self) -> None:
        if len({item.relative_path for item in self.files}) != len(self.files):
            raise PreservationError("Duplicate staged native file path")
        if self.database_payload is not None and not self.schema_identity:
            raise PreservationError("Staged database payload requires a verified schema identity")


@dataclass(frozen=True)
class TargetRequest:
    identity: Identity
    roots: NativeRoots
    profile: FormatProfile
    title: str
    target_configuration: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _validate_configuration(self.target_configuration)


def _validate_configuration(value: tuple[tuple[str, str], ...]) -> None:
    if type(value) is not tuple or any(
        type(item) is not tuple
        or len(item) != CONFIGURATION_PAIR_LENGTH
        or any(type(part) is not str or not part for part in item)
        for item in value
    ):
        raise PreservationError("Target configuration requires nonempty string key/value pairs")
    if len({item[0] for item in value}) != len(value):
        raise PreservationError("Duplicate target configuration key")


class DeepReader(Protocol):
    def inspect_source(self, request: SourceRequest) -> Compatibility: ...
    def snapshot(self, request: SourceRequest) -> SourceSnapshot: ...


class NativeWriter(Protocol):
    def inspect_target(self, roots: NativeRoots, runner: Runner) -> Compatibility: ...
    def stage(self, conversation: Conversation, request: TargetRequest) -> NativeStage: ...
    def read_stage(self, stage: NativeStage) -> Conversation: ...
    def normalizations(self, profile: FormatProfile) -> tuple[Normalization, ...]: ...


class NativeLauncher(Protocol):
    def launch_request(self, identity: Identity, roots: NativeRoots) -> RunRequest: ...


@dataclass(frozen=True)
class NativeIndexIntent:
    relative_path: str
    data: bytes

    def __post_init__(self) -> None:
        NativeFile(self.relative_path, self.data)


class NativeIndex(Protocol):
    def index_intent(self, stage: NativeStage) -> NativeIndexIntent: ...
    def append_index(self, stage: NativeStage) -> bool: ...


class NativeRegistration(Protocol):
    def register_stage(self, stage: NativeStage, *, recover: bool = False) -> bool: ...


class NativeIdentityAllocator(Protocol):
    def allocate_identity(self) -> Identity: ...


@dataclass(frozen=True)
class Adapter:
    agent: str
    reader: DeepReader | None = None
    writer: NativeWriter | None = None
    launcher: NativeLauncher | None = None
    registration: NativeRegistration | None = None
    index: NativeIndex | None = None
    identity_allocator: NativeIdentityAllocator | None = None


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}

    def register(self, adapter: Adapter) -> None:
        if not adapter.agent or adapter.agent == "claude" or adapter.agent in self._adapters:
            raise PreservationError(f"Invalid or duplicate adapter: {adapter.agent}")
        self._adapters[adapter.agent] = adapter

    def get(self, agent: str) -> Adapter:
        canonical = "claude-code" if agent == "claude" else agent
        try:
            return self._adapters[canonical]
        except KeyError as exc:
            raise PreservationError(f"No portability adapter registered for {agent}") from exc

    def reader(self, agent: str) -> DeepReader:
        reader = self.get(agent).reader
        if reader is None:
            raise PreservationError(f"Deep reading is unavailable for {agent}")
        return reader

    def writer(self, agent: str) -> NativeWriter:
        writer = self.get(agent).writer
        if writer is None:
            raise PreservationError(f"Native writing is unavailable for {agent}")
        return writer

    def launcher(self, agent: str) -> NativeLauncher:
        launcher = self.get(agent).launcher
        if launcher is None:
            raise PreservationError(f"Native launch is unavailable for {agent}")
        return launcher
