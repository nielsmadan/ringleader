from __future__ import annotations

import fcntl
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import registry
from ..paths import conversion_operations_dir, default_registry
from .capabilities import (
    AdapterRegistry,
    NativeIndexIntent,
    NativeMetadata,
    NativeRoots,
    NativeStage,
    NativeWriter,
    Runner,
    RunRequest,
    RunResult,
    SourceRequest,
    TargetRequest,
)
from .compact import PORTABLE_TRANSFORM_VERSION, CompactReport, compact, portable
from .compare import Normalization, compare
from .model import Conversation, Identity, PreservationError
from .operations import (
    OPERATION_VERSION,
    Artifacts,
    Operation,
    OperationStore,
    Step,
    StoredRoots,
    StoredStage,
    digest,
    durable_directory,
    encode,
    fsync_directory,
)
from .snapshot import TransformOptions


@dataclass(frozen=True)
class ConversionRequest:
    source: SourceRequest
    target_agent: str
    target_roots: NativeRoots
    compact: bool = False
    fork: bool = False
    title: str | None = None


@dataclass(frozen=True)
class ConversionResult:
    operation_id: str | None
    source: Identity
    target: Identity
    lifecycle: str
    source_ref: str
    reused: bool = False
    dry_run: bool = False
    report: CompactReport | None = None
    diagnostics: tuple[str, ...] = ()
    normalizations: tuple[str, ...] = ()


Probes = tuple[tuple[tuple[str, ...], RunResult], ...]


@dataclass(frozen=True)
class PreparedConversion:
    source: Identity
    target: Identity
    artifacts: Artifacts
    # prepare() read the stage back and probed the target; publish reuses both
    checked: bool = False
    probes: Probes = ()


class _Recording:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self.results: dict[tuple[str, ...], RunResult] = {}

    def run(self, request: RunRequest) -> RunResult:
        result = self.runner.run(request)
        self.results[request.argv] = result
        return result


class _Replay:
    # keyed by argv: each probe runs in its own scratch roots, so env and cwd always differ
    def __init__(self, runner: Runner, probes: Probes) -> None:
        self.runner = runner
        self.probes = dict(probes)

    def run(self, request: RunRequest) -> RunResult:
        if request.argv in self.probes:
            return self.probes[request.argv]
        return self.runner.run(request)


@dataclass(frozen=True)
class BatchResult:
    source: Identity
    result: ConversionResult | None = None
    error: str | None = None


def _record(reg: dict[str, Any], target: Identity) -> tuple[str, dict[str, Any] | None]:
    for bucket, lifecycle in (
        ("deleted", "deleted"),
        ("archived", "archived"),
        ("threads", "active"),
    ):
        record = registry.find_session(reg, target.native_id, source=target.agent, bucket=bucket)
        if record is not None:
            return lifecycle, record
    return "missing", None


def _applied(writer: NativeWriter, stage: NativeStage) -> tuple[Normalization, ...]:
    if stage.normalizations is None:
        # a journal written before normalizations were recorded predates today's
        # declared set; a writer that narrowed its set since then exposes the
        # wider, pre-recording one so a stage flattened at write time still compares
        legacy = getattr(writer, "legacy_normalizations", None)
        return legacy(stage.profile) if legacy is not None else writer.normalizations(stage.profile)
    return stage.normalizations


def _check_stage(expected: Conversation, writer: NativeWriter, stage: NativeStage) -> None:
    actual = writer.read_stage(stage)
    applied = _applied(writer, stage)
    if (
        stage.normalizations is not None
        and expected.checkpoints
        and not actual.checkpoints
        and all(item.kind != "flattened-checkpoint" for item in applied)
    ):
        raise PreservationError("Native stage dropped checkpoints without recording a flattening")
    compare(expected, actual, applied)


def _reuse_identity(artifacts: Artifacts) -> tuple[object, ...]:
    return (
        artifacts.snapshot.identity(artifacts.options),
        (artifacts.stage.roots.store, artifacts.stage.roots.cwd),
        artifacts.stage.profile,
        artifacts.conversation.title,
    )


