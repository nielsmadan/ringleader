from __future__ import annotations

import sys

from ..config import load_config, save_config
from ..models import active_model

_VALID_ENGINES = ("claude", "codex", "llm")
_SET_ARGC = 2


def cmd_config(argv: list[str]) -> int:
    if not argv:
        print("usage: rl config <show|set> [args]", file=sys.stderr)
        return 2
    sub, rest = argv[0], argv[1:]
    if sub == "show":
        return _show()
    if sub == "set":
        return _set(rest)
    print(f"rl config: unknown subcommand '{sub}'", file=sys.stderr)
    return 2


def _show() -> int:
    cfg = load_config()
    print(f"ai: {cfg.ai}")
    print(f"model: {cfg.model}")
    print(f"llm.provider: {cfg.llm_provider}")
    print(f"archive.retention_days: {cfg.archive_retention_days}")
    model = active_model(cfg)
    if model is None:
        print("active model: none — no model configured/available")
    else:
        print(f"active model: {model.name} (available={model.available()})")
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
    else:
        print(f"rl config set: unknown key '{key}'", file=sys.stderr)
        return 2
    save_config(cfg)
    return 0
