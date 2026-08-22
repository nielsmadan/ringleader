#!/usr/bin/env python3
"""Build a throwaway ringleader world for testing the skills from a zero state.

`rl` takes every input it touches from the environment, so a sandbox needs no support
from the tool itself — just a directory and four exported variables. Seeds real
transcripts (copies, never the originals) so triage/prune have something lifelike to
chew on, and restamps their mtimes, since recency drives nearly every decision `rl`
makes and archived transcripts are all months old.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SANDBOX = REPO / ".sandbox"
CLAUDE_ARCHIVE = Path.home() / ".local/share/ringleader/archive"
CODEX_LIVE = Path.home() / ".codex/sessions"
DAY = 86400


def _spread_mtime(path: Path, days_ago: float) -> None:
    when = time.time() - days_ago * DAY
    os.utime(path, (when, when))


def _seed_claude(dest: Path, n: int, rng: random.Random) -> int:
    """Copy n archived transcripts, keeping their <encoded-cwd>/ parent so project
    grouping behaves like the real thing."""
    if not CLAUDE_ARCHIVE.is_dir():
        return 0
    pool = [p for p in CLAUDE_ARCHIVE.glob("*/*.jsonl") if not p.name.startswith("rollout-")]
    picked = rng.sample(pool, min(n, len(pool)))
    for i, src in enumerate(picked):
        out = dest / src.parent.name / src.name
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        # 0-30 days, oldest first, so --recent windows and the prune age gate both bite
        _spread_mtime(out, days_ago=i * (30 / max(len(picked) - 1, 1)))
    return len(picked)


def _seed_codex(dest: Path, n: int, rng: random.Random) -> int:
    """Copy n interactive Codex rollouts, keeping the YYYY/MM/DD partitioning. Subagent
    and exec rollouts are skipped the same way the real source skips them, so the
    sandbox doesn't fill up with sessions that would never be registered anyway."""
    if not CODEX_LIVE.is_dir() or n <= 0:
        return 0
    pool = []
    for p in CODEX_LIVE.glob("**/rollout-*.jsonl"):
        try:
            with p.open() as fh:
                meta = json.loads(fh.readline()).get("payload", {})
        except (OSError, ValueError):
            continue
        source = meta.get("source")
        if source is None or source == "cli":  # interactive only
            pool.append(p)
        if len(pool) >= n * 4:  # enough to sample from without walking everything
            break
    picked = rng.sample(pool, min(n, len(pool)))
    for i, src in enumerate(picked):
        out = dest / src.relative_to(CODEX_LIVE)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        _spread_mtime(out, days_ago=i * (30 / max(len(picked) - 1, 1)))
    return len(picked)


def main() -> int:
    ap = argparse.ArgumentParser(prog="just sandbox", description=__doc__)
    ap.add_argument("-n", "--claude", type=int, default=20, help="claude sessions (default 20)")
    ap.add_argument("--codex", type=int, default=8, help="codex sessions (default 8)")
    ap.add_argument("--seed", type=int, default=0, help="rng seed, for a reproducible world")
    ap.add_argument("--retention", type=int, default=9999, help="cleanupPeriodDays for rl doctor")
    args = ap.parse_args()
    rng = random.Random(args.seed)

    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    data, claude, codex = SANDBOX / "data", SANDBOX / "claude", SANDBOX / "codex"
    for d in (data, claude, codex):
        d.mkdir(parents=True)

    settings = SANDBOX / "claude-settings.json"
    settings.write_text(json.dumps({"cleanupPeriodDays": args.retention}, indent=2))

    n_claude = _seed_claude(claude, args.claude, rng)
    n_codex = _seed_codex(codex, args.codex, rng)

    print(f"sandbox: {SANDBOX}")
    print(f"  seeded {n_claude} claude + {n_codex} codex sessions, mtimes spread over 30 days")
    print("  registry: empty (zero state)\n")
    print("Export these, then run `rl ...` or launch an agent — every call lands in the")
    print("sandbox, and nothing touches your real sessions or registry:\n")
    print(f'export XDG_DATA_HOME="{data}"')
    print(f'export SESSION_TRACKER_PROJECTS_DIR="{claude}"')
    print(f'export CODEX_SESSIONS_DIR="{codex}"')
    print(f'export RINGLEADER_CLAUDE_SETTINGS="{settings}"')
    print("\nRe-run `just sandbox` to reset to zero state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
