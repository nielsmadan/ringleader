from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from .. import registry
from ..agents import display_agent
from . import native
from .adapters import builtin_adapters
from .capabilities import NeedsVerification, RunRequest
from .launch import ExecLauncher, ProbeRunner
from .model import Identity, PreservationError
from .selection import SelectedSession, native_roots, resolve, sessions, source_request
from .service import (
    RETIRED,
    ConversionRequest,
    ConversionResult,
    ConversionService,
    PreparedConversion,
)
from .verification import load_verified
from .verify.workflow import Report, verify_and_record

Outcome = Literal["converted", "reused", "forked", "refused", "would-convert", "would-verify"]
_ERRORS = (OSError, ValueError, RuntimeError, TypeError, LookupError, AttributeError)
_REFUSALS = (PreservationError, OSError)


@dataclass(frozen=True)
class NativeResume:
    outcome: Outcome
    target: Identity | None
    launch: RunRequest | None
    reason: str | None
    warnings: tuple[str, ...] = ()
    version: str | None = None
    previous: str | None = None


class ConversionFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class _Paths:
    registry: Path
    operations: Path
    verified: Path


def _service(paths: _Paths) -> ConversionService:
    return ConversionService(
        builtin_adapters(load_verified(paths.verified)),
        ProbeRunner(),
        registry_path=paths.registry,
        operations_path=paths.operations,
    )


def _refused(reason: str) -> NativeResume:
    return NativeResume("refused", None, None, reason)


def _unexpected(target: str, error: Exception) -> ConversionFailure:
    return ConversionFailure(
        f"native conversion to {display_agent(target)} failed: {type(error).__name__}: {error}; "
        "nothing was published"
    )


def _recovery(paths: _Paths, operation_id: str) -> str:
    flags = (
        "--registry",
        str(paths.registry),
        "--operations",
        str(paths.operations),
        "--verified",
        str(paths.verified),
    )
    resume = shlex.join(("rl", "data", "transaction", "resume", operation_id, *flags))
    rollback = shlex.join(("rl", "data", "transaction", "rollback", operation_id, *flags))
    return f"transaction {operation_id}; finish it with `{resume}` or undo it with `{rollback}`"


def _failure(paths: _Paths, target: str, error: Exception, operation_id: str | None) -> str:
    where = (
        _recovery(paths, operation_id)
        if operation_id
        else f"inspect `rl data transaction list --operations {shlex.quote(str(paths.operations))}`"
    )
    return f"native conversion to {display_agent(target)} failed: {error}; {where}"


def _operation_id(worker: ConversionService, key: str, before: frozenset[str]) -> str | None:
    try:
        operations = [worker.store.load(item) for item in worker.store.matching(key=key)]
    except _ERRORS:
        return None
    fresh = [item for item in operations if item.id not in before]
    chosen = sorted(fresh or operations, key=lambda item: item.created_at)
    return chosen[-1].id if chosen else None


def _installed_version(executable: str, agent: str) -> str:
    try:
        result = native.probe_version(executable, agent, ProbeRunner())
    except (OSError, subprocess.SubprocessError, KeyError):
        return "(unknown version)"
    match = re.search(r"\d+(?:\.\d+)+", result.stdout.decode(errors="replace"))
    return match.group(0) if match else "(unknown version)"


def _would_verify(worker: ConversionService, target: str) -> NativeResume:
    version = _installed_version(worker.adapters.verifier(target).executable, target)
    return NativeResume("would-verify", None, None, None, version=version)


def _request(worker: ConversionService, source: SelectedSession, target: str) -> ConversionRequest:
    return ConversionRequest(
        source_request(worker.adapters, source),
        target,
        worker.adapters.roots(target, source.cwd),
    )


def _prepare(worker: ConversionService, source: SelectedSession, target: str) -> PreparedConversion:
    try:
        return worker.prepare(_request(worker, source, target))
    except subprocess.TimeoutExpired as exc:
        raise ConversionFailure(
            f"native conversion to {display_agent(target)} failed: {exc}; nothing was published"
        ) from exc


def _resumable(
    worker: ConversionService, source: SelectedSession, target: str
) -> ConversionResult | None:
    try:
        existing = worker.reusable(_request(worker, source, target))
    except _REFUSALS:
        return None
    except _ERRORS as exc:
        raise _unexpected(target, exc) from exc
    return existing if existing is not None and existing.lifecycle not in RETIRED else None


def _resume_existing(
    worker: ConversionService,
    paths: _Paths,
    source: SelectedSession,
    existing: ConversionResult,
    *,
    dry_run: bool,
) -> NativeResume:
    target = existing.target.agent
    if dry_run:
        planned = _attempt(
            target,
            lambda: worker.adapters.launcher(target).launch_request(
                existing.target, worker.adapters.roots(target, source.cwd)
            ),
        )
        if isinstance(planned, NativeResume):
            return planned
        return NativeResume("reused", existing.target, planned, None)
    refusal = _attempt(target, lambda: _validate(worker, source, target))
    if isinstance(refusal, NativeResume):
        return refusal
    return NativeResume("reused", existing.target, _launch(worker, existing, paths), None)


