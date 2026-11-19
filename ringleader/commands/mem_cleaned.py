from __future__ import annotations

import argparse
from datetime import UTC, datetime

from .. import projects, registry
from .mem_output import MemoryParser


def _mark(parser: MemoryParser, args: argparse.Namespace) -> int:
    reg = registry.load_registry(args.registry)
    matched = [
        row
        for row in projects.memory_project_rows(reg)
        if row.get("path") == args.selector or projects.matches(row, args.selector)
    ]
    if not matched:
        return parser.fail(f"No memory matches '{args.selector}' — nothing to track")
    keys = sorted({projects.memory_group_key(row) for row in matched})
    when = datetime.now(UTC).isoformat()
    for key in keys:
        registry.mark_memory_project_cleaned(reg, key, when)
    registry.commit_registry(args.registry, reg)
    parser.emit({"projects": keys, "cleanedAt": when}, f"marked memory cleaned: {', '.join(keys)}")
    return 0


def cmd_mem_cleaned(argv: list[str]) -> int:
    parser = MemoryParser(
        "mark cleaned", argv, "Acknowledge a project in the legacy memory catalog."
    )
    parser.add_argument("selector")
    args = parser.parse_args(argv)
    return parser.run(lambda: _mark(parser, args))
