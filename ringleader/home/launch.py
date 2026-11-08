from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Protocol

from ..handoff import Which, dry_run_lines, handoff_argv, launch_problem
from ..launch import Runner
from ..picker.app import Choice
from ..resume import Scope
from .intents import ExitIntent, ResumeIntent


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
    problem = launch_problem(argv, intent.cwd, which=which, is_dir=os.path.isdir)
    if problem is not None:
        return _fail(problem)
    if dry_run:
        print("\n".join(dry_run_lines(intent.cwd, argv)))
        return 0
    sys.stdout.flush()
    sys.stderr.flush()
    return runner(argv, intent.cwd)
