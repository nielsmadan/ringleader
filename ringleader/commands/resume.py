from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

from .. import export, paths, refresh, registry, resume, scanner
from ..presence import PresenceClient
from ..resume import Scope, SessionAction
from ..sources.base import SessionLaunchingSource
from ..stateio import atomic_write_text
from . import default_registry

if TYPE_CHECKING:
    from ..picker.app import Choice
    from ..portability.capabilities import RunRequest
    from ..portability.model import Identity
    from ..portability.native_resume import NativeResume

Runner = Callable[[Sequence[str], str], int]
Picker = Callable[[str, str, Scope, str, "str | None"], "Choice | None"]
Refresher = Callable[[str, str], tuple[dict[str, Any], str | None]]
ScopedRefresher = Callable[[str, str, str, Scope], tuple[dict[str, Any], bool]]
Which = Callable[[str], str | None]
Presence = Callable[[], frozenset[tuple[str, str]]]
NativeResumer = Callable[..., "NativeResume"]
Launch = Callable[["RunRequest"], None]

_MIN_PREFIX = 4

_WIDEN = {
    Scope.CWD: " — --recursive for this tree, --all for every session",
    Scope.RECURSIVE: " — --all for every session",
    Scope.ALL: "",
}


class _Abort(Exception):
    pass


@dataclass(frozen=True)
class _Pick:
    thread: dict[str, Any]
    threads: Sequence[dict[str, Any]]
    target: str | None
    confirmed: bool = False
    action: SessionAction = "resume"


@dataclass(frozen=True)
class _Plan:
    thread: dict[str, Any]
    argv: tuple[str, ...]
    cwd: str
    transcript: Path | None
    confirmed: bool
    target: str
    action: SessionAction = "resume"


def _launchable(source_name: str) -> SessionLaunchingSource | None:
    src = scanner.source_by_name(source_name)
    return src if scanner.supports(src, "launches_sessions", SessionLaunchingSource) else None


def _agents() -> tuple[str, ...]:
    return tuple(resume.agent_name(name) for name in resume.launchable_agents())


def _fail(message: str) -> int:
    print(f"rl-resume: {message}", file=sys.stderr)
    return 1


def _live_session_ids() -> frozenset[tuple[str, str]]:
    return PresenceClient().require_running_session_ids()


def exec_agent(argv: Sequence[str], cwd: str) -> int:
    # an agent resolves a session id against the directory it starts in
    os.chdir(cwd)
    os.execvpe(argv[0], list(argv), os.environ)


def _native_resume(*args: Any, **kwargs: Any) -> NativeResume:
    from ..portability.native_resume import native_resume  # noqa: PLC0415 -- keeps startup fast

    return native_resume(*args, **kwargs)


def _exec_request(request: RunRequest) -> None:
    from ..portability.launch import ExecLauncher  # noqa: PLC0415 -- keeps startup fast

    ExecLauncher().launch(request)


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _run_picker(
    registry_path: str, cwd: str, scope: Scope, query: str, target: str | None
) -> Choice | None:
    try:
        import textual  # noqa: F401, PLC0415 -- core dep; imported lazily to keep startup fast
    except ImportError as error:
        raise _Abort(
            "needs Textual — reinstall ringleader (`uv tool install --force ringleader`)"
        ) from error

    from ..picker.app import PickerApp  # noqa: PLC0415 -- only importable once textual is present

    return PickerApp(registry_path, cwd, scope=scope, query=query, target=target).run()


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="rl resume",
        description="Re-enter a dormant session with its own agent, or hand it off to "
        "another one. An exact session id, or an unambiguous id prefix of at least four "
        "characters, resumes straight away; any other query opens the picker with that "
        "filter already typed. --last takes the most recent session in scope without "
        "asking, across every agent.",
    )
    ap.add_argument("query", nargs="?", default="", help="session id, id prefix, or picker filter")
    ap.add_argument(
        "--last", action="store_true", help="resume the most recent session in scope, no picker"
    )
    ap.add_argument(
        "--to",
        metavar="AGENT",
        help="continue in this agent instead of its own: converts the session natively, "
        "and asks before falling back to a Markdown handoff",
    )
    ap.add_argument(
        "--markdown",
        action="store_true",
        help="with --to, hand off a Markdown transcript without trying native conversion",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print the directory and argv, launch nothing"
    )
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="start the picker over every session")
    scope.add_argument(
        "--recursive", action="store_true", help="start the picker over this directory tree"
    )
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument(
        "--operations",
        default=str(paths.conversion_operations_dir()),
        help="conversion journal directory",
    )
    ap.add_argument(
        "--verified",
        default=str(paths.verified_targets_path()),
        help="self-verification state file",
    )
    return ap


