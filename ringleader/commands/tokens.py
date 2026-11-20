from __future__ import annotations

from .usage import local_summary


def cmd_tokens(argv: list[str]) -> int:
    return local_summary(["--source", "claude", *argv], prog="rl data tokens")
