from __future__ import annotations

import argparse
import json
import shlex
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import registry
from ..paths import conversion_operations_dir, default_registry
from ..portability.adapters import builtin_adapters
from ..portability.capabilities import NativeRoots, SourceRequest
from ..portability.launch import ExecLauncher, Launcher, ProbeRunner
from ..portability.model import Identity, PreservationError
from ..portability.operations import StoredRoots
from ..portability.selection import (
    SelectedSession,
    open_sessions,
    resolve,
    running_selectors,
    sessions,
)
from ..portability.service import ConversionRequest, ConversionResult, ConversionService
from ..presence import PresenceClient


def service(args: argparse.Namespace) -> ConversionService:
    return ConversionService(
        builtin_adapters(),
        ProbeRunner(),
        registry_path=Path(args.registry),
        operations_path=Path(args.operations),
    )


def parser(command: str, description: str) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog=f"rl {command}", description=description)
    result.add_argument("--registry", default=str(default_registry()))
    result.add_argument(
        "--operations",
        default=str(conversion_operations_dir()),
        help="conversion journal directory",
    )
    result.add_argument("--json", action="store_true")
    return result


def _error(error: Exception, *, json_output: bool = False) -> int:
    if json_output:
        print(json.dumps({"error": str(error)}))
    else:
        print(f"rl: {error}", file=sys.stderr)
    return 1


def _roots(
    worker: ConversionService, selected: SelectedSession, *, launch: bool = False
) -> NativeRoots:
    stored = selected.thread.get("nativeRoots")
    if stored:
        roots = replace(StoredRoots(**stored).native(), cwd=selected.cwd)
    elif launch:
        roots = worker.adapters.roots(selected.identity.agent, selected.cwd)
    else:
        resolver = worker.adapters.get(selected.identity.agent).roots
        if resolver is None:
            raise PreservationError("Native source location is unavailable")
        roots, _ = resolver.resolve_source(selected.cwd, selected.source_ref)
    if launch and not Path(_source_ref(worker, selected, roots)).is_relative_to(roots.store):
        raise PreservationError(
            "Recorded session file is outside the native launch store; check native root overrides"
        )
    return roots


def _source_ref(worker: ConversionService, selected: SelectedSession, roots: NativeRoots) -> str:
    resolver = worker.adapters.get(selected.identity.agent).roots
    if resolver is None:
        raise PreservationError("Native source location is unavailable")
    if not selected.thread.get("nativeRoots"):
        return resolver.resolve_source(selected.cwd, selected.source_ref)[1]
    return resolver.source_ref(roots, selected.source_ref)


def _source(worker: ConversionService, selected: SelectedSession) -> SourceRequest:
    roots = _roots(worker, selected)
    return SourceRequest(selected.identity, roots, _source_ref(worker, selected, roots))


def _render_result(result: ConversionResult) -> str:
    target = f"{result.target.agent}:{result.target.native_id}"
    verb = "preview" if result.dry_run else "reused" if result.reused else "created"
    line = (
        f"{result.source.agent}:{result.source.native_id} → {target}: {verb} ({result.lifecycle})"
    )
    if result.operation_id:
        line += f"; transaction {result.operation_id}"
    if result.report:
        line += f"; compact {asdict(result.report)}"
    if result.diagnostics:
        line += "; " + "; ".join(result.diagnostics)
    return line


