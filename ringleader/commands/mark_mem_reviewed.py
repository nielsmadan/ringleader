from __future__ import annotations

import argparse

from .. import registry
from .mem_output import MemoryParser


def _mark(parser: MemoryParser, args: argparse.Namespace) -> int:
    reg = registry.load_registry(args.registry)
    try:
        registry.mark_memory_reviewed(reg, args.path)
    except KeyError:
        return parser.fail(f"No memory entry at {args.path}")
    registry.commit_registry(args.registry, reg)
    parser.emit({"path": args.path, "reviewed": True}, f"reviewed {args.path}")
    return 0


def cmd_mark_mem_reviewed(argv: list[str]) -> int:
    parser = MemoryParser("mark reviewed", argv, "Acknowledge a legacy Claude memory file by path.")
    parser.add_argument("path")
    args = parser.parse_args(argv)
    return parser.run(lambda: _mark(parser, args))