def _validate(worker: ConversionService, source: SelectedSession, target: str) -> None:
    adapter = worker.adapters.get(target)
    allocator = adapter.identity_allocator
    probe = allocator.allocate_identity() if allocator else Identity(adapter.agent, str(uuid4()))
    roots = worker.adapters.roots(target, source.cwd)
    ExecLauncher().validate(worker.adapters.launcher(target).launch_request(probe, roots))


def _attempt[T](target: str, action: Callable[[], T]) -> T | NativeResume:
    try:
        return action()
    except ConversionFailure:
        raise
    except _REFUSALS as exc:
        return _refused(str(exc))
    except _ERRORS as exc:
        raise _unexpected(target, exc) from exc


def _publish(
    worker: ConversionService, prepared: PreparedConversion, paths: _Paths
) -> ConversionResult:
    before = frozenset(worker.store.ids())
    try:
        return worker.publish(prepared)
    except (*_ERRORS, subprocess.SubprocessError) as exc:
        key = prepared.artifacts.key()
        target = prepared.target.agent
        raise ConversionFailure(
            _failure(paths, target, exc, _operation_id(worker, key, before))
        ) from exc


def _launch(worker: ConversionService, result: ConversionResult, paths: _Paths) -> RunRequest:
    try:
        created = resolve(
            sessions(registry.load_registry(paths.registry)),
            f"{result.target.agent}:{result.target.native_id}",
        )
        if created.lifecycle != "active":
            raise PreservationError(f"Converted session is {created.lifecycle}; cannot resume")
        roots = native_roots(worker.adapters, created, launch=True)
        return worker.adapters.launcher(created.identity.agent).launch_request(
            created.identity, roots
        )
    except _ERRORS as exc:
        raise ConversionFailure(
            _failure(paths, result.target.agent, exc, result.operation_id)
        ) from exc


def _preview(
    worker: ConversionService, source: SelectedSession, prepared: PreparedConversion, paths: _Paths
) -> NativeResume:
    before = frozenset(worker.store.ids())
    try:
        result = worker.preview(prepared)
    except _ERRORS as exc:
        key = prepared.artifacts.key()
        target = prepared.target.agent
        raise ConversionFailure(
            _failure(paths, target, exc, _operation_id(worker, key, before))
        ) from exc
    if not result.reused:
        return NativeResume("would-convert", None, None, None, result.diagnostics)
    if result.lifecycle in RETIRED:
        return NativeResume(
            "forked", None, None, None, result.diagnostics, previous=result.lifecycle
        )
    planned = worker.adapters.launcher(result.target.agent).launch_request(
        result.target, worker.adapters.roots(result.target.agent, source.cwd)
    )
    return NativeResume("reused", result.target, planned, None, result.diagnostics)


def _prepare_or_verify(  # noqa: PLR0913
    worker: ConversionService,
    source: SelectedSession,
    target: str,
    paths: _Paths,
    *,
    dry_run: bool,
    allow_verify: bool,
    report: Report,
) -> tuple[ConversionService, PreparedConversion] | NativeResume:
    try:
        return worker, _prepare(worker, source, target)
    except NeedsVerification as exc:
        if dry_run:
            return _attempt(target, lambda: _would_verify(worker, target))
        if not allow_verify:
            return _refused(str(exc))
    except ConversionFailure:
        raise
    except _REFUSALS as exc:
        return _refused(str(exc))
    except _ERRORS as exc:
        raise _unexpected(target, exc) from exc

    def verified() -> tuple[ConversionService, PreparedConversion]:
        verify_and_record(worker.adapters, target, paths.verified, report)
        fresh = _service(paths)
        return fresh, _prepare(fresh, source, target)

    return _attempt(target, verified)


def native_resume(  # noqa: PLR0913
    source: SelectedSession,
    target_agent: str,
    *,
    registry_path: Path,
    operations_path: Path,
    verified_path: Path,
    dry_run: bool,
    allow_verify: bool,
    report: Report,
) -> NativeResume:
    paths = _Paths(registry_path, operations_path, verified_path)
    try:
        worker = _service(paths)
        target = worker.adapters.get(target_agent).agent
    except _REFUSALS as exc:
        return _refused(str(exc))
    except _ERRORS as exc:
        raise _unexpected(target_agent, exc) from exc
    existing = _resumable(worker, source, target)
    if existing is not None:
        return _resume_existing(worker, paths, source, existing, dry_run=dry_run)
    ready = _prepare_or_verify(
        worker, source, target, paths, dry_run=dry_run, allow_verify=allow_verify, report=report
    )
    if isinstance(ready, NativeResume):
        return ready
    worker, prepared = ready
    if dry_run:
        return _preview(worker, source, prepared, paths)
    refusal = _attempt(target, lambda: _validate(worker, source, target))
    if isinstance(refusal, NativeResume):
        return refusal
    result = _publish(worker, prepared, paths)
    outcome: Outcome = "reused" if result.reused else "converted"
    previous = None
    if result.reused and result.lifecycle in RETIRED:
        previous = result.lifecycle
        result, outcome = _publish(worker, worker.as_fork(prepared), paths), "forked"
    return NativeResume(
        outcome,
        result.target,
        _launch(worker, result, paths),
        None,
        result.diagnostics,
        previous=previous,
    )