def _select(
    args: argparse.Namespace, worker: ConversionService
) -> tuple[list[SelectedSession], list[dict[str, Any]]]:
    all_sessions = sessions(registry.load_registry(args.registry))
    agent = worker.adapters.get(args.from_agent).agent if args.from_agent else None
    candidates = tuple(
        item for item in all_sessions if agent is None or item.identity.agent == agent
    )
    if args.open:
        candidates = open_sessions(candidates, datetime.now(UTC).isoformat())
        selectors = tuple(f"{item.identity.agent}:{item.identity.native_id}" for item in candidates)
    else:
        selectors = running_selectors(PresenceClient()) if args.running else args.ids
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else None
    selected: list[SelectedSession] = []
    errors: list[dict[str, Any]] = []
    for selector in selectors:
        if args.running and agent and not selector.startswith(agent + ":"):
            continue
        try:
            item = (
                resolve(all_sessions, selector) if args.running else resolve(candidates, selector)
            )
            if args.running and item.identity.native_id != selector.split(":", 1)[1]:
                raise PreservationError(
                    f"Running session {selector} not found by exact identity; run rl session refresh"
                )
            if item not in candidates or (
                workspace is not None and not item.cwd.is_relative_to(workspace)
            ):
                continue
            if item not in selected:
                selected.append(item)
        except (ValueError, OSError) as exc:
            errors.append({"selector": selector, "error": str(exc)})
    return selected, errors


def cmd_convert(argv: list[str]) -> int:
    ap = parser(
        "session convert",
        "Convert native history, preserving the source. Reads the registry without scanning.",
    )
    ap.add_argument(
        "ids", nargs="*", help="exact native IDs or unambiguous prefixes; AGENT:ID qualifies"
    )
    ap.add_argument("--to", required=True, help="claude, claude-code, codex, opencode or pi")
    selection = ap.add_mutually_exclusive_group()
    selection.add_argument(
        "--open",
        action="store_true",
        help="effective active/unfinished/blocked; hide snoozed/name-filtered; include kept open sessions",
    )
    selection.add_argument(
        "--running",
        action="store_true",
        help="verified live processes, regardless of lifecycle/snooze/name filters",
    )
    ap.add_argument("--from", dest="from_agent", help="optional source agent filter")
    ap.add_argument(
        "--workspace",
        help="filter sources at or below this directory; preserves each source workspace",
    )
    ap.add_argument(
        "--compact", action="store_true", help="deterministic tool omission; no model call"
    )
    ap.add_argument("--dry-run", action="store_true", help="validate without persistent writes")
    args = ap.parse_args(argv)
    if bool(args.ids) == bool(args.open or args.running):
        ap.error("provide IDs or exactly one of --open / --running")
    try:
        worker = service(args)
        target = worker.adapters.get(args.to).agent
        selected, errors = _select(args, worker)
    except (ValueError, OSError) as exc:
        return _error(exc, json_output=args.json)
    results = []
    for item in selected:
        try:
            request = ConversionRequest(
                _source(worker, item),
                target,
                worker.adapters.roots(target, item.cwd),
                compact=args.compact,
            )
            results.append(worker.convert(request, dry_run=args.dry_run))
        except (OSError, ValueError, RuntimeError, TypeError, LookupError) as exc:
            errors.append(
                {"selector": f"{item.identity.agent}:{item.identity.native_id}", "error": str(exc)}
            )
    if args.json:
        print(
            json.dumps(
                {"results": [asdict(result) for result in results], "errors": errors}, indent=2
            )
        )
    else:
        for result in results:
            print(_render_result(result))
        for error in errors:
            print(f"{error['selector']}: {error['error']}", file=sys.stderr)
        if not results and not errors:
            print("No sessions selected.")
    return int(bool(errors))


def _resume_command(identity: Identity, args: argparse.Namespace) -> str:
    return shlex.join(
        (
            "rl",
            "session",
            "resume",
            f"{identity.agent}:{identity.native_id}",
            "--registry",
            args.registry,
            "--operations",
            args.operations,
        )
    )


def _launch(worker: ConversionService, selected: SelectedSession, launcher: Launcher) -> None:
    if selected.lifecycle != "active":
        raise PreservationError(f"Session is {selected.lifecycle}; cannot resume")
    roots = _roots(worker, selected, launch=True)
    source_ref = _source_ref(worker, selected, roots)
    if not Path(source_ref).exists():
        raise PreservationError(f"Native session file is missing: {source_ref}")
    request = worker.adapters.launcher(selected.identity.agent).launch_request(
        selected.identity, roots
    )
    launcher.validate(request)
    sys.stdout.flush()
    sys.stderr.flush()
    launcher.launch(request)


