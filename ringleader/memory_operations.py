from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from . import memory_catalog, memory_journal, publish_state, registry
from .memory_sources.base import digest, file_identity, memory_id
from .memory_types import MemoryOrigin, MemoryRef
from .stateio import publish_lock

type Decision = Literal["harvest", "discard"]
type Cleanup = Literal["not-needed", "pending", "completed", "failed", "unknown"]
type SourceState = Literal["present", "removal-pending", "removed", "restored"]


InvalidOperation = memory_journal.InvalidOperation
root = memory_journal.root
_directory = memory_journal.directory


@dataclass
class Operation:
    id: str
    registry_path: str
    ref: MemoryRef
    native: MemoryRef
    name: str
    project: str | None
    scope: Literal["project", "global"]
    decision: Decision
    created: str
    harvest_id: str | None
    source_state: SourceState = "present"
    cleanup: Cleanup = "not-needed"
    sequence: int = 0
    revision: int = 0
    approval_hash: str | None = None
    saved: bool = False
    remove_requested: bool = False
    restoring: bool = False
    retention: int = 30
    expires: str | None = None
    receipt: str | None = None
    done: bool = False
    purged: bool = False
    progress: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    direct_paths: dict[str, str] = field(default_factory=dict)

    physical_key: str | None = None
    shared_sequence: bool = False
    _body: memory_journal.Body | None = field(default=None, init=False, repr=False, compare=False)
    _payload_keys: frozenset[str] = field(
        default_factory=frozenset, init=False, repr=False, compare=False
    )
    _legacy: bool = field(default=False, init=False, repr=False, compare=False)
    _header_only: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def origin(self) -> MemoryOrigin:
        ref = self.ref
        return MemoryOrigin(
            ref.memory_id, ref.source, ref.fingerprint, ref.incarnation, self.scope, self.project
        )

    @property
    def identity(self) -> tuple[str, str]:
        return self.ref.source, self.ref.memory_id

    @property
    def payload_keys(self) -> frozenset[str]:
        return (
            self._payload_keys if self._header_only and not self.purged else frozenset(self.payload)
        )

    @property
    def native_finished(self) -> bool:
        return not self.restoring and self.source_state in {"removed", "restored"}

    @classmethod
    def new(
        cls, ref: MemoryRef, row: dict[str, Any], decision: Decision, path: Path, now: datetime
    ) -> Operation:
        return cls(
            uuid4().hex,
            str(path.resolve()),
            ref,
            memory_catalog.native_ref(row),
            row["name"],
            row.get("project"),
            row["scope"],
            decision,
            now.isoformat(),
            uuid4().hex if decision == "harvest" else None,
            physical_key=physical_key(ref),
            shared_sequence=True,
        )

    def write(self) -> None:
        if self._legacy and self._header_only and not self.purged:
            original = load(self.id, Path(self.registry_path))
            self.payload = original.payload
            self.name = original.name
            self._header_only = False
        revision = self.revision + 1
        metadata = {f.name: getattr(self, f.name) for f in fields(self) if f.init} | {
            "revision": revision,
            "ref": asdict(self.ref),
            "native": asdict(self.native),
            "payload": dict.fromkeys(self.payload_keys, True),
            "name": self.name[: memory_journal.TITLE_PREVIEW],
        }
        content: dict[str, Any] | None = None
        if not self._header_only or self.purged:
            content = {"payload": self.payload} if self.payload else {}
            if self.name != metadata["name"]:
                content["name"] = self.name
        self._body = memory_journal.write(_directory(self.id), metadata, content, self._body)
        self._legacy = False
        self.revision = revision

    def component(self, name: str) -> None:
        if name not in self.progress:
            self.progress.append(name)
            self.write()

    def project_state(self, reg: dict[str, Any]) -> None:
        decisions = reg.setdefault("memoryDecisions", {})
        decision = decisions.setdefault(self.id, {})
        if decision.get("lifecycleRevision", -1) > self.revision:
            raise ValueError("Memory operation projection has a newer revision")
        decision.update(
            source=self.ref.source,
            memoryId=self.ref.memory_id,
            incarnation=self.ref.incarnation,
            fingerprint=self.ref.fingerprint,
            storeId=self.ref.store_id,
            sourceRef=self.ref.source_ref,
            sourceState=self.source_state,
            decision=self.decision,
            harvestId=self.harvest_id,
            cleanup=self.cleanup,
            journal=self.id,
            recoveryExpiresAt=self.expires,
            recoveryAvailable="snapshot" in self.payload_keys,
            lifecycleRevision=self.revision,
            approvalHash=self.approval_hash,
            sequence=self.sequence,
            directPaths=list(self.direct_paths),
        )
        if self.source_state == "removed":
            rows = memory_catalog.rows_by_id(reg)
            key = self.ref.source, self.ref.memory_id
            row = rows.get(key)
            if row and memory_catalog.reviewed_ref(row) == self.ref:
                del rows[key]
                reg["memoryEntries"] = list(rows.values())

    def project_registry(self, path: Path) -> None:
        registry.mutate_registry(path, self.project_state)


