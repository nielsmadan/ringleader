from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from .. import resume, scanner, worktrees
from ..agents import canonical_agent, display_agent
from ..launch import Runner, exec_agent
from ..sources.base import SessionLaunchingSource


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rl start",
        description="Start a fresh conversation, optionally in a new worktree.",
    )
    sources = resume.launchable_agents()
    agents = sorted({*sources, *(display_agent(a) for a in sources)})
    parser.add_argument("agent", choices=agents, help="agent to launch in the current directory")
    parser.add_argument(
        "--worktree",
        action="store_true",
        help="start in a new worktree (persistent Git checkout for Pi)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="preview without creating or launching anything"
    )
    return parser


def _preview(
    argv: tuple[str, ...], cwd: Path, value: worktrees.WorktreePlan | None, absent: bool
) -> None:
    print("dry run — no worktree or session created")
    if value and value.source.name == "opencode":
        print(f"  source directory: {cwd}")
        print("  launch directory: returned by OpenCode worktree.create")
    else:
        print(f"  launch directory: {value.destination if value and value.destination else cwd}")
    print(f"  argv: {shlex.join(argv)}")
    if value:
        print(f"  owner: {value.owner}")
        if value.source.name == "opencode":
            print(
                "  API: location.get → worktree.create → launch fresh session in returned directory"
            )
        elif value.destination:
            print(f"  Git: create {value.branch} at {value.commit}; local changes are not copied")
        else:
            print("  naming, starting revision, setup and cleanup follow the harness settings")
    if absent:
        print(f"  note: {argv[0]} is not installed")


def cmd_start(
    argv: list[str],
    *,
    runner: Runner | None = None,
    which: Callable[[str], str | None] | None = None,
) -> int:
    parser = _parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    value: worktrees.WorktreePlan | None = None
    prepared: worktrees.PreparedWorktree | None = None
    try:
        source = scanner.source_by_name(canonical_agent(args.agent))
        if not scanner.supports(source, "launches_sessions", SessionLaunchingSource):
            raise worktrees.WorktreeError(f"{args.agent} cannot launch sessions")
        command = source.launch_argv()
        cwd = Path(os.getcwd())
        absent = (which or shutil.which)(command[0]) is None
        if absent and not args.dry_run:
            raise worktrees.WorktreeError(f"{command[0]} is not installed — put it on PATH first")
        value = (
            worktrees.plan(source, None, cwd, None, name=f"rl-{uuid4().hex[:12]}")
            if args.worktree
            else None
        )
        if args.dry_run:
            _preview(value.argv if value else command, cwd, value, absent)
            return 0
        if value:
            print(f"Preparing worktree with {value.owner}…", file=sys.stderr, flush=True)
            prepared = worktrees.prepare(value)
            command, cwd = prepared.argv, prepared.cwd
            print(f"Worktree owner: {value.owner}", file=sys.stderr, flush=True)
            if value.destination:
                print(
                    f"Persistent checkout: {cwd}; local changes were not copied",
                    file=sys.stderr,
                    flush=True,
                )
        try:
            return (runner or exec_agent)(command, str(cwd))
        except OSError as exc:
            retained = "; any created worktree is retained" if value else ""
            raise worktrees.WorktreeError(f"Launch failed in {cwd}: {exc}{retained}") from exc
    except (OSError, worktrees.WorktreeError) as exc:
        print(f"rl-start: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt as exc:
        message = str(exc) or "cancelled"
        if not str(exc) and value is not None:
            destination = value.destination
            if prepared is not None and value.source.name == "opencode":
                destination = prepared.cwd
            message += (
                f"; any checkout at {destination} is retained"
                if destination
                else "; any created worktree is retained"
            )
        print(f"rl-start: {message}", file=sys.stderr)
        return 130
