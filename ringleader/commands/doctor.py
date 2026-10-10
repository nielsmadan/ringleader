from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from ..agents import display_agent
from ..paths import conversion_operations_dir, default_registry, verified_targets_path
from ..portability.adapters import builtin_adapters
from ..portability.diagnostics import compatibility_rows, unfinished_rows
from ..portability.model import PreservationError
from ..portability.verify.workflow import NotInstalled, verify_and_record
from ..presence import PresenceClient, presence_rows
from . import hooks, skills, statusline
from .conversion import service

MIN_RETENTION_DAYS = 30
GLYPHS = {"ok": "✓", "warn": "⚠", "error": "✖"}


def check_claude_retention(settings_path: str | Path) -> dict[str, Any]:
    p = Path(settings_path)
    value = None
    try:
        value = json.loads(p.read_text()).get("cleanupPeriodDays")
    except (OSError, json.JSONDecodeError):
        value = None
    ok = isinstance(value, int) and value >= MIN_RETENTION_DAYS
    advice = (
        "cleanupPeriodDays looks fine."
        if ok
        else "Set cleanupPeriodDays >= 30 in ~/.claude/settings.json so ringleader can "
        "curate sessions before Claude Code deletes them."
    )
    return {
        "harness": "claude-code",
        "setting": "cleanupPeriodDays",
        "value": value,
        "ok": ok,
        "advice": advice,
    }


def verify_conversion(selection: str, verified: Path) -> int:
    adapters = builtin_adapters()
    try:
        agents = (
            tuple(agent for agent in adapters.agents() if adapters.get(agent).verifier)
            if selection == "all"
            else (adapters.get(selection).agent,)
        )
        for agent in agents:
            adapters.verifier(agent)
    except PreservationError as exc:
        print(f"rl: {exc}")
        return 2
    failed = False
    for agent in agents:
        try:
            verify_and_record(adapters, agent, verified, _say)
        except NotInstalled as exc:
            print(f"- {display_agent(agent)}: {exc.executable} is not installed; skipped")
            if selection != "all":
                failed = True
        except PreservationError:
            failed = True
    return 1 if failed else 0


def _say(line: str) -> None:
    print(line, flush=True)


def cmd_doctor(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl doctor")
    ap.add_argument(
        "--settings",
        # flag wins, then the sandbox/skill-driven env override, then the real file
        default=os.environ.get("RINGLEADER_CLAUDE_SETTINGS")
        or str(Path.home() / ".claude" / "settings.json"),
    )
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--operations", default=str(conversion_operations_dir()))
    ap.add_argument(
        "--verify-conversion",
        nargs="?",
        const="all",
        metavar="AGENT",
        help="run the offline conversion self-verification for installed agents (or AGENT) "
        "and record each pass",
    )
    ap.add_argument(
        "--verified",
        default=str(verified_targets_path()),
        help="self-verification state file",
    )
    args = ap.parse_args(argv)
    if args.verify_conversion:
        return verify_conversion(args.verify_conversion, Path(args.verified))
    r = check_claude_retention(args.settings)
    mark = "✓" if r["ok"] else "⚠"
    print(f"{mark} {r['harness']} {r['setting']}={r['value']}")
    if not r["ok"]:
        print(f"  {r['advice']}")

    for label, run, run_argv in (
        ("skills", skills.cmd_status, []),
        ("statusline", statusline.cmd_status, ["--settings", args.settings]),
        ("hooks", hooks.cmd_status, []),
    ):
        print(f"\n--- {label} ---")
        run(run_argv)

    print("\n--- presence ---")
    for row in presence_rows(PresenceClient()):
        print(f"{GLYPHS[row['state']]} {row['label']}: {row['detail']}")

    try:
        worker = service(args)
    except PreservationError as exc:
        print(f"\nrl: {exc}")
        return 2
    print("\n--- conversion compatibility ---")
    for row in compatibility_rows(worker, Path.cwd()):
        detail = row["profile"] or (
            "needs self-verification" if row["needs_verification"] else "unsupported"
        )
        diagnostics = "; ".join(row["diagnostics"])
        print(
            f"{display_agent(row['agent'])}: {detail}" + (f"; {diagnostics}" if diagnostics else "")
        )
    print("\n--- unfinished conversion transactions ---")
    operations = unfinished_rows(worker)
    for operation in operations:
        print(
            f"{operation['id']}: {operation['state']}; {operation['error'] or 'resume or private-only rollback'}"
        )
    if not operations:
        print("None.")
    return 0
