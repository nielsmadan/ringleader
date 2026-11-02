from __future__ import annotations

import importlib
import pkgutil
import sys as _sys
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from ..paths import default_registry as default_registry

# A leaf runs a command; a group is a nested dict of them. `cli.main` walks this.
CommandFn = Callable[[list[str]], int]


@dataclass(frozen=True)
class Lazy:
    module: str
    attr: str

    def resolve(self) -> CommandFn:
        fn: CommandFn = getattr(importlib.import_module(f".{self.module}", __name__), self.attr)
        return fn

    def __call__(self, argv: list[str]) -> int:
        return self.resolve()(argv)


def __getattr__(name: str) -> ModuleType:
    if name not in {module.name for module in pkgutil.iter_modules(__path__)}:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        return importlib.import_module(f".{name}", __name__)
    except ModuleNotFoundError as error:
        if error.name != f"{__name__}.{name}":
            raise
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def _reserved(label: str) -> CommandFn:
    def _stub(argv: list[str]) -> int:
        print(f"rl {label}: not yet implemented — see Spec 2", file=_sys.stderr)
        return 2

    return _stub


_SESSION = {
    "convert": Lazy("conversion", "cmd_convert"),
    "fork": Lazy("conversion", "cmd_fork"),
    "handoff": Lazy("conversion", "cmd_handoff"),
    "resume": Lazy("conversion", "cmd_resume"),
    "lineage": Lazy("conversion", "cmd_lineage"),
    "refresh": Lazy("sync", "cmd_refresh"),
    "set": Lazy("sync", "cmd_set"),
    "enrich": Lazy("enrich", "cmd_enrich"),
    "harvest": Lazy("harvest", "cmd_harvest"),
    "finish": Lazy("finish", "cmd_finish"),
    "sweep": Lazy("sweep", "cmd_sweep"),
    "archive": Lazy("archive", "cmd_archive"),
    "unarchive": Lazy("unarchive", "cmd_unarchive"),
    "delete": Lazy("delete", "cmd_delete"),
    "complete": Lazy("complete", "cmd_complete"),
    "snooze": Lazy("snooze", "cmd_snooze"),
    "wake": Lazy("wake", "cmd_wake"),
    "keep": Lazy("keep", "cmd_keep"),
    "unkeep": Lazy("keep", "cmd_unkeep"),
    "name": Lazy("name", "cmd_name"),
    "search": Lazy("search", "cmd_search"),
    "relocate": Lazy("relocate", "cmd_relocate"),
    "whoami": Lazy("whoami", "cmd_whoami"),
    "mark": {
        "harvested": Lazy("mark_harvested", "cmd_mark_harvested"),
        "reviewed": Lazy("mark_reviewed", "cmd_mark_reviewed"),
        "cleaned": Lazy("cleaned", "cmd_cleaned"),
    },
}
_MEM = {
    "refresh": Lazy("mem_scan", "cmd_mem_scan"),
    "prune": Lazy("mem_prune", "cmd_mem_prune"),
    "triage": Lazy("triage", "cmd_mem_triage"),
    "overview": Lazy("overview", "cmd_mem_overview"),
    "mark": {
        "cleaned": Lazy("mem_cleaned", "cmd_mem_cleaned"),
        "reviewed": Lazy("mark_mem_reviewed", "cmd_mark_mem_reviewed"),
    },
}
_DATA = {
    "transaction": {
        "list": Lazy("transactions", "cmd_list"),
        "resume": Lazy("transactions", "cmd_resume"),
        "rollback": Lazy("transactions", "cmd_rollback"),
    },
    "export": Lazy("export", "cmd_export"),
    "tokens": Lazy("tokens", "cmd_tokens"),
    "migrate": Lazy("migrate", "cmd_migrate"),
    "presence": {
        "install": Lazy("presence", "cmd_install"),
        "status": Lazy("presence", "cmd_status"),
        "uninstall": Lazy("presence", "cmd_uninstall"),
    },
}
_WORKSPACE = {
    "move": Lazy("move_workspace", "cmd_move"),
    "retarget": Lazy("move_workspace", "cmd_retarget"),
}

TABLE: dict[str, CommandFn | dict[str, Any]] = {
    # flat daily verbs
    "overview": Lazy("overview", "cmd_overview"),
    "triage": Lazy("triage", "cmd_triage"),
    "prune": Lazy("prune", "cmd_prune"),
    "roundup": Lazy("roundup", "cmd_roundup"),
    "usage": Lazy("usage", "cmd_usage"),
    "analyze": Lazy("analyze", "cmd_analyze"),
    "doctor": Lazy("doctor", "cmd_doctor"),
    "harvests": Lazy("harvests", "cmd_harvests"),
    "resume": Lazy("resume", "cmd_resume"),
    "start": Lazy("start", "cmd_start"),
    "inbox": Lazy("inbox", "cmd_inbox"),
    "install": Lazy("setup", "cmd_install"),
    "update": Lazy("setup", "cmd_update"),
    "uninstall": Lazy("setup", "cmd_uninstall"),
    # namespaces
    "workspace": _WORKSPACE,
    "session": _SESSION,
    "mem": _MEM,
    "data": _DATA,
    "config": Lazy("config", "cmd_config"),
}


# Leaves that parse their own sub-verb instead of nesting in TABLE. Declared by the
# module that dispatches them, so a new subcommand cannot hide from `command_paths()`.
def _leaf_subcommands() -> dict[str, tuple[str, ...]]:
    from . import (  # noqa: PLC0415 -- keeps CLI startup light
        analyze,
        config,
        harvests,
        setup,
        usage,
    )

    return {
        "config": config.SUBCOMMANDS,
        "usage": usage.SUBCOMMANDS,
        "analyze": analyze.SUBCOMMANDS,
        "harvests": harvests.SUBCOMMANDS,
        "install": setup.nouns_for("install"),
        "update": setup.nouns_for("update"),
        "uninstall": setup.nouns_for("uninstall"),
    }


def command_paths() -> set[str]:
    """Every command a user can type, space-joined. Walking TABLE alone under-reports:
    six of its leaves parse a further sub-verb themselves."""
    out: set[str] = set()
    leaf_subcommands = _leaf_subcommands()

    def walk(node: dict[str, Any], prefix: str = "") -> None:
        for key, value in node.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                walk(value, path + " ")
                continue
            out.add(path)
            out.update(f"{path} {sub}" for sub in leaf_subcommands.get(path, ()))

    walk(TABLE)
    return out
