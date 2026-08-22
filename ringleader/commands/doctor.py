from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

MIN_RETENTION_DAYS = 30


def check_claude_retention(settings_path) -> dict:
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
    args = ap.parse_args(argv)
    r = check_claude_retention(args.settings)
    mark = "✓" if r["ok"] else "⚠"
    print(f"{mark} {r['harness']} {r['setting']}={r['value']}")
    if not r["ok"]:
        print(f"  {r['advice']}")
    return 0
