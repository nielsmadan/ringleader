from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from .. import handoff, registry, resume
from ..agents import canonical_agent, display_agent
from ..launch import Runner, exec_agent
from . import default_registry


def _triage_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="rl handoff triage",
        description="Hand one project's triage to an agent: launch it in the project "
        "directory with the ringleader-triage skill, the same handoff as `t` in rl inbox.",
    )
    sources = resume.launchable_agents()
    ap.add_argument(
        "--project",
        required=True,
        help="project key (project_key in rl inbox --json), or a project label when only one "
        "waiting project has it",
    )
    ap.add_argument(
        "--agent",
        required=True,
        choices=sorted({*sources, *(display_agent(a) for a in sources)}),
        help="agent to launch",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print the directory and argv; launch nothing"
    )
    ap.add_argument("--json", action="store_true", help="with --dry-run, print one JSON document")
    ap.add_argument(
        "--registry",
        default=str(default_registry()),
        help="registry to plan from (default: %(default)s)",
    )
    return ap


def cmd_triage(
    argv: list[str],
    *,
    runner: Runner | None = None,
    which: handoff.Which | None = None,
) -> int:
    ap = _triage_parser()
    args = ap.parse_args(argv)
    if args.json and not args.dry_run:
        ap.error("--json needs --dry-run")

    def plan(now_iso: str) -> handoff.HandoffPlan:
        return handoff.plan_triage(
            registry.load_registry(args.registry),
            now_iso,
            project=args.project,
            source=canonical_agent(args.agent),
            which=which or shutil.which,
            is_dir=os.path.isdir,
            home=Path.home(),
        )

    if args.json:
        from .. import contract_io  # noqa: PLC0415 -- machine mode only

        return contract_io.run_json(lambda: contract_io.handoff_doc(plan))
    try:
        result = plan(datetime.now(UTC).isoformat())
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(f"rl-handoff: {error}", file=sys.stderr)
        return 1
    if isinstance(result, handoff.HandoffRefused):
        print(f"rl-handoff: {result.message}", file=sys.stderr)
        return 1
    if args.dry_run:
        print("\n".join(handoff.dry_run_lines(result.cwd, result.argv)))
        return 0
    problem = handoff.launch_problem(
        result.argv, result.cwd, which=which or shutil.which, is_dir=os.path.isdir
    )
    if problem is not None:
        print(f"rl-handoff: {problem}", file=sys.stderr)
        return 1
    return (runner or exec_agent)(result.argv, result.cwd)
