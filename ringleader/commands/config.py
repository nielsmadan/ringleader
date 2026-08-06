from __future__ import annotations

import sys

from ..config import load_config, save_config
from ..models import active_model

_VALID_BACKENDS = ("claude", "codex", "api")
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
    print(f"backend: {cfg.backend}")
    print(f"model: {cfg.model}")
    print(f"api.provider: {cfg.api_provider}")
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
    if key == "backend":
        if value not in _VALID_BACKENDS:
            print(
                f"rl config set backend: invalid value '{value}' "
                f"(must be one of {', '.join(_VALID_BACKENDS)})",
                file=sys.stderr,
            )
            return 2
        cfg.backend = value
    elif key == "model":
        cfg.model = value
    elif key == "api.provider":
        cfg.api_provider = value
    else:
        print(f"rl config set: unknown key '{key}'", file=sys.stderr)
        return 2
    save_config(cfg)
    return 0
