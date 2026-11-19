from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    from .gitinfo import GitIdentity


@dataclass(frozen=True)
class MemoryRef:
    source: str
    memory_id: str
    store_id: str
    source_ref: str
    fingerprint: str
    incarnation: str


@dataclass(frozen=True)
class MemoryObservation:
    ref: MemoryRef
    name: str
    description: str | None
    project: str | None
    scope: Literal["project", "global"]
    mtime: str
    mem_type: str | None
    mutation_block: str | None
    obsolete_signals: tuple[str, ...] = ()
    is_inline: bool = False


@dataclass(frozen=True)
class MemoryRecord(MemoryObservation):
    content: str = ""

    def observation(self) -> MemoryObservation:
        return MemoryObservation(
            self.ref,
            self.name,
            self.description,
            self.project,
            self.scope,
            self.mtime,
            self.mem_type,
            self.mutation_block,
            self.obsolete_signals,
            self.is_inline,
        )


@dataclass(frozen=True)
class MemorySourceStatus:
    source: str
    status: Literal["ready", "not-installed", "not-supported", "unavailable"]
    reason: str | None


@dataclass(frozen=True)
class MemoryStoreStatus:
    source: str
    store_id: str
    state: Literal["ready", "absent", "unreadable", "unsupported"]
    completeness: Literal["complete", "partial", "unavailable"]
    schema_version: str | None
    reason: str | None


@dataclass(frozen=True)
class MemoryScan:
    records: tuple[MemoryObservation, ...]
    statuses: tuple[MemorySourceStatus, ...]
    stores: tuple[MemoryStoreStatus, ...]
    project_identities: Mapping[str, GitIdentity] = field(default_factory=dict)
    inventoried_sources: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MemorySnapshot:
    ref: MemoryRef
    payload: bytes


@dataclass(frozen=True)
class NativeRemoval:
    cleanup: Literal["not-needed", "pending"]
    receipt: bytes | None


@dataclass(frozen=True)
class NativeIntent:
    action: Literal["remove", "restore"]
    payload: bytes


@dataclass(frozen=True)
class Progress:
    done: frozenset[str]
    checkpoint: Callable[[str], None]


class NativeMemoryTransaction(Protocol):
    def capture(self) -> MemorySnapshot: ...
    def prepare(
        self, snapshot: MemorySnapshot, *, action: Literal["remove", "restore"], now: datetime
    ) -> NativeIntent: ...
    def apply(
        self, intent: NativeIntent, *, now: datetime, progress: Progress
    ) -> NativeRemoval: ...


@runtime_checkable
class MemorySource(Protocol):
    name: str

    def scan(self) -> MemoryScan: ...
    def read_selected(self, ref: MemoryRef) -> MemoryRecord: ...
    def verify_workspace(self, ref: MemoryRef) -> None: ...
    @property
    def hook_path(self) -> Path: ...
    def protected_locations(self, ref: MemoryRef | None = None) -> tuple[Path, ...]: ...
    def instruction_config_roots(self) -> tuple[Path, ...]: ...
    def session_may_use_store(
        self, ref: MemoryRef, project: str | None, cwd: str | None
    ) -> bool: ...
    def mutation(self, ref: MemoryRef) -> AbstractContextManager[NativeMemoryTransaction]: ...
    def cleanup_status(
        self, receipt: bytes
    ) -> Literal["pending", "completed", "failed", "unknown"]: ...


@dataclass(frozen=True)
class SessionOrigin:
    session_id: str


@dataclass(frozen=True)
class MemoryOrigin:
    memory_id: str
    source: str
    fingerprint: str
    incarnation: str
    source_scope: Literal["project", "global"]
    source_project: str | None


type HarvestOrigin = SessionOrigin | MemoryOrigin


def origin_fields(origin: HarvestOrigin) -> dict[str, str | None]:
    if isinstance(origin, SessionOrigin):
        return {"sessionId": origin.session_id}
    return {
        "originKind": "memory",
        "memoryId": origin.memory_id,
        "source": origin.source,
        "memoryFingerprint": origin.fingerprint,
        "memoryIncarnation": origin.incarnation,
        "sourceScope": origin.source_scope,
        "sourceProject": origin.source_project,
    }


def parse_origin(fields: Mapping[str, object]) -> HarvestOrigin:
    def required(key: str) -> str:
        value = fields.get(key)
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.splitlines()) != 1
            or "\x00" in value
            or value != value.strip()
        ):
            raise ValueError(f"invalid {key}")
        return value

    memory_keys = {
        "originKind",
        "memoryId",
        "source",
        "memoryFingerprint",
        "memoryIncarnation",
        "sourceScope",
        "sourceProject",
    }
    if not fields.keys() & memory_keys:
        return SessionOrigin(required("sessionId"))
    if (
        fields.get("originKind") != "memory"
        or "sessionId" in fields
        or not memory_keys <= fields.keys()
    ):
        raise ValueError("invalid harvest origin")
    scope = required("sourceScope")
    if scope not in {"project", "global"}:
        raise ValueError("invalid sourceScope")
    project = required("sourceProject") if fields["sourceProject"] is not None else None
    return MemoryOrigin(
        required("memoryId"),
        required("source"),
        required("memoryFingerprint"),
        required("memoryIncarnation"),
        cast(Literal["project", "global"], scope),
        project,
    )