def physical_key(ref: MemoryRef) -> str:
    store = Path(ref.store_id)
    key = Path(ref.source_ref).name if ref.source == "claude-code" else ref.source_ref
    return digest(ref.source, file_identity(store.stat()), key)


def _decode(data: dict[str, Any], operation_id: str, path: Path) -> Operation:
    try:
        ref = MemoryRef(**data["ref"])
        native = MemoryRef(**data["native"])
        operation = Operation(**(data | {"ref": ref, "native": native}))
        _validate(operation, operation_id, path)
    except (KeyError, ValueError, TypeError) as exc:
        journal = _directory(operation_id) / "journal.json"
        raise InvalidOperation(f"Invalid memory operation journal {journal}: {exc}") from exc
    return operation


def _validate(operation: Operation, operation_id: str, path: Path) -> None:
    if (
        operation.id != operation_id
        or not registry.owner_matches(operation.registry_path, path)
        or operation.decision not in ("harvest", "discard")
        or operation.source_state not in ("present", "removal-pending", "removed", "restored")
        or operation.ref
        != MemoryRef(**(asdict(operation.native) | {"incarnation": operation.ref.incarnation}))
    ):
        raise ValueError("Mismatched memory operation identity")
    if (
        operation.scope not in ("project", "global")
        or operation.cleanup not in ("not-needed", "pending", "completed", "failed", "unknown")
        or type(operation.retention) is not int
        or operation.retention < 0
        or type(operation.revision) is not int
        or operation.revision < 1
        or type(operation.sequence) is not int
        or operation.sequence < 1
        or not isinstance(operation.payload, dict)
        or not isinstance(operation.direct_paths, dict)
        or not isinstance(operation.progress, list)
        or not all(isinstance(item, str) for item in operation.progress)
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in operation.direct_paths.items()
        )
        or type(operation.shared_sequence) is not bool
        or (
            operation.physical_key is not None
            and not re.fullmatch(r"[0-9a-f]{64}", operation.physical_key)
        )
        or not isinstance(operation.name, str)
        or not all(
            value is None or isinstance(value, str)
            for value in (operation.project, operation.approval_hash, operation.receipt)
        )
        or not all(
            isinstance(value, str) and value
            for ref in (operation.ref, operation.native)
            for value in asdict(ref).values()
        )
        or not all(
            type(flag) is bool
            for flag in (
                operation.saved,
                operation.done,
                operation.purged,
                operation.restoring,
                operation.remove_requested,
            )
        )
        or (operation.decision == "harvest") != bool(operation.harvest_id)
        or (
            operation.harvest_id is not None
            and not re.fullmatch("[0-9a-f]{32}", operation.harvest_id)
        )
    ):
        raise ValueError("Invalid memory operation metadata")
    timestamps = (
        (operation.created,)
        if operation.expires is None
        else (
            operation.created,
            operation.expires,
        )
    )
    if any(datetime.fromisoformat(value).utcoffset() is None for value in timestamps):
        raise ValueError("Expected timezone-aware memory operation timestamps")


def _read_operation(
    directory: Path, *, selected: bool | Callable[[Operation], bool], owner: Path | None = None
) -> Operation:
    data, body, legacy = memory_journal.read_header(directory)
    owner_path = owner or Path(data["registry_path"])
    operation = _decode(data, directory.name, owner_path)
    selected = selected(operation) if callable(selected) else selected
    operation._body = body
    operation._legacy = legacy
    if not legacy:
        operation._header_only = not selected
        if selected and body:
            payload = memory_journal.read_body(directory, body)
            content = payload.get("payload", {})
            if not isinstance(content, dict) or content.keys() != operation.payload.keys():
                raise InvalidOperation(
                    f"Invalid memory operation payload projection for {directory}"
                )
            name = payload.get("name", operation.name)
            if not isinstance(name, str) or name[: memory_journal.TITLE_PREVIEW] != operation.name:
                raise InvalidOperation(f"Invalid memory operation title for {directory}")
            operation.payload = content
            operation.name = name
    if not selected:
        operation._payload_keys = frozenset(operation.payload)
        operation.payload = {}
        operation._header_only = True
    return operation


def load(operation_id: str, path: Path) -> Operation:
    return _read_operation(_directory(operation_id), selected=True, owner=path)


def operation_header(operation_id: str, path: Path) -> Operation:
    return _read_operation(_directory(operation_id), selected=False, owner=path)


