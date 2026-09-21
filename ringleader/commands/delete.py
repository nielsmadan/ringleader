from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from .. import registry, scanner
from . import default_registry


def _now() -> str:
    return datetime.now(UTC).isoformat()


def cmd_delete(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session delete")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    args = ap.parse_args(argv)

    reg = registry.load_registry(args.registry)
    t = registry.find_session(reg, args.id)
    if t is None:
        print(f"rl-delete: unknown session {args.id}", file=sys.stderr)
        return 1

    try:
        f = scanner.filesystem_path(t, "delete")
        companions = scanner.companion_paths(t.get("source"), f)
        scanner.hide_from_picker(t)
    except RuntimeError as e:
        print(f"rl-delete: {e}", file=sys.stderr)
        return 1

    f.unlink(missing_ok=True)
    scanner.remove_companions(companions)
    registry.prune_session(reg, args.id, _now(), reason="deleted")
    registry.commit_registry(args.registry, reg)
    print(f"deleted {args.id}")
    return 0
