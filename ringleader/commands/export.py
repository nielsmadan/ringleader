from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import export, registry
from . import default_registry


def cmd_export(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl data export")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    reg = registry.load_registry(args.registry)
    t = registry.find_session(reg, args.id)
    if t is None:
        print(f"rl-export: unknown session {args.id}", file=sys.stderr)
        return 1

    try:
        text = export.render_conversation(t)
    except (OSError, RuntimeError) as error:
        print(f"rl-export: {error}", file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0