def _fork(argv: list[str], *, handoff: bool) -> int:
    command = "handoff" if handoff else "fork"
    ap = parser(
        f"session {command}",
        "Create a fresh native session, preserving source history and workspace.",
    )
    ap.add_argument("id")
    ap.add_argument("--to", required=handoff, help="destination agent (fork defaults to source)")
    ap.add_argument("--compact", action="store_true")
    if not handoff:
        ap.add_argument(
            "--resume", action="store_true", help="replace this process with the new native session"
        )
    args = ap.parse_args(argv)
    result = None
    try:
        worker = service(args)
        selected = resolve(sessions(registry.load_registry(args.registry)), args.id)
        target = worker.adapters.get(args.to or selected.identity.agent).agent
        roots = worker.adapters.roots(target, selected.cwd)
        launcher = ExecLauncher()
        launch = handoff or args.resume
        if launch:
            allocator = worker.adapters.get(target).identity_allocator
            probe_id = (
                allocator.allocate_identity() if allocator else Identity(target, str(uuid4()))
            )
            launcher.validate(worker.adapters.launcher(target).launch_request(probe_id, roots))
        result = worker.convert(
            ConversionRequest(
                _source(worker, selected), target, roots, compact=args.compact, fork=True
            )
        )
        print(
            json.dumps(asdict(result), indent=2) if args.json else _render_result(result),
            flush=True,
        )
        if launch:
            created = resolve(
                sessions(registry.load_registry(args.registry)),
                f"{target}:{result.target.native_id}",
            )
            _launch(worker, created, launcher)
        return 0
    except (OSError, ValueError, RuntimeError, TypeError, LookupError) as exc:
        if result is not None:
            print(
                f"Target retained: {result.target.agent}:{result.target.native_id}. Resume with: {_resume_command(result.target, args)}",
                file=sys.stderr,
            )
        return _error(exc, json_output=args.json and result is None)


def cmd_fork(argv: list[str]) -> int:
    return _fork(argv, handoff=False)


def cmd_handoff(argv: list[str]) -> int:
    return _fork(argv, handoff=True)


def cmd_resume(argv: list[str]) -> int:
    ap = parser(
        "session resume", "Replace this process with the native CLI in the recorded workspace."
    )
    ap.add_argument("id")
    args = ap.parse_args(argv)
    try:
        worker = service(args)
        selected = resolve(sessions(registry.load_registry(args.registry)), args.id)
        _launch(worker, selected, ExecLauncher())
        return 0
    except (OSError, ValueError, RuntimeError, TypeError, LookupError) as exc:
        return _error(exc, json_output=args.json)


def cmd_lineage(argv: list[str]) -> int:
    ap = parser(
        "session lineage",
        "Read authoritative conversion/fork relationships, including archived and deleted sessions.",
    )
    ap.add_argument("id")
    args = ap.parse_args(argv)
    try:
        worker = service(args)
        reg = registry.load_registry(args.registry)
        candidates = list(sessions(reg))
        known = {item.identity for item in candidates}
        for relation in reg.get("conversionLineage", {}).values():
            for value in (relation["source"], relation["target"]):
                identity = Identity(value["agent"], value["nativeId"])
                if identity not in known:
                    candidates.append(SelectedSession(identity, "missing", {}))
                    known.add(identity)
        selected = resolve(tuple(candidates), args.id)
        records = worker.lineage(selected.identity)
        if args.json:
            print(json.dumps(records, indent=2))
        else:
            for record in records:
                source, target = record["source"], record["target"]
                print(
                    f"{source['agent']}:{source['nativeId']} → {target['agent']}:{target['nativeId']}: {record['kind']}; transaction {record['operationId']}"
                )
            if not records:
                print("No conversion lineage recorded.")
        return 0
    except (OSError, ValueError, RuntimeError, TypeError, LookupError) as exc:
        return _error(exc, json_output=args.json)