def _scope(args: argparse.Namespace) -> Scope:
    if args.all:
        return Scope.ALL
    return Scope.RECURSIVE if args.recursive else Scope.CWD


def _requested_target(value: str | None) -> str | None:
    if value is None:
        return None
    agents = _agents()
    if value not in agents:
        raise _Abort(f"unknown agent '{value}' — pick one of {', '.join(agents)}")
    return resume.source_name(value)


def _eligible_threads(threads: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t for t in threads if resume.eligible(t)]


def _eligible(reg: dict[str, Any]) -> list[dict[str, Any]]:
    return _eligible_threads(reg.get("threads") or [])


def _by_id(records: Sequence[dict[str, Any]], query: str) -> dict[str, Any] | None:
    exact = [r for r in records if r.get("sessionId") == query]
    if exact:
        return exact[0] if len(exact) == 1 else None
    if len(query.removeprefix("ses_")) < _MIN_PREFIX:
        return None
    prefixed = [r for r in records if str(r.get("sessionId") or "").startswith(query)]
    return prefixed[0] if len(prefixed) == 1 else None


def _where(scope: Scope, cwd: str) -> str:
    if scope is Scope.ALL:
        return "anywhere on this machine"
    return f"under {cwd}" if scope is Scope.RECURSIVE else f"in {cwd}"


def _matches(reg: dict[str, Any], query: str, scope: Scope, cwd: str) -> bool:
    return bool(resume.rank(resume.candidates(reg, scope=scope, cwd=cwd), query))


def _check_archived(reg: dict[str, Any], query: str) -> None:
    record = _by_id(reg.get("archived") or [], query)
    if record is None:
        return
    sid = record["sessionId"]
    raise _Abort(f"{sid} is archived — `rl session unarchive {sid}` to bring it back")


def _hit(reg: dict[str, Any], query: str) -> dict[str, Any] | None:
    direct = _by_id(_eligible(reg), query)
    if direct is None:
        _check_archived(reg, query)
    return direct


