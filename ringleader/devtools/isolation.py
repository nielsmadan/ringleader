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
    env = {
        var: str(root / f"{prefix}{renamed.get(store, store)}")
        for store, variables in AGENT_STORE_VARS.items()
        for var in variables
    }
    codex = Path(env["CODEX_SESSIONS_DIR"])
    return {
        **env,
        "CODEX_HOME": str(codex),
        "CODEX_SESSIONS_DIR": str(codex / "sessions"),
        "CODEX_SQLITE_HOME": str(codex / "sqlite"),
        "CLAUDE_CONFIG_DIR": str(root / f"{prefix}claude-config"),
        "RINGLEADER_MEMORY_FIXTURE_ROOT": str(root.resolve()),
    }
