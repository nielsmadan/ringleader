from __future__ import annotations

import sys

from . import commands

# name -> callable(argv) -> int
DISPATCH = commands.TABLE


def _usage(stream=None) -> None:
    stream = sys.stdout if stream is None else stream
    print("ringleader — usage: rl <command> [args]\n", file=stream)
    print("commands: " + "  ".join(sorted(DISPATCH)), file=stream)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        _usage()
        return 0
    sub, rest = argv[0], argv[1:]
    fn = DISPATCH.get(sub)
    if fn is None:
        print(f"rl: unknown command '{sub}'", file=sys.stderr)
        _usage(sys.stderr)
        return 2
    return fn(rest) or 0
