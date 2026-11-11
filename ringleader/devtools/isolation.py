from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

AGENT_STORE_VARS: Mapping[str, tuple[str, ...]] = {
    "claude": ("SESSION_TRACKER_PROJECTS_DIR",),
    "codex": ("CODEX_SESSIONS_DIR",),
    "pi": ("PI_SESSIONS_DIR",),
    "factory": ("FACTORY_HOME_OVERRIDE",),
    "cursor": ("CURSOR_CONFIG_DIR", "CURSOR_DATA_DIR"),
}


def agent_store_env(
    root: Path, *, prefix: str = "_", names: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Every agent's own store override pointed at `root/<prefix><store>`, so nothing that
    scans or moves sessions can reach the machine's real stores. `names` renames a store
    directory for a layout that predates this mapping."""
    renamed = names or {}
    return {
        var: str(root / f"{prefix}{renamed.get(store, store)}")
        for store, variables in AGENT_STORE_VARS.items()
        for var in variables
    }
