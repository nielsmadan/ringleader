from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import registry
from . import default_registry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cmd_delete(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl delete")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    args = ap.parse_args(argv)

    reg = registry.load_registry(args.registry)
    t = next((x for x in reg["threads"] if x["sessionId"] == args.id), None)
    if t is None:
        print(f"rl-delete: unknown session {args.id}", file=sys.stderr)
        return 1

    f = Path(t["file"])
    f.unlink(missing_ok=True)
    shutil.rmtree(f.with_suffix(""), ignore_errors=True)
    registry.prune_session(reg, args.id, _now(), reason="deleted")
    registry.save_registry(args.registry, reg)
    print(f"deleted {args.id}")
    return 0
