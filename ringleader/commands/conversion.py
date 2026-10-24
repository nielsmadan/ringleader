from __future__ import annotations

import argparse
import json
import shlex
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import registry
from ..agents import display_agent
from ..paths import conversion_operations_dir, default_registry, verified_targets_path
from ..portability.adapters import builtin_adapters
from ..portability.capabilities import NeedsVerification
from ..portability.launch import ExecLauncher, Launcher, ProbeRunner
from ..portability.model import Identity, PreservationError
from ..portability.selection import (
    SelectedSession,
    native_roots,
    open_sessions,
    resolve,
    running_selectors,
    sessions,
    source_ref,
    source_request,
)
from ..portability.service import ConversionRequest, ConversionResult, ConversionService
from ..portability.verification import load_verified
from ..presence import PresenceClient


def service(args: argparse.Namespace) -> ConversionService:
    return ConversionService(
        builtin_adapters(load_verified(Path(args.verified))),
        ProbeRunner(),
        registry_path=Path(args.registry),
        operations_path=Path(args.operations),
    )


def lineage_service(args: argparse.Namespace) -> ConversionService:
    # lineage only reads the registry; it needs no self-verification state.
    return ConversionService(
        builtin_adapters(),
        ProbeRunner(),
        registry_path=Path(args.registry),
        operations_path=Path(args.operations),
    )


def parser(command: str, description: str, *, verified: bool = True) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog=f"rl {command}", description=description)
    result.add_argument("--registry", default=str(default_registry()))
    result.add_argument(
        "--operations",
        default=str(conversion_operations_dir()),
        help="conversion journal directory",
    )
    if verified:
        result.add_argument(
            "--verified",
            default=str(verified_targets_path()),
            help="self-verification state file",
        )
    result.add_argument("--json", action="store_true")
    return result


def render_error(error: Exception, *, json_output: bool = False) -> int:
    if json_output:
        print(json.dumps({"error": str(error)}))
    else:
        print(f"rl: {error}", file=sys.stderr)
    return 1


def render_result(result: ConversionResult) -> str:
    source = f"{display_agent(result.source.agent)}:{result.source.native_id}"
    target = f"{display_agent(result.target.agent)}:{result.target.native_id}"
    verb = "preview" if result.dry_run else "reused" if result.reused else "created"
    line = f"{source} → {target}: {verb} ({result.lifecycle})"
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
    ap.add_argument("--to", required=True, help="claude, claude-code, codex, opencode, pi or droid")
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
        return render_error(exc, json_output=args.json)
    results = []
    previews: list[dict[str, str]] = []
    for item in selected:
        selector = f"{item.identity.agent}:{item.identity.native_id}"
        try:
            request = ConversionRequest(
                source_request(worker.adapters, item),
                target,
                worker.adapters.roots(target, item.cwd),
                compact=args.compact,
            )
            results.append(worker.convert(request, dry_run=args.dry_run))
        except NeedsVerification as exc:
            if args.dry_run:
                previews.append({"selector": selector, "wouldSelfVerify": str(exc)})
            else:
                errors.append({"selector": selector, "error": str(exc)})
        except (OSError, ValueError, RuntimeError, TypeError, LookupError) as exc:
            errors.append({"selector": selector, "error": str(exc)})
    if args.json:
        print(
            json.dumps(
                {
                    "results": [asdict(result) for result in results],
                    "wouldSelfVerify": previews,
                    "errors": errors,
                },
                indent=2,
            )
        )
    else:
        for result in results:
            print(render_result(result))
        for preview in previews:
            print(f"{preview['selector']}: would self-verify; {preview['wouldSelfVerify']}")
        for error in errors:
            print(f"{error['selector']}: {error['error']}", file=sys.stderr)
        if not results and not errors and not previews:
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
    roots = native_roots(worker.adapters, selected, launch=True)
    path = source_ref(worker.adapters, selected, roots)
    if not Path(path).exists():
        raise PreservationError(f"Native session file is missing: {path}")
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
                source_request(worker.adapters, selected),
                target,
                roots,
                compact=args.compact,
                fork=True,
            )
        )
        print(
            json.dumps(asdict(result), indent=2) if args.json else render_result(result),
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
                f"Target retained: {display_agent(result.target.agent)}:{result.target.native_id}. Resume with: {_resume_command(result.target, args)}",
                file=sys.stderr,
            )
        return render_error(exc, json_output=args.json and result is None)


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
        return render_error(exc, json_output=args.json)


def cmd_lineage(argv: list[str]) -> int:
    ap = parser(
        "session lineage",
        "Read authoritative conversion/fork relationships, including archived and deleted sessions.",
        verified=False,
    )
    ap.add_argument("id")
    args = ap.parse_args(argv)
    try:
        worker = lineage_service(args)
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
                    f"{display_agent(source['agent'])}:{source['nativeId']} → "
                    f"{display_agent(target['agent'])}:{target['nativeId']}: "
                    f"{record['kind']}; transaction {record['operationId']}"
                )
            if not records:
                print("No conversion lineage recorded.")
        return 0
    except (OSError, ValueError, RuntimeError, TypeError, LookupError) as exc:
        return render_error(exc, json_output=args.json)
