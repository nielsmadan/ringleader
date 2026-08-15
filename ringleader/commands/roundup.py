from __future__ import annotations

import argparse
from datetime import UTC, datetime

from .. import prune, refresh, registry
from . import default_registry, enrich


def _now() -> str:
    return datetime.now(UTC).isoformat()


def cmd_roundup(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl roundup")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--yes", action="store_true", help="reserved for future confirmation prompts")
    args = ap.parse_args(argv)

    now = _now()

    refresh.auto_refresh(args.registry, now)

    reg = registry.load_registry(args.registry)
    cands = prune.prune_candidates(reg, now)
    deleted = prune.delete_sessions(reg, cands, now)
    registry.save_registry(args.registry, reg)

    enrich.cmd_enrich(["--registry", args.registry])

    reg = registry.load_registry(args.registry)
    reg["lastRoundupAt"] = now
    registry.save_registry(args.registry, reg)

    print(f"refreshed, pruned {deleted}, enriched (see above)")
    return 0
