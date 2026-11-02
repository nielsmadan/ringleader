from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Callable
from typing import Protocol

from .. import scanner
from ..launch import Runner
from ..picker.app import Choice
from ..resume import Scope
from ..sources.base import SessionLaunchingSource
from .intents import AgentHandoffIntent, ExitIntent, ResumeIntent

type Which = Callable[[str], str | None]


class ResumeCommand(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        picker: Callable[[str, str, Scope, str, str | None], Choice | None],
        runner: Runner,
        which: Which,
    ) -> int: ...


def _fail(message: str) -> int:
    print(f"rl-inbox: {message}", file=sys.stderr)
    return 1


def handoff_argv(intent: AgentHandoffIntent) -> tuple[str, ...]:
    source = scanner.source_by_name(intent.agent)
    if not scanner.supports(source, "launches_sessions", SessionLaunchingSource):
        raise ValueError(f"{intent.agent} cannot be launched by rl")
    argv: tuple[str, ...] = source.launch_argv(intent.prompt)
    return argv


def run_intent(
    intent: ExitIntent,
    registry_path: str,
    *,
    dry_run: bool,
    resume_command: ResumeCommand,
    runner: Runner,
    which: Which,
) -> int:
    if isinstance(intent, ResumeIntent):
        choice = intent.choice
        return resume_command(
            ["--registry", registry_path, *(["--dry-run"] if dry_run else [])],
            picker=lambda *_args: choice,
            runner=runner,
            which=which,
        )
    try:
        argv = handoff_argv(intent)
    except ValueError as error:
        return _fail(str(error))
    if which(argv[0]) is None:
        return _fail(f"{argv[0]} is not installed — put it on PATH first")
    if not os.path.isdir(intent.cwd):
        return _fail(f"{intent.cwd} is not a directory — nothing launched")
    if dry_run:
        print("dry run — nothing launched")
        print(f"  directory: {intent.cwd}")
        print(f"  argv: {shlex.join(argv)}")
        return 0
    sys.stdout.flush()
    sys.stderr.flush()
    return runner(argv, intent.cwd)