def _lineage(record: dict[str, Any]) -> dict[str, Any] | None:
    while record:
        if record.get("lineage"):
            lineage: dict[str, Any] = record["lineage"]
            return lineage
        record = record.get("thread") or {}
    return None


RETIRED = frozenset({"archived", "deleted", "missing"})


class ConversionService:
    def __init__(
        self,
        adapters: AdapterRegistry,
        runner: Runner,
        *,
        registry_path: Path | None = None,
        operations_path: Path | None = None,
        fault: Callable[[str, Operation], None] | None = None,
    ) -> None:
        self.adapters = adapters
        self.runner = runner
        self.registry_path = registry_path or default_registry()
        self.store = OperationStore(operations_path or conversion_operations_dir())
        self.fault = fault

    @contextmanager
    def _lock(self) -> Iterator[None]:
        # Operation lock precedes the short registry mutation lock.
        if self.store.root.resolve() != self.store.root:
            raise PreservationError("Conversion state root changed")
        durable_directory(self.store.root)
        if (self.store.root / "operations.lock").is_symlink():
            raise PreservationError("Conversion operation lock must not be a symlink")
        descriptor = os.open(self.store.root / "operations.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _point(self, name: str, operation: Operation) -> None:
        if self.fault:
            self.fault(name, operation)

    def _save(self, operation: Operation, label: str) -> Operation:
        self._point(f"before:{label}", operation)
        self.store.save(operation)
        self._point(f"after:{label}", operation)
        return operation

    def _step(self, operation: Operation, step: Step) -> Operation:
        return self._save(operation.with_step(step), f"{step.name}:{step.phase}")

    def convert(self, request: ConversionRequest, *, dry_run: bool = False) -> ConversionResult:
        prepared = self.prepare(request)
        return self.preview(prepared) if dry_run else self.publish(prepared)

    def prepare(self, request: ConversionRequest) -> PreparedConversion:
        source = replace(request.source, roots=StoredRoots.capture(request.source.roots).native())
        snapshot = self.adapters.reader(source.identity.agent).snapshot(source)
        if snapshot.conversation.source != source.identity:
            raise PreservationError("Source snapshot identity disagrees with selection")
        options = TransformOptions(
            compact=request.compact, portable_version=PORTABLE_TRANSFORM_VERSION
        )
        snapshot.identity(options)
        roots = StoredRoots.capture(replace(request.target_roots, cwd=source.roots.cwd)).native()
        adapter = self.adapters.get(request.target_agent)
        writer = self.adapters.writer(adapter.agent)
        recording = _Recording(self.runner)
        compatibility = writer.inspect_target(roots, recording)
        profile = compatibility.require()
        original = portable(snapshot.conversation, adapter.agent)
        if request.title is not None:
            original = replace(original, title=request.title)
        transformed = compact(original) if options.compact else None
        conversation = transformed.conversation if transformed else original
        identity = (
            adapter.identity_allocator.allocate_identity()
            if adapter.identity_allocator
            else Identity(adapter.agent, str(uuid4()))
        )
        stage = writer.stage(
            conversation,
            TargetRequest(
                identity,
                roots,
                profile,
                conversation.title or "Imported session",
                compatibility.target_configuration,
                compatibility.runtime_version,
            ),
        )
        expected = replace(conversation, title=conversation.title or "Imported session")
        _check_stage(expected, writer, stage)
        self._metadata(stage, expected.title)
        if stage.identity != identity or stage.roots != roots or stage.profile != profile:
            raise PreservationError("Native stage disagrees with inspected target")
        if stage.schema_identity != compatibility.schema_identity:
            raise PreservationError("Native schema changed during staging")
        artifacts = Artifacts(
            OPERATION_VERSION,
            snapshot,
            expected,
            StoredStage.capture(stage),
            compatibility,
            adapter.index.index_intent(stage) if adapter.index else None,
            options,
            "fork" if request.fork else "convert",
            transformed.report if transformed else None,
            str(self.registry_path.resolve()),
        )
        return PreparedConversion(
            source.identity,
            identity,
            artifacts,
            checked=True,
            probes=tuple(recording.results.items()),
        )

    def as_fork(self, prepared: PreparedConversion) -> PreparedConversion:
        return replace(prepared, artifacts=replace(prepared.artifacts, kind="fork"))

    def preview(self, prepared: PreparedConversion) -> ConversionResult:
        artifacts = prepared.artifacts
        diagnostics = (*artifacts.compatibility.diagnostics, *artifacts.compatibility.warnings)
        prior = None if artifacts.kind == "fork" else self._prior(artifacts.key())
        if prior is None:
            return ConversionResult(
                None,
                prepared.source,
                prepared.target,
                "preview",
                artifacts.stage.source_ref,
                dry_run=True,
                report=artifacts.report,
                diagnostics=diagnostics,
            )
        operation, prior_artifacts = prior
        self._check_registry(prior_artifacts)
        return ConversionResult(
            operation.id,
            operation.source,
            operation.target,
            self._prior_lifecycle(operation, prior_artifacts),
            prior_artifacts.stage.source_ref,
            reused=True,
            dry_run=True,
            report=prior_artifacts.report,
            diagnostics=diagnostics,
        )

    def publish(self, prepared: PreparedConversion) -> ConversionResult:
        artifacts = prepared.artifacts
        reuse_key = artifacts.key()
        with self._lock():
            if artifacts.kind == "convert":
                prior = self._prior(reuse_key)
                if prior is not None:
                    return self._run(*prior, reused=True)
            encoded = encode(artifacts)
            operation = Operation(
                OPERATION_VERSION,
                uuid4().hex,
                reuse_key,
                digest(encoded),
                prepared.source,
                prepared.target,
                datetime.now(UTC).isoformat(),
                "prepared",
                artifacts.registry_path,
            )
            self._point("before:create", operation)
            self.store.create(operation, encoded)
            self._point("after:create", operation)
            return self._run(operation, artifacts, checked=prepared.checked, probes=prepared.probes)

    def _prior(self, key: str) -> tuple[Operation, Artifacts] | None:
        for operation_id in self.store.matching(key=key):
            prior = self.store.load(operation_id)
            if prior.key == key and prior.state != "rolled_back":
                try:
                    prior_artifacts = self.store.artifacts(prior)
                except FileNotFoundError:
                    continue  # a concurrent publish wrote the journal but not artifacts.json yet
                if prior_artifacts.kind == "convert":
                    return prior, prior_artifacts
        return None

    def _prior_lifecycle(self, operation: Operation, artifacts: Artifacts) -> str:
        if operation.terminal_lifecycle:
            return operation.terminal_lifecycle
        lifecycle, _ = _record(registry.load_registry(self.registry_path), operation.target)
        if lifecycle in ("archived", "deleted"):
            return lifecycle
        if operation.state != "complete":
            return "pending"
        return self._result(operation, artifacts, reused=True).lifecycle

    def reusable(self, request: ConversionRequest) -> ConversionResult | None:
        source = replace(request.source, roots=StoredRoots.capture(request.source.roots).native())
        snapshot = self.adapters.reader(source.identity.agent).snapshot(source)
        options = TransformOptions(
            compact=request.compact, portable_version=PORTABLE_TRANSFORM_VERSION
        )
        agent = self.adapters.get(request.target_agent).agent
        roots = StoredRoots.capture(replace(request.target_roots, cwd=source.roots.cwd))
        wanted = (
            snapshot.identity(options),
            (roots.store, roots.cwd),
            self.adapters.writer(agent).profile,
            (request.title if request.title is not None else snapshot.conversation.title)
            or "Imported session",
        )
        candidates = sorted(
            (
                self.store.load(operation_id)
                for operation_id in self.store.matching(source=source.identity, target_agent=agent)
            ),
            key=lambda item: item.created_at,
            reverse=True,
        )
        forks: list[ConversionResult] = []
        for operation in candidates:
            if (
                operation.source != source.identity
                or operation.target.agent != agent
                or operation.state != "complete"
                or operation.registry_path != str(self.registry_path.resolve())
            ):
                continue
            try:
                artifacts = self.store.artifacts(operation)
            except FileNotFoundError:
                continue
            if _reuse_identity(artifacts) != wanted:
                continue
            result = ConversionResult(
                operation.id,
                operation.source,
                operation.target,
                self._prior_lifecycle(operation, artifacts),
                artifacts.stage.source_ref,
                reused=True,
                report=artifacts.report,
                normalizations=tuple(item.kind for item in artifacts.stage.normalizations or ()),
            )
            if artifacts.kind == "fork":
                forks.append(result)
                continue
            if result.lifecycle not in RETIRED:
                return result
            # a fork made after this convert was retired replaces it for later resumes
            return next((item for item in forks if item.lifecycle not in RETIRED), result)
        return None

    def batch(
        self, requests: tuple[ConversionRequest, ...], *, dry_run: bool = False
    ) -> tuple[BatchResult, ...]:
        results = []
        for request in requests:
            try:
                results.append(
                    BatchResult(request.source.identity, self.convert(request, dry_run=dry_run))
                )
            except (OSError, ValueError, RuntimeError, TypeError, LookupError) as exc:
                results.append(BatchResult(request.source.identity, error=str(exc)))
        return tuple(results)

    def _check_registry(self, artifacts: Artifacts | Operation) -> None:
        if artifacts.registry_path != str(self.registry_path.resolve()):
            raise PreservationError(
                f"Conversion belongs to registry {artifacts.registry_path}; repeat with --registry {artifacts.registry_path}"
            )

    def resume(self, operation_id: str) -> ConversionResult:
        with self._lock():
            operation = self.store.load(operation_id)
            self._check_registry(operation)
            if operation.state == "rolled_back":
                raise PreservationError("Conversion was rolled back; start a new conversion")
            return self._run(operation, self.store.artifacts(operation), reused=True)

    def transactions(self) -> tuple[Operation, ...]:
        return tuple(self.store.load(key) for key in self.store.ids())

    def lineage(self, identity: Identity) -> tuple[dict[str, Any], ...]:
        reg = registry.load_registry(self.registry_path)
        entries = dict(reg.get("conversionLineage", {}))
        for bucket in ("threads", "archived", "deleted"):
            for record in reg[bucket]:
                item = _lineage(record)
                if item:
                    entries.setdefault(item["operationId"], item)
        qualified = {"agent": identity.agent, "nativeId": identity.native_id}
        return tuple(
            item for item in entries.values() if qualified in (item["source"], item["target"])
        )

    def _metadata(self, stage: NativeStage, title: str | None) -> None:
        actual = self.adapters.writer(stage.identity.agent).stage_metadata(stage)
        if actual != NativeMetadata(stage.identity, str(stage.roots.cwd), title):
            raise PreservationError(
                "Native picker metadata does not preserve target identity, cwd and title"
            )

    def _configuration(self, artifacts: Artifacts, probes: Probes = ()) -> None:
        stage = artifacts.stage.native()
        current = self.adapters.writer(stage.identity.agent).inspect_target(
            stage.roots, _Replay(self.runner, probes)
        )
        if (current.require(), current.schema_identity, current.target_configuration) != (
            stage.profile,
            artifacts.compatibility.schema_identity,
            artifacts.compatibility.target_configuration,
        ):
            raise PreservationError(
                "Target profile/schema/configuration changed; no further publication"
            )

    def _run(
        self,
        operation: Operation,
        artifacts: Artifacts,
        *,
        reused: bool = False,
        checked: bool = False,
        probes: Probes = (),
    ) -> ConversionResult:
        self._check_registry(artifacts)
        try:
            return self._resume(operation, artifacts, reused=reused, checked=checked, probes=probes)
        except (OSError, ValueError, RuntimeError, TypeError, LookupError) as exc:
            current = self.store.load(operation.id)
            self.store.save(replace(current, last_error=str(exc)))
            raise

    def _resume(
        self,
        operation: Operation,
        artifacts: Artifacts,
        *,
        reused: bool = False,
        checked: bool = False,
        probes: Probes = (),
    ) -> ConversionResult:
        lifecycle, record = _record(registry.load_registry(self.registry_path), operation.target)
        if record is not None and not operation.steps:
            raise PreservationError("Registry target identity collision before publication")
        existing_lineage = _lineage(record) if record is not None else None
        if existing_lineage is not None and existing_lineage.get("operationId") != operation.id:
            raise PreservationError("Registry target identity collision")
        if operation.state == "complete":
            return self._result(operation, artifacts, reused=reused)
        if operation.terminal_lifecycle or lifecycle in ("archived", "deleted"):
            operation = self._retire(operation, artifacts, lifecycle, record)
            return self._result(operation, artifacts, reused=reused)
        stage = artifacts.stage.native()
        registration = self.adapters.get(stage.identity.agent).registration
        if registration and registration.registration_status(stage) == "archived":
            operation = self._retire(operation, artifacts, "archived", record)
            return self._result(operation, artifacts, reused=reused)
        # the target's configuration and schema can still drift after prepare()
        self._configuration(artifacts, probes)
        if not checked:
            _check_stage(artifacts.conversation, self.adapters.writer(stage.identity.agent), stage)
            self._metadata(stage, artifacts.conversation.title)
        operation = self._index(operation, stage, artifacts.index_intent)
        for index in range(len(stage.files)):
            operation = self._file(operation, stage, index)
        operation = self._database(operation, stage)
        operation = self._registry(operation, artifacts)
        operation = self._save(replace(operation, state="complete", last_error=None), "complete")
        return self._result(operation, artifacts, reused=reused)

    def _retire(
        self,
        operation: Operation,
        artifacts: Artifacts,
        lifecycle: str,
        record: dict[str, Any] | None,
    ) -> Operation:
        stage = artifacts.stage.native()
        if not operation.terminal_lifecycle:
            owned = (_lineage(record) or {}).get("operationId") == operation.id if record else False
            registration = self.adapters.get(stage.identity.agent).registration
            if registration and registration.registration_status(stage) == "archived":
                owned = bool(operation.step("database") or operation.step("file:0"))
            if record is not None:
                archived_path = record.get("archivePath")
                original_path = (
                    record.get("originalPath")
                    or record.get("file")
                    or (record.get("thread") or {}).get("file")
                )
                for index, item in enumerate(stage.files):
                    step = operation.step(f"file:{index}")
                    same_path = (
                        original_path
                        and Path(original_path).resolve()
                        == (stage.roots.store / item.relative_path).resolve()
                    )
                    if (
                        same_path
                        and step
                        and (
                            step.phase == "done"
                            or (
                                archived_path
                                and self._owned_file(Path(archived_path), step, item.data)
                            )
                        )
                    ):
                        owned = True
            if not owned:
                raise PreservationError(
                    "Lifecycle target lacks durable publication ownership; inspect transaction"
                )
            if lifecycle not in ("archived", "deleted"):
                raise PreservationError("Unknown terminal conversion lifecycle")
            operation = self._save(
                replace(
                    operation,
                    terminal_lifecycle="archived" if lifecycle == "archived" else "deleted",
                ),
                "retire",
            )
        operation = self._registry(operation, artifacts, create_active=False)
        return self._save(replace(operation, state="complete", last_error=None), "complete")

    def _native_path(self, stage: NativeStage, relative: str) -> Path:
        path = stage.roots.store / relative
        if path.is_symlink() or path.parent.resolve() != path.parent:
            raise PreservationError("Native publication path changed or contains symlinks")
        return path

    def _index(
        self, operation: Operation, stage: NativeStage, intent: NativeIndexIntent | None
    ) -> Operation:
        index = self.adapters.get(stage.identity.agent).index
        if index is None and intent is None:
            return operation
        if index is None or intent is None or intent != index.index_intent(stage):
            raise PreservationError("Native index capability changed from durable intent")
        path = self._native_path(stage, intent.relative_path)
        data = path.read_bytes() if path.exists() else b""
        found = intent.data.rstrip(b"\n") in data.splitlines()
        step = operation.step("index")
        if step:
            if not found:
                raise PreservationError(
                    "Name-index intent has missing evidence; inspect transaction, never reappend"
                )
            if step.phase == "intent":
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
                fsync_directory(path.parent)
                operation = self._step(operation, Step("index", "done"))
            return operation
        if found:
            raise PreservationError("Native name-index collision before publication")
        operation = self._step(operation, Step("index", "intent"))
        self._point("before:index:action", operation)
        index.append_index(stage)
        fsync_directory(path.parent)
        self._point("after:index:action", operation)
        return self._step(operation, Step("index", "done"))

    def _publisher(self, operation: Operation, target: Path, index: int) -> Path:
        prepared = operation.step(f"prepare:{index}")
        suffix = f"-{prepared.publisher_token}" if prepared and prepared.publisher_token else ""
        return target.parent / f".ringleader-{operation.id}-{index}{suffix}.pending"

    def _prepare(self, operation: Operation, stage: NativeStage, index: int) -> Operation:
        item = stage.files[index]
        target = self._native_path(stage, item.relative_path)
        publisher = self._publisher(operation, target, index)
        name = f"prepare:{index}"
        previous = operation.step(name)
        if previous and previous.phase == "done":
            return operation
        if previous and (publisher.exists() or publisher.is_symlink()):
            if operation.step(f"file:{index}"):
                raise PreservationError(
                    "External file intent exists; private preparation cannot restart"
                )
            if (
                not previous.inode
                and not publisher.is_symlink()
                and publisher.read_bytes() == item.data
            ):
                with publisher.open("rb") as stream:
                    os.fsync(stream.fileno())
                    stat = os.fstat(stream.fileno())
                fsync_directory(publisher.parent)
                return self._step(
                    operation,
                    Step(name, "done", stat.st_dev, stat.st_ino, previous.publisher_token),
                )
            if not self._private_inode(publisher, previous):
                next_step = Step(name, "intent", publisher_token=uuid4().hex)
                operation = self._save(
                    replace(
                        operation.with_step(next_step),
                        abandoned_private=(*operation.abandoned_private, str(publisher)),
                    ),
                    f"{name}:abandon",
                )
                return self._prepare(operation, stage, index)
        if previous is None:
            operation = self._step(operation, Step(name, "intent"))
        self._point(f"before:{name}:action", operation)
        durable_directory(target.parent)
        flags = os.O_WRONLY if publisher.exists() else os.O_CREAT | os.O_EXCL | os.O_WRONLY
        descriptor = os.open(publisher, flags | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stat = os.fstat(stream.fileno())
            if stat.st_nlink != 1 or (
                flags == os.O_WRONLY
                and (
                    previous is None
                    or (stat.st_dev, stat.st_ino) != (previous.device, previous.inode)
                )
            ):
                raise PreservationError("Private publisher changed before opening")
            operation = self._save(
                operation.with_step(
                    Step(
                        name,
                        "intent",
                        stat.st_dev,
                        stat.st_ino,
                        previous.publisher_token if previous else "",
                    )
                ),
                f"{name}:owner",
            )
            stream.truncate()
            stream.write(item.data)
            stream.flush()
            os.fsync(stream.fileno())
        fsync_directory(publisher.parent)
        self._point(f"after:{name}:action", operation)
        return self._step(
            operation,
            Step(
                name, "done", stat.st_dev, stat.st_ino, previous.publisher_token if previous else ""
            ),
        )

    def _private_inode(self, path: Path, step: Step) -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        stat = path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_nlink) == (step.device, step.inode, 1)

    def _owned_file(self, path: Path, step: Step, data: bytes) -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        stat = path.stat()
        if (stat.st_dev, stat.st_ino) != (step.device, step.inode):
            return False
        with path.open("rb") as stream:
            return stream.read(len(data)) == data

    def _cleanup_publisher(
        self,
        operation: Operation,
        stage: NativeStage,
        index: int,
        *,
        abandon_unverified: bool = False,
    ) -> str | None:
        target = self._native_path(stage, stage.files[index].relative_path)
        publisher = self._publisher(operation, target, index)
        prepared = operation.step(f"prepare:{index}")
        if publisher.exists() or publisher.is_symlink():
            owned = prepared is not None and (
                self._private_inode(publisher, prepared)
                if abandon_unverified
                else self._owned_file(publisher, prepared, stage.files[index].data)
            )
            if not owned:
                if abandon_unverified:
                    return str(publisher)
                raise PreservationError("Private publisher changed; refusing cleanup")
            publisher.unlink()
            fsync_directory(publisher.parent)
        return None

    def _file(self, operation: Operation, stage: NativeStage, index: int) -> Operation:
        item = stage.files[index]
        target = self._native_path(stage, item.relative_path)
        operation = self._prepare(operation, stage, index)
        prepared = operation.step(f"prepare:{index}")
        assert prepared is not None
        publisher = self._publisher(operation, target, index)
        name = f"file:{index}"
        previous = operation.step(name)
        if previous:
            if not self._owned_file(target, prepared, item.data):
                raise PreservationError(
                    "Published target is missing, replaced or edited; inspect transaction, never recreate"
                )
            if previous.phase == "intent":
                fsync_directory(target.parent)
                operation = self._step(
                    operation, Step(name, "done", prepared.device, prepared.inode)
                )
        else:
            if not self._owned_file(publisher, prepared, item.data):
                raise PreservationError("Private publisher is missing or changed")
            operation = self._step(operation, Step(name, "intent", prepared.device, prepared.inode))
            self._point(f"before:{name}:action", operation)
            os.link(publisher, target)
            fsync_directory(target.parent)
            self._point(f"after:{name}:action", operation)
            operation = self._step(operation, Step(name, "done", prepared.device, prepared.inode))
        self._cleanup_publisher(operation, stage, index)
        return operation

    def _database(self, operation: Operation, stage: NativeStage) -> Operation:
        registration = self.adapters.get(stage.identity.agent).registration
        if registration is None:
            return operation
        previous = operation.step("database")
        if previous:
            status = registration.registration_status(stage)
            if status != "owned":
                raise PreservationError(
                    f"Database intent has {status} target; inspect transaction, never recreate"
                )
            if previous.phase == "intent":
                operation = self._step(operation, Step("database", "done"))
            return operation
        status = registration.registration_status(stage)
        owned_files = bool(stage.files) and all(
            (step := operation.step(f"file:{index}")) is not None
            and step.phase == "done"
            and self._owned_file(self._native_path(stage, item.relative_path), step, item.data)
            for index, item in enumerate(stage.files)
        )
        if status != "absent" and not (status == "owned" and owned_files):
            raise PreservationError("Native database target identity collision")
        operation = self._step(operation, Step("database", "intent"))
        self._point("before:database:action", operation)
        if status == "absent":
            registration.register_stage(stage, recover=owned_files)
        fsync_directory(stage.roots.store)
        self._point("after:database:action", operation)
        return self._step(operation, Step("database", "done"))

    def _registry(
        self, operation: Operation, artifacts: Artifacts, *, create_active: bool = True
    ) -> Operation:
        previous = operation.step("registry")
        if previous and previous.phase == "done":
            return operation
        if previous is None:
            operation = self._step(operation, Step("registry", "intent"))
        stage = artifacts.stage.native()
        lineage = {
            "operationId": operation.id,
            "kind": artifacts.kind,
            "source": {"agent": operation.source.agent, "nativeId": operation.source.native_id},
            "target": {"agent": operation.target.agent, "nativeId": operation.target.native_id},
            "snapshot": artifacts.snapshot.identity(artifacts.options),
            "branch": artifacts.snapshot.conversation.branch_id,
            "compact": artifacts.options.compact,
            "createdAt": operation.created_at,
            "profile": stage.profile.name,
        }

        def mutation(reg: dict[str, Any]) -> None:
            _lifecycle, existing = _record(reg, operation.target)
            if existing is not None:
                existing_lineage = _lineage(existing)
                if existing_lineage is not None and existing_lineage != lineage:
                    raise PreservationError("Registry lineage collision")
                while existing:
                    existing["lineage"] = lineage
                    existing.setdefault("nativeRoots", asdict(artifacts.stage.roots))
                    existing = existing.get("thread")
            elif create_active and operation.id not in reg.get("conversionLineage", {}):
                record = registry._new_thread(
                    {
                        "source": operation.target.agent,
                        "session_id": operation.target.native_id,
                        "source_ref": operation.target.native_id,
                        "file": str(stage.roots.store / stage.source_ref) if stage.files else None,
                        "cwd": str(stage.roots.cwd),
                        "mtime": operation.created_at,
                        "custom_title": artifacts.conversation.title,
                        "size_bytes": sum(len(item.data) for item in stage.files),
                        "message_count": len(artifacts.conversation.history),
                    }
                )
                record["lineage"] = lineage
                record["nativeRoots"] = asdict(artifacts.stage.roots)
                reg["threads"].append(record)
            reg.setdefault("conversionLineage", {})[operation.id] = lineage

        self._point("before:registry:action", operation)
        registry.mutate_registry(self.registry_path, mutation)
        self._point("after:registry:action", operation)
        return self._step(operation, Step("registry", "done"))

    def _result(
        self, operation: Operation, artifacts: Artifacts, *, reused: bool
    ) -> ConversionResult:
        lifecycle, _ = _record(registry.load_registry(self.registry_path), operation.target)
        stage = artifacts.stage.native()
        diagnostics = (*artifacts.compatibility.diagnostics, *artifacts.compatibility.warnings)
        if operation.abandoned_private:
            diagnostics += (
                "Unverified private artifacts retained: " + ", ".join(operation.abandoned_private),
            )
        if lifecycle not in ("archived", "deleted"):
            registration = self.adapters.get(operation.target.agent).registration
            registered = False
            if registration and (operation.step("database") or operation.terminal_lifecycle):
                status = registration.registration_status(stage)
                registered = status == "owned"
                if status != "owned":
                    lifecycle = {"absent": "missing", "archived": "archived"}.get(status, "changed")
            identified = registered and operation.state == "complete"
            for index, item in enumerate(stage.files) if lifecycle != "archived" else ():
                path = self._native_path(stage, item.relative_path)
                step = operation.step(f"file:{index}")
                if identified:
                    if not path.is_file():
                        lifecycle = "missing"
                elif step and not self._owned_file(path, step, item.data):
                    lifecycle = "changed" if path.exists() else "missing"
            if lifecycle == "missing":
                diagnostics += ("Existing conversion target is missing; it was not recreated",)
        return ConversionResult(
            operation.id,
            operation.source,
            operation.target,
            lifecycle,
            stage.source_ref,
            reused=reused,
            report=artifacts.report,
            diagnostics=diagnostics,
            normalizations=tuple(item.kind for item in artifacts.stage.normalizations or ()),
        )

    def rollback(self, operation_id: str) -> Operation:
        with self._lock():
            operation = self.store.load(operation_id)
            self._check_registry(operation)
            if operation.state == "rolled_back":
                self.store._path(operation.id, "artifacts.json").unlink(missing_ok=True)
                fsync_directory(self.store.directory(operation.id))
                return operation
            if any(not step.name.startswith("prepare:") for step in operation.steps):
                raise PreservationError(
                    "External publication intent exists; rollback refused, use forward recovery"
                )
            abandoned = list(operation.abandoned_private)
            if operation.steps:
                artifacts = self.store.artifacts(operation)
                stage = artifacts.stage.native()
                for index in range(len(stage.files)):
                    path = self._cleanup_publisher(operation, stage, index, abandon_unverified=True)
                    if path:
                        abandoned.append(path)
            diagnostic = (
                "Unverified private artifacts retained: " + ", ".join(abandoned)
                if abandoned
                else None
            )
            operation = self._save(
                replace(
                    operation,
                    state="rolled_back",
                    last_error=diagnostic,
                    abandoned_private=tuple(abandoned),
                ),
                "rollback",
            )
            self.store._path(operation.id, "artifacts.json").unlink(missing_ok=True)
            fsync_directory(self.store.directory(operation.id))
            return operation
