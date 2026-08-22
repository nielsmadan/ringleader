from __future__ import annotations

from .usage import cmd_usage


def cmd_tokens(argv: list[str]) -> int:
    return cmd_usage(["--source", "claude", *argv], prog="rl data tokens")