def durable(operation_id: str) -> bool:
    return (_directory(operation_id) / "journal.json").is_file()


def cleanup_checkpoints() -> None:
    memory_journal.root()
    with publish_lock():
        memory_journal.cleanup_abandoned()
        history(cleanup=True)


def _different_lineage(projection: dict[str, Any], operation_id: str, ref: MemoryRef) -> bool:
    source = projection.get("source")
    key = projection.get("sourceRef")
    store = projection.get("storeId")
    if (
        source not in ("codex", "claude-code")
        or not isinstance(key, str)
        or not isinstance(store, str)
    ):
        return False
    entry_key = Path(key).name if source == "claude-code" else key
    return (
        projection.get("journal") == operation_id
        and projection.get("memoryId") == memory_id(source, store, entry_key)
        and (source != ref.source or (source == "codex" and key != ref.source_ref))
    )


def history(
    *,
    registry_path: Path | None = None,
    ref: MemoryRef | None = None,
    cleanup: bool = False,
    select: Callable[[Operation], bool] | None = None,
    allow_missing_headers: bool = False,
) -> list[Operation]:
    return list(
        _iter_history(
            registry_path=registry_path,
            ref=ref,
            cleanup=cleanup,
            select=select,
            allow_missing_headers=allow_missing_headers,
        )
    )


def _iter_history(
    *,
    registry_path: Path | None = None,
    ref: MemoryRef | None = None,
    cleanup: bool = False,
    select: Callable[[Operation], bool] | None = None,
    allow_missing_headers: bool = False,
) -> Iterable[Operation]:
    for directory in memory_journal.directories():
        if not directory.is_dir():
            raise InvalidOperation(f"Invalid memory operation directory {directory}")
        try:
            if not (directory / "journal.json").exists():
                if allow_missing_headers:
                    continue
                memory_journal.check_headerless(directory)
                if cleanup:
                    memory_journal.discard_uncommitted(directory)
                continue
            op = _read_operation(directory, selected=select or False)
        except (InvalidOperation, OSError):
            if registry_path is None or ref is None:
                raise
            projection = (
                registry.load_registry(registry_path)
                .get("memoryDecisions", {})
                .get(directory.name, {})
            )
            if not _different_lineage(projection, directory.name, ref):
                raise
            continue
        if cleanup:
            memory_journal.discard_stranded(directory, op._body)
        yield op


def harvest_operations(ids: set[str], *, owners_bound: bool = False) -> dict[str, Operation]:
    matches = {}
    for op in _iter_history(allow_missing_headers=owners_bound):
        if op.harvest_id is not None and op.harvest_id in ids:
            if op.harvest_id in matches:
                raise InvalidOperation(
                    f"Conflicting lifecycle operations for memory harvest {op.harvest_id}"
                )
            matches[op.harvest_id] = op
    return matches


def owned(records: Iterable[Operation], path: Path) -> list[Operation]:
    canonical = str(path.resolve())
    return [op for op in records if registry.owner_matches(op.registry_path, canonical)]


def operations(path: Path) -> list[Operation]:
    return owned(history(), path)


type LineageKey = tuple[str, str, str]


def _lineage_keys(op: Operation) -> tuple[LineageKey, ...]:
    keys = [
        (op.ref.source, "id", op.ref.memory_id),
        (op.ref.source, "native", op.native.incarnation.rsplit(":", 1)[-1]),
    ]
    if op.physical_key:
        keys.append((op.ref.source, "store", op.physical_key))
    return tuple(keys)


class Lineages:
    def __init__(self, records: Iterable[Operation]) -> None:
        self._groups: dict[LineageKey, int] = {}
        self._records: dict[int, list[Operation]] = {}
        for index, op in enumerate(records):
            keys = _lineage_keys(op)
            matches = {self._groups[key] for key in keys if key in self._groups}
            group = min(matches, default=index)
            current = self._records.setdefault(group, [])
            for other in matches - {group}:
                joined = self._records.pop(other)
                for previous in joined:
                    self._groups.update(dict.fromkeys(_lineage_keys(previous), group))
                current.extend(joined)
            current.append(op)
            self._groups.update(dict.fromkeys(keys, group))

    def related(self, op: Operation) -> list[Operation]:
        matches = {self._groups[key] for key in _lineage_keys(op) if key in self._groups}
        return [other for group in matches for other in self._records[group]]

    def check_approval(self, op: Operation, *, require_latest: bool) -> None:
        if any(
            other.id != op.id and "intent" in other.payload_keys and not other.native_finished
            for other in self.related(op)
        ):
            raise ValueError("An unfinished native operation owns this memory; recover it first")
        if require_latest and self.latest(op).id != op.id:
            raise ValueError("A newer decision owns this memory identity")

    def latest(self, op: Operation) -> Operation:
        lineage = self.related(op)
        local_owners = {other.registry_path for other in lineage if not other.shared_sequence}
        if len(local_owners) > 1:
            raise InvalidOperation(
                "Cannot order legacy decisions across registries for the same native memory; reconcile this history before native changes"
            )
        newest = max(lineage, key=lambda other: other.sequence, default=op)
        if any(other.id != newest.id and other.sequence == newest.sequence for other in lineage):
            raise InvalidOperation(
                "Conflicting decision sequence for the same native memory; reconcile this history before native changes"
            )
        return newest


