#!/usr/bin/env python3
"""Build a throwaway ringleader world for testing the skills from a zero state.

`rl` takes every input it touches from the environment, so a sandbox needs no support
from the tool itself — just a directory and four exported variables.

Two steps, deliberately separate:

  build   — rebuild `.sandbox/` from the frozen fixture and reset the registry to
            empty. Run this whenever you want to start a skill run from zero. On the
            first run it freezes a fixture automatically, so this is the only command
            you normally need.
  freeze  — re-pick which real sessions the fixture holds (`--freeze`). The fixture is
            otherwise fixed for good, which is what keeps runs comparable as you keep
            archiving and pruning. Real transcripts are full of client names, home paths
            and pasted secrets, so it stays local and gitignored — never committed.

Mtimes are restamped on every build: recency drives nearly every decision `rl` makes,
and archived transcripts are all months old.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / ".fixture"
SANDBOX = REPO / ".sandbox"
CLAUDE_ARCHIVE = Path.home() / ".local/share/ringleader/archive"
CODEX_LIVE = Path.home() / ".codex/sessions"
DAY = 86400
# a transcript far above this is one pathological session, not a representative one,
# and it would dominate the fixture's size for no extra coverage
MAX_MB = 5


def _pick(pool: list[Path], n: int, rng: random.Random) -> list[Path]:
    pool = sorted(p for p in pool if p.stat().st_size <= MAX_MB * 1024 * 1024)
    return sorted(rng.sample(pool, min(n, len(pool))))


def _codex_interactive(p: Path) -> bool:
    """Real Codex rollouts are mostly subagent/exec traffic that the source discards;
    freezing those would fill the fixture with sessions that never register."""
    try:
        with p.open() as fh:
            source = json.loads(fh.readline()).get("payload", {}).get("source")
    except (OSError, ValueError):
        return False
    return source is None or source == "cli"


def freeze(n_claude: int, n_codex: int, seed: int) -> int:
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    rng = random.Random(seed)
    manifest: dict = {"frozen_at": datetime.now(UTC).isoformat(), "seed": seed, "sessions": []}

    claude_pool = [p for p in CLAUDE_ARCHIVE.glob("*/*.jsonl") if not p.name.startswith("rollout-")]
    for src in _pick(claude_pool, n_claude, rng):
        dest = FIXTURE / "claude" / src.parent.name / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        manifest["sessions"].append({"source": "claude-code", "from": str(src)})

    if CODEX_LIVE.is_dir() and n_codex:
        codex_pool = [p for p in CODEX_LIVE.glob("**/rollout-*.jsonl") if _codex_interactive(p)]
        for src in _pick(codex_pool, n_codex, rng):
            dest = FIXTURE / "codex" / src.relative_to(CODEX_LIVE)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            manifest["sessions"].append({"source": "codex", "from": str(src)})

    (FIXTURE / "manifest.json").write_text(json.dumps(manifest, indent=2))
    n = len(manifest["sessions"])
    size = sum(f.stat().st_size for f in FIXTURE.rglob("*.jsonl"))
    print(f"froze {n} real sessions into {FIXTURE}  ({size / 1024 / 1024:.1f} MB)")
    print("  this fixture is now fixed — `just sandbox` rebuilds from it every time.")
    print("  re-run `just sandbox-refreeze` only when you want a different set.")
    return 0


def build(span: int, retention: int, freeze_args: tuple[int, int, int]) -> int:
    if not FIXTURE.is_dir():
        # first run: pick the sessions and freeze them, so `just sandbox` is the only
        # command anyone needs to remember. Re-freezing is the deliberate act.
        print("no frozen fixture yet — picking one now\n")
        freeze(*freeze_args)
        print()

    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    data, claude, codex = SANDBOX / "data", SANDBOX / "claude", SANDBOX / "codex"
    for d in (data, claude, codex):
        d.mkdir(parents=True)
    for sub, dest in (("claude", claude), ("codex", codex)):
        if (FIXTURE / sub).is_dir():
            shutil.copytree(FIXTURE / sub, dest, dirs_exist_ok=True)

    files = sorted(SANDBOX.rglob("*.jsonl"))
    for i, f in enumerate(files):  # oldest first, so --recent windows and prune both bite
        when = time.time() - (i * (span / max(len(files) - 1, 1))) * DAY
        os.utime(f, (when, when))

    settings = SANDBOX / "claude-settings.json"
    settings.write_text(json.dumps({"cleanupPeriodDays": retention}, indent=2))

    print(f"sandbox: {SANDBOX}")
    print(f"  {len(files)} real sessions from the frozen fixture, mtimes spread over {span} days")
    print("  registry: empty (zero state)\n")
    print("Export these, then run `rl ...` or launch an agent — every call lands in the")
    print("sandbox, and nothing touches your real sessions, registry or settings:\n")
    print(f'export XDG_DATA_HOME="{data}"')
    print(f'export SESSION_TRACKER_PROJECTS_DIR="{claude}"')
    print(f'export CODEX_SESSIONS_DIR="{codex}"')
    print(f'export RINGLEADER_CLAUDE_SETTINGS="{settings}"')
    print("\nRe-run `just sandbox` to reset to zero state (same sessions, every time).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="sandbox", description=__doc__)
    ap.add_argument("--freeze", action="store_true", help="pick real sessions and freeze them")
    ap.add_argument("-n", "--claude", type=int, default=20, help="claude sessions to freeze")
    ap.add_argument("--codex", type=int, default=8, help="codex sessions to freeze")
    ap.add_argument("--seed", type=int, default=0, help="which sessions get picked")
    ap.add_argument("--span", type=int, default=30, help="spread mtimes over N days")
    ap.add_argument("--retention", type=int, default=9999, help="cleanupPeriodDays for rl doctor")
    args = ap.parse_args()
    if args.freeze:
        return freeze(args.claude, args.codex, args.seed)
    return build(args.span, args.retention, (args.claude, args.codex, args.seed))


if __name__ == "__main__":
    raise SystemExit(main())
