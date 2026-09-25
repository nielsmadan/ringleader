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
from typing import TYPE_CHECKING, Any

from .. import export, paths, refresh, registry, resume, scanner
from ..presence import PresenceClient
from ..resume import Scope
from ..sources.base import SessionLaunchingSource
from ..stateio import atomic_write_text
from . import default_registry

if TYPE_CHECKING:
    from ..picker.app import Choice

Runner = Callable[[Sequence[str], str], int]
Picker = Callable[[str, str, Scope, str, "str | None"], "Choice | None"]
Refresher = Callable[[str, str], tuple[dict[str, Any], str | None]]
Which = Callable[[str], str | None]
Presence = Callable[[], frozenset[tuple[str, str]]]

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


@dataclass(frozen=True)
class _Plan:
    thread: dict[str, Any]
    argv: tuple[str, ...]
    cwd: str
    transcript: Path | None
    confirmed: bool


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


def _exec(argv: Sequence[str], cwd: str) -> int:
    # an agent resolves a session id against the directory it starts in
    os.chdir(cwd)
    os.execvpe(argv[0], list(argv), os.environ)


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
    ap.add_argument("--to", metavar="AGENT", help="resume with this agent instead of its own")
    ap.add_argument(
        "--dry-run", action="store_true", help="print the directory and argv, launch nothing"
    )
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="start the picker over every session")
    scope.add_argument(
        "--recursive", action="store_true", help="start the picker over this directory tree"
    )
    ap.add_argument("--registry", default=str(default_registry()))
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
    reg: dict[str, Any],
    scope: Scope,
    cwd: str,
    refresher: Refresher,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The most recent session in scope, and the registry it came from. Like a query, an
    empty scope earns one scan before it is refused — never a picker, since --last asked
    not to be asked."""
    for attempt in (reg, None):
        current = (
            attempt
            if attempt is not None
            else refresher(args.registry, datetime.now(UTC).isoformat())[0]
        )
        rows = resume.candidates(current, scope=scope, cwd=cwd)
        if rows:
            return rows[0], current
    raise _Abort(f"no sessions {_where(scope, cwd)}, even after a fresh scan{_WIDEN[scope]}")


def _choose(
    args: argparse.Namespace,
    reg: dict[str, Any],
    cwd: str,
    picker: Picker,
    refresher: Refresher,
    target: str | None,
) -> _Pick | None:
    scope = _scope(args)
    if args.last:
        thread, reg = _for_last(args, reg, scope, cwd, refresher)
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
        return _Plan(thread, tuple(src.resume_argv(session_id)), cwd, None, pick.confirmed)
    path = _handoff_path(session_id)
    argv = tuple(src.launch_argv(_prompt(thread, path)))
    return _Plan(thread, argv, cwd, path, pick.confirmed)


def _running(thread: dict[str, Any], probe: Presence) -> bool:
    session_id = thread.get("sessionId")
    if not session_id:
        return False
    try:
        live = probe()
    except Exception:  # noqa: BLE001 -- a probe that fails any way means "not running"
        return False
    return (resume.agent_name(resume.source_of(thread)), session_id) in live


def _ask(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ")
    except (EOFError, OSError):
        return False
    return answer.strip().lower() in ("y", "yes")


def _permitted(plan: _Plan, probe: Presence) -> bool:
    """Two agents appending to one transcript is not recoverable, so resuming a session
    presence reports as live asks first. A handoff starts a new session and never does."""
    if plan.transcript is not None or plan.confirmed or not _running(plan.thread, probe):
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


def _resolve(args: argparse.Namespace, picker: Picker, refresher: Refresher) -> _Plan | None:
    requested = _requested_target(args.to)
    reg = registry.load_registry(args.registry)
    cwd = os.getcwd()
    pick = _choose(args, reg, cwd, picker, refresher, requested)
    if pick is None:
        return None
    return _plan(pick, requested or pick.target or resume.source_of(pick.thread))


def cmd_resume(
    argv: list[str],
    *,
    runner: Runner | None = None,
    picker: Picker | None = None,
    which: Which | None = None,
    refresher: Refresher | None = None,
    presence: Presence | None = None,
) -> int:
    args = _parser().parse_args(argv)
    if args.last and args.query:
        return _fail(f"--last takes the most recent session, so it cannot also take '{args.query}'")
    try:
        plan = _resolve(args, picker or _run_picker, refresher or refresh.auto_refresh)
        if plan is None:
            return 0
        binary = plan.argv[0]
        absent = (which or shutil.which)(binary) is None
        if absent and not args.dry_run:
            raise _Abort(f"{binary} is not installed — put it on PATH first")
        text = _render(plan.thread) if plan.transcript is not None else None
    except _Abort as error:
        return _fail(str(error))
    if args.dry_run:
        _report(plan, absent=binary if absent else None)
        return 0
    if not _permitted(plan, presence or _live_session_ids):
        return 0
    if plan.transcript is not None and text is not None:
        try:
            paths.ensure_handoffs_dir()
            atomic_write_text(plan.transcript, text)
        except OSError as error:
            return _fail(f"cannot write {plan.transcript}: {error}")
    return (runner or _exec)(plan.argv, plan.cwd)
