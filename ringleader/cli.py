from __future__ import annotations

import sys

from . import commands

TABLE = commands.TABLE

_FLAT = ("overview", "triage", "prune", "roundup", "doctor", "harvests", "config")
_GROUPS = ("session", "mem", "data")
_RESERVED = set()


def _leaf_names(node, group: str = "") -> str:
    if not isinstance(node, dict):
        return ""
    return "  ".join(f"{k} (coming soon)" if f"{group} {k}" in _RESERVED else k for k in node)


def _usage(stream=None) -> None:
    stream = sys.stdout if stream is None else stream
    print("ringleader — usage: rl <command> [args]\n", file=stream)
    print("commands: " + "  ".join(_FLAT), file=stream)
    for g in _GROUPS:
        node = TABLE.get(g)
        subs = _leaf_names(node, g) if isinstance(node, dict) else "(coming soon)"
        print(f"  {g}: {subs}", file=stream)


def _group_usage(crumbs: list[str], node: dict, stream) -> None:
    group = " ".join(crumbs)
    print(f"rl {group} — subcommands: " + _leaf_names(node, group), file=stream)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        _usage()
        return 0
    node = TABLE
    crumbs: list[str] = []
    rest = argv
    while rest:
        tok, rest = rest[0], rest[1:]
        entry = node.get(tok) if isinstance(node, dict) else None
        if entry is None:
            where = f" in 'rl {' '.join(crumbs)}'" if crumbs else ""
            print(f"rl: unknown command '{tok}'{where}", file=sys.stderr)
            _usage(sys.stderr)
            return 2
        crumbs.append(tok)
        if callable(entry):
            return entry(rest) or 0
        node = entry
    _group_usage(crumbs, node, sys.stdout)
    return 0
