from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from ..paths import conversion_operations_dir, default_registry
from ..portability.diagnostics import compatibility_rows, unfinished_rows
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
    args = ap.parse_args(argv)
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

    worker = service(args)
    print("\n--- conversion compatibility ---")
    for row in compatibility_rows(worker, Path.cwd()):
        detail = row["profile"] or "unsupported"
        diagnostics = "; ".join(row["diagnostics"])
        print(f"{row['agent']}: {detail}" + (f"; {diagnostics}" if diagnostics else ""))
    print("\n--- unfinished conversion transactions ---")
    operations = unfinished_rows(worker)
    for operation in operations:
        print(
            f"{operation['id']}: {operation['state']}; {operation['error'] or 'resume or private-only rollback'}"
        )
    if not operations:
        print("None.")
    return 0
