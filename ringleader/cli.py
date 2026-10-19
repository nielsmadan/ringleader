from __future__ import annotations

import os
import sys
from typing import Any, TextIO

from . import commands, registry, session_env

TABLE = commands.TABLE

_FLAT = (
    "overview",
    "triage",
    "prune",
    "roundup",
    "usage",
    "analyze",
    "doctor",
    "harvests",
    "config",
    "resume",
    "inbox",
    "install",
    "update",
    "uninstall",
)
_GROUPS = ("session", "mem", "data", "workspace")
_RESERVED: set[str] = set()


def _leaf_names(node: object, group: str = "") -> str:
    if not isinstance(node, dict):
        return ""
    return "  ".join(f"{k} (coming soon)" if f"{group} {k}" in _RESERVED else k for k in node)


def _is_tty(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _opens_inbox() -> bool:
    return (
        _is_tty(sys.stdin)
        and _is_tty(sys.stdout)
        and os.environ.get("TERM") != "dumb"
        and session_env.current_session() is None
    )


def _usage(stream: TextIO | None = None) -> None:
    stream = sys.stdout if stream is None else stream
    print("ringleader — usage: rl <command> [args]\n", file=stream)
    print("bare `rl` in a terminal opens the inbox\n", file=stream)
    print("commands: " + "  ".join(_FLAT), file=stream)
    for g in _GROUPS:
        node = TABLE.get(g)
        subs = _leaf_names(node, g) if isinstance(node, dict) else "(coming soon)"
        print(f"  {g}: {subs}", file=stream)


def _group_usage(crumbs: list[str], node: dict[str, Any], stream: TextIO) -> None:
    group = " ".join(crumbs)
    print(f"rl {group} — subcommands: " + _leaf_names(node, group), file=stream)


def _quiet_stdout() -> None:
    try:
        fd = sys.stdout.fileno()
    except (OSError, ValueError):
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, fd)
    finally:
        os.close(devnull)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv and _opens_inbox():
        argv = ["inbox"]
    if not argv or argv[0] in ("-h", "--help", "help"):
        _usage()
        return 0
    node: dict[str, Any] = TABLE
    crumbs: list[str] = []
    rest = argv
    while rest:
        tok, rest = rest[0], rest[1:]
        entry = node.get(tok)
        if entry is None:
            where = f" in 'rl {' '.join(crumbs)}'" if crumbs else ""
            print(f"rl: unknown command '{tok}'{where}", file=sys.stderr)
            _usage(sys.stderr)
            return 2
        crumbs.append(tok)
        if isinstance(entry, dict):
            node = entry
            continue
        if not callable(entry):
            raise TypeError("invalid command table")
        try:
            return entry(rest) or 0
        except (registry.RegistryConflict, registry.AmbiguousSession) as error:
            print(f"rl: {error}", file=sys.stderr)
            return 1
        except BrokenPipeError:
            _quiet_stdout()
            return 1
    _group_usage(crumbs, node, sys.stdout)
    return 0
