from __future__ import annotations

import argparse
from datetime import UTC, datetime

from .. import prune, refresh, registry
from ..config import load_config
from ..paths import archive_root
from . import default_registry, enrich, sweep


def _now() -> str:
    return datetime.now(UTC).isoformat()


def cmd_roundup(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl roundup")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--yes", action="store_true", help="reserved for future confirmation prompts")
    args = ap.parse_args(argv)

    now = _now()

    refresh.auto_refresh(args.registry, now)
    swept = sweep.cmd_sweep(["--registry", args.registry])

    reg = registry.load_registry(args.registry)
    cands = prune.prune_candidates(reg, now)
    deleted = prune.delete_sessions(reg, cands, now)
    retention = load_config().archive_retention_days
    purged = prune.purge_archived(
        reg,
        prune.archive_purge_candidates(reg, now, retention_days=retention),
        now,
        archive_root_path=str(archive_root()),
    )
    registry.save_registry(args.registry, reg)

    enrich.cmd_enrich(["--registry", args.registry])

    reg = registry.load_registry(args.registry)
    reg["lastRoundupAt"] = now
    registry.save_registry(args.registry, reg)

    print(f"refreshed, pruned {deleted}, purged {purged} archived, enriched (see above)")
    return swept
