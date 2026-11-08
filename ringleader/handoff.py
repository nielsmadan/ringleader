from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, get_args

from . import resume, scanner
from .home import state as home_state
from .home.intents import AgentHandoffIntent, triage_handoff
from .sources.base import SessionLaunchingSource

type Which = Callable[[str], str | None]
type IsDir = Callable[[str], bool]
type HandoffCode = Literal["nothing_to_triage", "agent_not_installed"]

HANDOFF_CODES: Final[tuple[HandoffCode, ...]] = get_args(HandoffCode.__value__)


@dataclass(frozen=True)
class HandoffReady:
    source: str
    project_key: str
    cwd: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class HandoffRefused:
    source: str
    project_key: str
    code: HandoffCode
    message: str


type HandoffPlan = HandoffReady | HandoffRefused


def _launcher(source: str) -> SessionLaunchingSource:
    launcher = scanner.source_by_name(source)
    if not scanner.supports(launcher, "launches_sessions", SessionLaunchingSource):
        raise ValueError(f"{source} cannot be launched by rl")
    checked: SessionLaunchingSource = launcher
    return checked


def handoff_argv(intent: AgentHandoffIntent) -> tuple[str, ...]:
    return _launcher(intent.agent).launch_argv(intent.prompt)


def installed(source: str, which: Which) -> bool:
    return which(_launcher(source).launch_argv()[0]) is not None


def launch_problem(argv: tuple[str, ...], cwd: str, *, which: Which, is_dir: IsDir) -> str | None:
    if which(argv[0]) is None:
        return f"{argv[0]} is not installed — put it on PATH first"
    if not is_dir(cwd):
        return f"{cwd} is not a directory — nothing launched"
    return None


def dry_run_lines(cwd: str, argv: tuple[str, ...]) -> list[str]:
    return ["dry run — nothing launched", f"  directory: {cwd}", f"  argv: {shlex.join(argv)}"]


def _project(
    queue: tuple[home_state.ProjectRef, ...], project: str
) -> home_state.ProjectRef | None:
    by_key = next((ref for ref in queue if ref.key == project), None)
    labelled = [ref for ref in queue if ref.label == project]
    return by_key or (labelled[0] if len(labelled) == 1 else None)


def plan_triage(  # noqa: PLR0913 -- every input is injected so the plan stays pure
    reg: dict[str, Any],
    now_iso: str,
    *,
    project: str,
    source: str,
    which: Which,
    is_dir: IsDir,
    home: Path,
) -> HandoffPlan:
    queue = home_state.project_queue(home_state.visible_threads(reg), now_iso)
    ref = _project(queue, project)
    if ref is None:
        message = f"no sessions of {project} are waiting for triage"
        return HandoffRefused(source, project, "nothing_to_triage", message)
    if not installed(source, which):
        message = f"{resume.agent_name(source)} is not installed — put it on PATH first"
        return HandoffRefused(source, ref.key, "agent_not_installed", message)
    intent = triage_handoff(source, ref, reg["threads"], home, is_dir=is_dir)
    return HandoffReady(source, ref.key, intent.cwd, handoff_argv(intent))