def _for_query(
    args: argparse.Namespace,
    reg: dict[str, Any],
    scope: Scope,
    cwd: str,
    refresher: Refresher,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The thread a query names outright, and the registry that answer came from. A query
    nothing in the registry answers at all earns one scan before it is refused."""
    direct = _hit(reg, args.query)
    if direct is not None or _matches(reg, args.query, scope, cwd):
        return direct, reg
    reg, _ = refresher(args.registry, datetime.now(UTC).isoformat())
    direct = _hit(reg, args.query)
    if direct is not None or _matches(reg, args.query, scope, cwd):
        return direct, reg
    raise _Abort(
        f"no session matches '{args.query}' {_where(scope, cwd)}, "
        f"even after a fresh scan{_WIDEN[scope]}"
    )


def _for_last(
    args: argparse.Namespace,
    scope: Scope,
    cwd: str,
    scoper: ScopedRefresher,
    refresher: Refresher,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The most recent session in scope, and the registry it came from. The session just
    left is both the likeliest answer and the one a registry scanned earlier lacks, so the
    scope is always rescanned; one still empty earns a full scan, unless the rescan was
    one, before it is refused — never a picker, since --last asked not to be asked."""
    now_iso = datetime.now(UTC).isoformat()
    started = monotonic()
    _progress(f"Refreshing sessions {_where(scope, cwd)}…")
    current, complete = scoper(args.registry, now_iso, cwd, scope)
    rows = resume.candidates(current, scope=scope, cwd=cwd)
    if not rows and not complete:
        _progress("No session found in scope; scanning all session stores…")
        current = refresher(args.registry, now_iso)[0]
        rows = resume.candidates(current, scope=scope, cwd=cwd)
    if not rows:
        raise _Abort(f"no sessions {_where(scope, cwd)}, even after a fresh scan{_WIDEN[scope]}")
    _progress(f"Refresh finished in {monotonic() - started:.1f}s.")
    return rows[0], current


def _choose(
    args: argparse.Namespace,
    reg: dict[str, Any],
    cwd: str,
    picker: Picker,
    refreshers: tuple[ScopedRefresher, Refresher],
    target: str | None,
) -> _Pick | None:
    scope = _scope(args)
    scoper, refresher = refreshers
    if args.last:
        thread, reg = _for_last(args, scope, cwd, scoper, refresher)
        return _Pick(thread, _eligible(reg), None)
    if args.query:
        direct, reg = _for_query(args, reg, scope, cwd, refresher)
        if direct is not None:
            return _Pick(direct, _eligible(reg), None)
    choice = picker(args.registry, cwd, scope, args.query, target)
    if choice is None:
        return None
    threads = list(choice.threads) or (registry.load_registry(args.registry).get("threads") or [])
    return _Pick(
        choice.thread,
        _eligible_threads(threads),
        choice.target,
        confirmed=choice.confirmed,
        action=choice.action,
    )


def _launch_dir(thread: dict[str, Any], threads: Sequence[dict[str, Any]]) -> str:
    cwd = resume.launch_cwd(thread, threads)
    if not cwd:
        raise _Abort(f"{thread['sessionId']} has no recorded directory to start in")
    if not Path(cwd).is_dir():
        raise _Abort(f"{cwd} no longer exists — `rl workspace retarget` if it moved")
    return cwd


def _render(thread: dict[str, Any]) -> str:
    try:
        text = export.render_conversation(thread)
    except (OSError, RuntimeError) as error:
        raise _Abort(f"cannot read the conversation of {thread['sessionId']}: {error}") from error
    if not text.strip():
        raise _Abort(f"{thread['sessionId']} renders an empty transcript — nothing to hand off")
    return text


def _handoff_path(session_id: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return paths.handoffs_dir() / f"{Path(session_id).name}-{stamp}.md"


def _prompt(thread: dict[str, Any], path: Path) -> str:
    origin = resume.agent_name(resume.source_of(thread))
    return (
        f"Read {path} — a Markdown transcript of a {origin} session "
        f"({resume.label(thread)}) handed over to you — then carry on from where it stops."
    )


def _plan(pick: _Pick, target_source: str) -> _Plan:
    thread = pick.thread
    session_id = str(thread["sessionId"])
    source = resume.source_of(thread)
    src = _launchable(target_source)
    if src is None:
        raise _Abort(
            f"{resume.agent_name(source)} sessions cannot be relaunched by rl"
            " — try --to <agent> to hand it off"
        )
    cwd = _launch_dir(thread, pick.threads)
    if target_source == source:
        argv = src.fork_argv(session_id) if pick.action == "fork" else src.resume_argv(session_id)
        return _Plan(thread, argv, cwd, None, pick.confirmed, target_source, pick.action)
    if pick.action == "fork":
        raise _Abort("native fork requires the session's original agent")
    path = _handoff_path(session_id)
    argv = tuple(src.launch_argv(_prompt(thread, path)))
    return _Plan(thread, argv, cwd, path, pick.confirmed, target_source)


def _live(source: str, session_id: str | None, probe: Presence) -> bool:
    if not session_id:
        return False
    try:
        live = probe()
    except Exception:  # noqa: BLE001 -- a probe that fails any way means "not running"
        return False
    return (resume.agent_name(source), session_id) in live


def _running(thread: dict[str, Any], probe: Presence) -> bool:
    return _live(resume.source_of(thread), thread.get("sessionId"), probe)


def _ask(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ")
    except (EOFError, OSError):
        return False
    return answer.strip().lower() in ("y", "yes")


def _permitted(plan: _Plan, probe: Presence) -> bool:
    """Concurrent writers to the same transcript require confirmation."""
    if (
        plan.action == "fork"
        or plan.transcript is not None
        or plan.confirmed
        or not _running(plan.thread, probe)
    ):
        return True
    if _ask(f"{resume.label(plan.thread)} is running right now — resume it anyway?"):
        return True
    print("not resumed", file=sys.stderr)
    return False


def _report(plan: _Plan, *, absent: str | None) -> None:
    print("dry run — nothing launched")
    print(f"  directory: {plan.cwd}")
    print(f"  argv: {shlex.join(plan.argv)}")
    if plan.transcript is not None:
        print(f"  would write: {plan.transcript}")
    if absent is not None:
        print(f"  note: {absent} is not installed — put it on PATH before resuming")


def _native_line(outcome: NativeResume, agent: str) -> str:
    if outcome.outcome == "would-verify":
        return f"native: would self-verify {agent} {outcome.version}, then convert"
    if outcome.outcome == "forked":
        return f"native: would fork {agent} (previous conversion {outcome.previous})"
    if outcome.outcome == "reused" and outcome.target is not None:
        return f"native: would reopen {agent}:{outcome.target.native_id} (reused)"
    if outcome.outcome == "refused":
        return f"native: refused — {outcome.reason}; fallback would ask"
    return f"native: would convert to {agent} (new)"


def _report_native(plan: _Plan, outcome: NativeResume, *, absent: str | None) -> None:
    print("dry run — nothing launched")
    directory = outcome.launch.cwd if outcome.launch is not None else plan.cwd
    print(f"  directory: {directory}")
    print(f"  {_native_line(outcome, resume.agent_name(plan.target))}")
    if outcome.launch is not None:
        print(f"  argv: {shlex.join(outcome.launch.argv)}")
    if outcome.outcome == "refused":
        print(f"  fallback argv: {shlex.join(plan.argv)}")
        print(f"  fallback would write: {plan.transcript}")
    if absent is not None:
        print(f"  note: {absent} is not installed — put it on PATH before resuming")


def _carry_out(plan: _Plan, text: str | None, runner: Runner) -> int:
    if plan.transcript is not None and text is not None:
        try:
            paths.ensure_handoffs_dir()
            atomic_write_text(plan.transcript, text)
        except OSError as error:
            return _fail(f"cannot write {plan.transcript}: {error}")
    return runner(plan.argv, plan.cwd)


def _fallback(plan: _Plan, runner: Runner) -> int:
    if not (_interactive() and _ask("Hand off a Markdown transcript instead?")):
        print("not resumed", file=sys.stderr)
        return 1
    try:
        text = _render(plan.thread)
    except _Abort as error:
        return _fail(str(error))
    return _carry_out(plan, text, runner)


@dataclass(frozen=True)
class _Seams:
    runner: Runner
    presence: Presence
    native: NativeResumer
    launch: Launch


def _progress(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _resume_command(args: argparse.Namespace, target: Identity) -> str:
    return shlex.join(
        (
            "rl",
            "resume",
            f"{target.agent}:{target.native_id}",
            "--registry",
            args.registry,
            "--operations",
            args.operations,
            "--verified",
            args.verified,
        )
    )


def _native(args: argparse.Namespace, plan: _Plan, absent: str | None, seams: _Seams) -> int:
    from ..portability.model import Identity, PreservationError  # noqa: PLC0415
    from ..portability.native_resume import ConversionFailure  # noqa: PLC0415
    from ..portability.selection import SelectedSession  # noqa: PLC0415

    agent = resume.agent_name(plan.target)
    if (
        not args.dry_run
        and _running(plan.thread, seams.presence)
        and not _ask(
            f"{resume.label(plan.thread)} is running right now — convert its current state anyway?"
        )
    ):
        print("not resumed", file=sys.stderr)
        return 0
    selected = SelectedSession(Identity(*registry.identity(plan.thread)), "active", plan.thread)
    try:
        outcome = seams.native(
            selected,
            plan.target,
            registry_path=Path(args.registry),
            operations_path=Path(args.operations),
            verified_path=Path(args.verified),
            dry_run=args.dry_run,
            allow_verify=_interactive(),
            report=_progress,
        )
    except ConversionFailure as error:
        return _fail(str(error))
    if args.dry_run:
        _report_native(plan, outcome, absent=absent)
        return 0
    if outcome.outcome == "refused":
        print(f"native conversion to {agent} refused: {outcome.reason}", file=sys.stderr)
        return _fallback(plan, seams.runner)
    if outcome.launch is None or outcome.target is None:
        return _fail(f"native conversion to {agent} produced nothing to launch")
    target = outcome.target
    if (
        outcome.outcome == "reused"
        and _live(target.agent, target.native_id, seams.presence)
        and not _ask(f"{agent}:{target.native_id} is running right now — resume it anyway?")
    ):
        print("not resumed", file=sys.stderr)
        return 0
    for warning in outcome.warnings:
        print(f"rl-resume: {warning}", file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        seams.launch(outcome.launch)
    except (OSError, PreservationError) as error:
        return _fail(
            f"{error}; target retained: {resume.agent_name(target.agent)}:{target.native_id}. "
            f"Resume with: {_resume_command(args, target)}"
        )
    return 0


def _resolve(
    args: argparse.Namespace, picker: Picker, refreshers: tuple[ScopedRefresher, Refresher]
) -> _Plan | None:
    requested = _requested_target(args.to)
    if args.last:
        _progress("Loading session registry…")
    reg = registry.load_registry(args.registry)
    if args.last and not reg.get("sourcesScannedAt"):
        _progress("No scan baseline yet; refresh will scan all session stores.")
    cwd = os.getcwd()
    pick = _choose(args, reg, cwd, picker, refreshers, requested)
    if pick is None:
        return None
    return _plan(pick, requested or pick.target or resume.source_of(pick.thread))


def cmd_resume(  # noqa: PLR0913 -- injection points the suite drives
    argv: list[str],
    *,
    runner: Runner | None = None,
    picker: Picker | None = None,
    which: Which | None = None,
    refresher: Refresher | None = None,
    scoper: ScopedRefresher | None = None,
    presence: Presence | None = None,
    native: NativeResumer | None = None,
    launch: Launch | None = None,
) -> int:
    args = _parser().parse_args(argv)
    if args.last and args.query:
        return _fail(f"--last takes the most recent session, so it cannot also take '{args.query}'")
    started = monotonic()

    def _scan(registry_path: str, now_iso: str) -> tuple[dict[str, Any], str | None]:
        return refresh.auto_refresh(registry_path, now_iso, persist=not args.dry_run)

    def _scan_scope(
        registry_path: str, now_iso: str, cwd: str, scope: Scope
    ) -> tuple[dict[str, Any], bool]:
        root, recursive = resume.bounds(scope, cwd)
        return refresh.scoped_refresh(
            registry_path, now_iso, cwd=root, recursive=recursive, persist=not args.dry_run
        )

    try:
        plan = _resolve(args, picker or _run_picker, (scoper or _scan_scope, refresher or _scan))
        if plan is None:
            return 0
        binary = plan.argv[0]
        absent = (which or shutil.which)(binary) is None
        if absent and not args.dry_run:
            raise _Abort(f"{binary} is not installed — put it on PATH first")
        converting = plan.transcript is not None and not args.markdown
        text = _render(plan.thread) if plan.transcript is not None and not converting else None
    except _Abort as error:
        return _fail(str(error))
    seams = _Seams(
        runner or exec_agent,
        presence or _live_session_ids,
        native or _native_resume,
        launch or _exec_request,
    )
    if converting:
        return _native(args, plan, binary if absent else None, seams)
    if args.dry_run:
        _report(plan, absent=binary if absent else None)
        return 0

    def _probe() -> frozenset[tuple[str, str]]:
        if args.last:
            _progress("Checking whether the session is already running…")
        return seams.presence()

    if not _permitted(plan, _probe):
        return 0
    if args.last:
        _progress(
            f"Launching {shlex.join(plan.argv)} (ringleader took {monotonic() - started:.1f}s)…"
        )
    return _carry_out(plan, text, seams.runner)
