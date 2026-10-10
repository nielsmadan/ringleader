from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .capabilities import NativeRoots
from .model import PreservationError


def _path(variable: str, default: Path) -> Path:
    return Path(os.environ.get(variable) or default).expanduser().resolve()


@dataclass(frozen=True)
class RootLayout:
    native_variable: str
    default_store: str
    scan_variable: str = ""
    session_leaf: str = ""
    database: bool = False
    names_home: bool = False

    def resolve_source(self, cwd: Path, recorded: str | None) -> tuple[NativeRoots, str]:
        if recorded is None and self.database:
            data = _path("XDG_DATA_HOME", Path.home() / ".local/share")
            recorded = str(_path("OPENCODE_DB", data / "opencode/opencode.db"))
        if recorded is None:
            raise PreservationError("Source has no recorded native file; run rl session refresh")
        return self.resolve(cwd, source_ref=recorded), recorded

    def source_ref(self, roots: NativeRoots, recorded: str | None) -> str:
        if recorded:
            return recorded
        if self.database:
            return str(roots.store / "opencode.db")
        raise PreservationError("Source has no recorded native file; run rl session refresh")

    def resolve(self, cwd: Path, *, source_ref: str | None = None) -> NativeRoots:
        home = Path.home().resolve()
        data = _path("XDG_DATA_HOME", home / ".local/share")
        store = (
            _path(self.native_variable, home) / self.default_store
            if self.names_home
            else _path(self.native_variable, home / self.default_store)
        )
        if self.database:
            database = _path("OPENCODE_DB", data / "opencode/opencode.db")
            if source_ref is not None:
                database = Path(source_ref).resolve()
            if source_ref is None and (
                database.name != "opencode.db" or database.parent.name != "opencode"
            ):
                raise PreservationError("OpenCode requires a store named opencode with opencode.db")
            store = database.parent
            if source_ref is None and store != data / "opencode":
                raise PreservationError(
                    "OPENCODE_DB disagrees with XDG_DATA_HOME/opencode/opencode.db"
                )
        elif self.scan_variable and os.environ.get(self.scan_variable):
            leaf = _path(self.scan_variable, store / self.session_leaf)
            if source_ref is None:
                if leaf.name != self.session_leaf:
                    raise PreservationError(
                        f"{self.scan_variable} must end in /{self.session_leaf} for native publication or launch"
                    )
                if os.environ.get(self.native_variable) and leaf.parent != store:
                    raise PreservationError(
                        f"{self.scan_variable} disagrees with {self.native_variable}; refusing a different native store"
                    )
            store = leaf.parent
        return NativeRoots(
            home,
            store,
            cwd.resolve(),
            _path("XDG_CONFIG_HOME", home / ".config"),
            _path("XDG_CACHE_HOME", home / ".cache"),
            data,
            _path("XDG_STATE_HOME", home / ".local/state"),
            _path("TMPDIR", Path(tempfile.gettempdir())),
        )
