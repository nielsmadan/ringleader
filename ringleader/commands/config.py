from __future__ import annotations

import shlex
import sys
from collections.abc import Callable

from .. import namefilter, registry
from ..config import key_preset, load_config, save_config
from ..models import active_model, describe
from . import default_registry

_VALID_ENGINES = ("claude", "codex", "llm")
_SET_ARGC = 2


def cmd_config(argv: list[str]) -> int:
    if not argv:
        print(f"usage: rl config <{'|'.join(SUBCOMMANDS)}> [args]", file=sys.stderr)
        return 2
    sub, rest = argv[0], argv[1:]
    action = _ACTIONS.get(sub)
    if action is None:
        print(f"rl config: unknown subcommand '{sub}'", file=sys.stderr)
        return 2
    return action(rest)


def _show() -> int:
    cfg = load_config()
    print(f"ai: {cfg.ai}")
    print(f"model: {cfg.model}")
    print(f"ai.command_prefix: {shlex.join(cfg.ai_command_prefix) or 'none'}")
    print(f"enrich.model: {cfg.enrich_model or 'default'}")
    print(f"analyze.model: {cfg.analyze_model or 'default'}")
    print(f"llm.provider: {cfg.llm_provider}")
    print(f"archive.retention_days: {cfg.archive_retention_days}")
    print(f"session.filters: {', '.join(cfg.session_filters) or 'none'}")
    print(f"inbox.keys: {cfg.inbox_keys}")
    for task in ("enrich", "harvest", "analyze"):
        model = active_model(cfg, task)
        print(
            f"{task} model: {describe(model) if model else 'none — no model configured/available'}"
        )
    return 0


def _set(args: list[str]) -> int:
    if len(args) != _SET_ARGC:
        print("usage: rl config set <key> <value>", file=sys.stderr)
        return 2
    key, value = args
    cfg = load_config()
    if key == "ai":
        if value not in _VALID_ENGINES:
            print(
                f"rl config set ai: invalid value '{value}' "
                f"(must be one of {', '.join(_VALID_ENGINES)})",
                file=sys.stderr,
            )
            return 2
        cfg.ai = value
    elif key == "model":
        cfg.model = value
    elif key == "ai.command_prefix":
        try:
            words = shlex.split(value)
        except ValueError as e:
            print(f"rl config set ai.command_prefix: {e}", file=sys.stderr)
            return 2
        cfg.ai_command_prefix = [] if words == ["none"] else words
    elif key == "enrich.model":
        cfg.enrich_model = None if value == "default" else value
    elif key == "analyze.model":
        cfg.analyze_model = None if value == "default" else value
    elif key == "llm.provider":
        cfg.llm_provider = value
    elif key == "archive.retention_days":
        try:
            days = int(value)
        except ValueError:
            days = -1
        if days < 0:
            print(
                f"rl config set archive.retention_days: invalid value '{value}' "
                "(must be a non-negative integer; 0 = keep forever)",
                file=sys.stderr,
            )
            return 2
        cfg.archive_retention_days = days
    elif key == "inbox.keys":
        try:
            cfg.inbox_keys = key_preset(value)
        except ValueError as error:
            print(f"rl config set: {error}", file=sys.stderr)
            return 2
    else:
        print(f"rl config set: unknown key '{key}'", file=sys.stderr)
        return 2
    save_config(cfg)
    return 0


def _match_count(pattern: str) -> int | None:
    """How many live sessions the pattern hits right now, or None if unknowable —
    an anchored glob is easy to get subtly wrong, so `add` says what it caught."""
    try:
        reg = registry.load_registry(default_registry())
    except (OSError, ValueError):
        return None
    return sum(1 for t in reg["threads"] if namefilter.thread_match(t, [pattern]))


def _filter(args: list[str]) -> int:
    if not args:
        print("usage: rl config filter <add|remove|list> [pattern]", file=sys.stderr)
        return 2
    sub, rest = args[0], args[1:]
    cfg = load_config()
    if sub == "list":
        for pattern in cfg.session_filters:
            print(pattern)
        if not cfg.session_filters:
            print("no session filters")
        return 0
    if sub not in ("add", "remove"):
        print(f"rl config filter: unknown subcommand '{sub}'", file=sys.stderr)
        return 2
    if len(rest) != 1 or not rest[0]:
        print(f"usage: rl config filter {sub} <pattern>", file=sys.stderr)
        return 2
    pattern = rest[0]
    if sub == "add":
        if pattern in cfg.session_filters:
            print(f"rl config filter add: '{pattern}' is already a filter", file=sys.stderr)
            return 2
        cfg.session_filters.append(pattern)
        save_config(cfg)
        hits = _match_count(pattern)
        suffix = "" if hits is None else f" — matches {hits} session(s)"
        print(f"filtering '{pattern}'{suffix}")
        return 0
    if pattern not in cfg.session_filters:
        print(f"rl config filter remove: '{pattern}' is not a filter", file=sys.stderr)
        return 2
    cfg.session_filters.remove(pattern)
    save_config(cfg)
    print(f"unfiltered '{pattern}'")
    return 0


# dispatched from, and enumerated by `commands.command_paths()`: one declaration, so a
# subcommand cannot exist without the coverage gate seeing it
_ACTIONS: dict[str, Callable[[list[str]], int]] = {
    "show": lambda rest: _show(),
    "set": _set,
    "filter": _filter,
}
SUBCOMMANDS: tuple[str, ...] = tuple(_ACTIONS)
