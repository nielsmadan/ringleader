from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import memory_catalog
from .config import load_config
from .gitinfo import git_identities, lookup_git_project
from .memory_sources.base import MemoryReader, NativePaths, SelectedReader
from .memory_sources.claude_code import ClaudeMemorySource, ClaudeRoots
from .memory_sources.codex import CodexMemorySource, resolve_sqlite_home
from .memory_sources.unsupported import REASONS, UnsupportedMemorySource
from .memory_types import MemoryRecord, MemoryScan
from .paths import claude_config_dir, managed_claude_root

_BINARIES = {"claude-code": "claude", "cursor": "cursor-agent"}
SOURCE_NAMES = ("claude-code", "codex", *REASONS)


def _installed(source: str) -> bool:
    return shutil.which(_BINARIES.get(source, source)) is not None


def _session_projects(reg: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    rows = [
        *reg.get("threads", ()),
        *(item.get("thread") or item for item in reg.get("archived", ())),
    ]
    return tuple(
        row
        for row in rows
        if isinstance(row.get("project"), str) and Path(row["project"]).is_absolute()
    )


def _managed_claude(paths: NativePaths) -> tuple[Path, ...]:
    root = managed_claude_root(paths.boundary)
    return (root / "managed-settings.json",) if root is not None else ()


def _config_paths(cwd: Path, projects: Iterable[Path], paths: NativePaths) -> tuple[Path, ...]:
    system = paths.boundary / "etc/codex" if paths.boundary else Path("/etc").resolve() / "codex"
    roots = {
        parent
        for project in (cwd, *projects)
        for parent in (project, *project.parents)
        if paths.boundary is None or parent.is_relative_to(paths.boundary)
    }
    return (
        system / "requirements.toml",
        system / "config.toml",
        *(p / ".codex/config.toml" for p in sorted(roots)),
    )


def new_sources(
    reg: Mapping[str, Any],
    *,
    codex_sqlite_home: Path | None = None,
    claude_trust_file: Path | None = None,
    project_roots: Iterable[Path] = (),
    trusted_claude_projects: Iterable[Path] = (),
    installed: Callable[[str], bool] = _installed,
) -> tuple[MemoryReader, ...]:
    boundary = os.environ.get("RINGLEADER_MEMORY_FIXTURE_ROOT")
    paths = NativePaths(Path(boundary) if boundary else None)
    home = Path.home() if paths.boundary is None else paths.boundary / "home"
    config = claude_config_dir(home)
    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    sessions = _session_projects(reg)
    known = set(project_roots) | {Path(row.get("gitRoot") or row["project"]) for row in sessions}
    project_dir = Path(os.environ.get("SESSION_TRACKER_PROJECTS_DIR") or config / "projects")
    roots = ClaudeRoots(
        config,
        project_dir,
        home,
        tuple(sorted(known)),
        tuple(trusted_claude_projects),
        claude_trust_file or (home / ".claude.json" if config == home / ".claude" else None),
        _managed_claude(paths),
        named_project_dir=os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME"),
        prior_stores=tuple(
            {
                Path(row["storeId"])
                for row in memory_catalog.rows_by_id(reg).values()
                if row.get("source") == "claude-code" and row.get("storeId")
            }
        ),
    )
    error = None
    sqlite_home = codex_sqlite_home
    codex_projects = {
        row["sessionId"]: row["project"] for row in sessions if row.get("source") == "codex"
    }
    if sqlite_home is None:
        try:
            sqlite_home = resolve_sqlite_home(
                codex_home,
                os.environ.get("CODEX_SQLITE_HOME"),
                configs=_config_paths(Path.cwd(), map(Path, codex_projects.values()), paths),
                paths=paths,
            )
        except (OSError, ValueError, RecursionError) as exc:
            error = f"Cannot resolve Codex SQLite home: {exc}. Specify memory.codex_sqlite_home."
    return (
        ClaudeMemorySource(roots, installed("claude-code"), paths),
        CodexMemorySource(
            codex_home, sqlite_home, codex_projects, installed("codex"), paths, error
        ),
        *(UnsupportedMemorySource(name, installed(name)) for name in REASONS),
    )


def scan(sources: Iterable[MemoryReader]) -> MemoryScan:
    scans = [source.scan() for source in sources]
    return MemoryScan(
        tuple(row for result in scans for row in result.records),
        tuple(status for result in scans for status in result.statuses),
        tuple(store for result in scans for store in result.stores),
        inventoried_sources=frozenset(
            source for result in scans for source in result.inventoried_sources
        ),
    )


def observe_catalog(
    reg: Mapping[str, Any], sources: Iterable[MemoryReader], started: str
) -> MemoryScan:
    observed = scan(sources)
    boundary = os.environ.get("RINGLEADER_MEMORY_FIXTURE_ROOT")
    paths = NativePaths(Path(boundary) if boundary else None)

    def safe_project(cwd: str) -> bool:
        try:
            paths.check(Path(cwd))
            return Path(cwd).is_dir()
        except (OSError, ValueError):
            return False

    projects = {record.project for record in observed.records if record.project}
    recorded = (*_session_projects(reg), *reg.get("memoryEntries", ()))
    identities = git_identities(
        (cwd for cwd in projects if safe_project(cwd)),
        recorded,
        lookup_git_project,
        datetime.fromisoformat(started),
    )
    return replace(observed, project_identities=identities)


def configured_sources(reg: Mapping[str, Any]) -> tuple[MemoryReader, ...]:
    cfg = load_config()
    return new_sources(
        reg,
        codex_sqlite_home=Path(cfg.memory_codex_sqlite_home)
        if cfg.memory_codex_sqlite_home
        else None,
        claude_trust_file=Path(cfg.memory_claude_trust_file)
        if cfg.memory_claude_trust_file
        else None,
    )


type SourceResolver = Callable[[Mapping[str, Any]], Iterable[MemoryReader]]


def read_record(
    reg: Mapping[str, Any], row: Mapping[str, Any], *, sources: SourceResolver | None = None
) -> MemoryRecord:
    if not row.get("fingerprint") or not row.get("nativeIncarnation"):
        raise ValueError("Run `rl mem refresh` to review this entry")
    ref = memory_catalog.native_ref(row)
    reader = next(
        (source for source in (sources or configured_sources)(reg) if source.name == ref.source),
        None,
    )
    if isinstance(reader, SelectedReader):
        return memory_catalog.bind_record(reader.read_selected(ref), row)
    raise ValueError("This native memory source is unavailable")