def needs_projection(op: Operation, reg: dict[str, Any]) -> bool:
    projected = reg.get("memoryDecisions", {}).get(op.id, {})
    revision: int = projected.get("lifecycleRevision", 0)
    return op.revision > revision


def needs_harvest_repair(op: Operation, reg: dict[str, Any]) -> bool:
    if op.decision != "harvest" or op.restoring or op.native_finished or op.done:
        return False
    projected = reg.get("memoryDecisions", {}).get(op.id, {})
    return bool(
        not op.saved
        or "writing" in op.payload_keys
        or (
            not op.direct_paths
            and not projected.get("harvestToken")
            and op.harvest_id not in reg.get("memoryPublications", {})
        )
    )


def _unfinished(op: Operation, reg: dict[str, Any], *, superseded: bool) -> bool:
    if needs_projection(op, reg):
        return True
    if op.restoring:
        return not superseded
    if op.done:
        return (op.retention == 0 and not op.purged) or (
            not superseded
            and op.receipt is not None
            and op.cleanup in {"pending", "failed", "unknown"}
        )
    if op.native_finished:
        return True
    return needs_harvest_repair(op, reg) or (not superseded and op.remove_requested)


@dataclass(frozen=True)
class RecoveryListing:
    rows: tuple[dict[str, Any], ...]
    retry: frozenset[str]
    expired: tuple[str, ...]


def expired(op: Operation, now: datetime) -> bool:
    return bool(
        op.done
        and not op.restoring
        and not op.purged
        and op.expires
        and datetime.fromisoformat(op.expires) <= now
    )


def recovery_listing(path: Path, now: datetime) -> RecoveryListing:
    reg = registry.load_registry(path)
    records = history()
    lineages = Lineages(records)
    local = owned(records, path)
    terminal = {op.id for op in local if op.native_finished and op.harvest_id}
    updates = publish_state.memory_queue_updates(reg, operation_ids=terminal)
    rows: list[dict[str, Any]] = []
    retry: set[str] = set()
    for op in sorted(
        local,
        key=lambda item: (datetime.fromisoformat(item.created), item.sequence, item.id),
        reverse=True,
    ):
        try:
            newest = lineages.latest(op)
            successor = newest.id if newest.id != op.id else None
            row = recovery_entry(op, now, successor)
            if op.id in updates or _unfinished(op, reg, superseded=successor is not None):
                retry.add(op.id)
            problem = memory_journal.body_problem(_directory(op.id), op._body) if op._body else None
            if problem:
                row.update(recoveryAvailable=False, restorable=False, diagnostic=problem)
            if problem or successor or op.source_state != "present" or op.id in retry:
                rows.append(row)
        except InvalidOperation as exc:
            row = recovery_entry(op, now, None)
            row.update(restorable=False, diagnostic=str(exc))
            rows.append(row)
    return RecoveryListing(
        tuple(rows), frozenset(retry), tuple(op.id for op in local if expired(op, now))
    )


def recovery_targets(path: Path) -> tuple[str, ...]:
    return tuple(sorted(recovery_listing(path, datetime.now(UTC)).retry))


def recovery_entry(op: Operation, now: datetime, superseded_by: str | None) -> dict[str, Any]:
    retained = not op.purged and "snapshot" in op.payload_keys
    restorable = (
        superseded_by is None
        and op.source_state == "removed"
        and retained
        and not op.restoring
        and op.expires is not None
        and datetime.fromisoformat(op.expires) > now
    )
    return {
        "operationId": op.id,
        "memoryId": op.ref.memory_id,
        "source": op.ref.source,
        "storeId": op.ref.store_id,
        "sourceRef": op.ref.source_ref,
        "fingerprint": op.ref.fingerprint,
        "incarnation": op.ref.incarnation,
        "name": op.name,
        "scope": op.scope,
        "project": op.project,
        "decision": op.decision,
        "sourceState": op.source_state,
        "cleanup": op.cleanup,
        "restoring": op.restoring,
        "harvestId": op.harvest_id,
        "recoveryExpiresAt": op.expires,
        "recoveryAvailable": retained,
        "restorable": restorable,
        "supersededBy": superseded_by,
    }
