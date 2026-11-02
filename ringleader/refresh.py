from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from . import registry, scanner
from .agents import source_of
from .config import load_config
from .gitinfo import GitResolver
from .sources.base import (
    RawRef,
    ScopedDiscoverySource,
    SessionExclusionSource,
    Source,
    in_workspace,
)
from .sources.claude_code import ClaudeCodeSource


def _aware(now_iso: str) -> datetime:
    moment = datetime.fromisoformat(now_iso)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def delta_note(added: int, removed: int) -> str | None:
    """One-line summary of what the last scan changed, or None if nothing did."""
    bits = []
    if added:
        bits.append(f"{added} new")
    if removed:
        bits.append(f"{removed} gone")
    if not bits:
        return None
    return "(scanning… " + ", ".join(bits) + ")"


@dataclass(frozen=True)
class Refreshed:
    reg: dict[str, Any]
    note: str | None
    unavailable_sources: frozenset[str]


def auto_refresh(  # noqa: PLR0913
    registry_path: str | Path,
    now_iso: str,
    *,
    tail: int = 30,
    claude_projects_dir: Path | None = None,
    projects_root: Path | None = None,
    git_resolver: GitResolver | None = None,
    sources: Iterable[Source] | None = None,
    name_filters: Iterable[str] | None = None,
    persist: bool = True,
    pool: scanner.SourcePool | None = None,
) -> tuple[dict[str, Any], str | None]:
    refreshed = refresh_registry(
        registry_path,
        now_iso,
        tail=tail,
        claude_projects_dir=claude_projects_dir,
        projects_root=projects_root,
        git_resolver=git_resolver,
        sources=sources,
        name_filters=name_filters,
        persist=persist,
        pool=pool,
    )
    return refreshed.reg, refreshed.note


def refresh_registry(  # noqa: PLR0913
    registry_path: str | Path,
    now_iso: str,
    *,
    tail: int = 30,
    claude_projects_dir: Path | None = None,
    projects_root: Path | None = None,
    git_resolver: GitResolver | None = None,
    sources: Iterable[Source] | None = None,
    name_filters: Iterable[str] | None = None,
    persist: bool = True,
    pool: scanner.SourcePool | None = None,
    initial: dict[str, Any] | None = None,
) -> Refreshed:
    """Scan installed sources, reconcile into the registry, persist, and report the
    membership delta. Read commands run this before rendering; an unavailable source
    retains its cached sessions. Classification and naming remain separate agentic steps.

    `persist=False` scans into memory only, for a preview that must leave no trace.
    A long-lived caller passes the same `pool` to every refresh so the sources keep
    what they learned about unchanged files between scans."""
    if projects_root is not None:
        if claude_projects_dir is not None:
            raise ValueError("pass claude_projects_dir, not both")
        claude_projects_dir = projects_root
    reg = registry.load_registry(registry_path) if initial is None else initial
    before = {registry.identity(t) for t in reg["threads"]}
    if sources is None and claude_projects_dir is not None:
        sources = [ClaudeCodeSource(tail_turns=tail, projects_dir=claude_projects_dir)]
    reg, unavailable_sources = _refresh(
        reg,
        registry_path,
        now_iso,
        sources=sources,
        git_resolver=git_resolver,
        name_filters=name_filters,
        persist=persist,
        pool=pool,
    )
    after = {registry.identity(t) for t in reg["threads"]}
    return Refreshed(reg, delta_note(len(after - before), len(before - after)), unavailable_sources)


def scoped_refresh(  # noqa: PLR0913
    registry_path: str | Path,
    now_iso: str,
    *,
    cwd: str | None,
    recursive: bool,
    git_resolver: GitResolver | None = None,
    sources: Iterable[Source] | None = None,
    name_filters: Iterable[str] | None = None,
    persist: bool = True,
) -> tuple[dict[str, Any], bool]:
    """Rescan only what can hold a session in `cwd` (anywhere, with none) changed since each
    source's last full scan, and fold it in without tombstoning the rest. A source never
    scanned in full is discovered whole, and a registry with no baseline at all gets the
    full scan instead. Returns the registry and whether that full scan ran."""
    reg = registry.load_registry(registry_path)
    baselines = {
        name: datetime.fromisoformat(at) for name, at in (reg.get("sourcesScannedAt") or {}).items()
    }
    if not baselines:
        full = auto_refresh(
            registry_path,
            now_iso,
            git_resolver=git_resolver,
            sources=sources,
            name_filters=name_filters,
            persist=persist,
        )
        return full[0], True
    known = _known_files(reg["threads"], cwd, recursive=recursive)

    excluded = _known_files(
        (row["thread"] for row in reg["deleted"] if row.get("reason") == "excluded"),
        cwd,
        recursive=recursive,
    )

    def refs_of(src: Source) -> Iterable[RawRef]:
        since = baselines.get(src.name)
        if since is None:
            return src.discover()
        recovered = (
            frozenset(
                file
                for file in excluded.get(src.name, frozenset())
                if Path(file).is_file() and not src.excludes_session(file)
            )
            if isinstance(src, SessionExclusionSource)
            else frozenset()
        )
        return scanner.scoped_refs(
            src,
            cwd,
            recursive=recursive,
            since=datetime.min.replace(tzinfo=UTC) if recovered else since,
            known=known.get(src.name, frozenset()) | recovered,
        )

    scoped, _ = _refresh(
        reg,
        registry_path,
        now_iso,
        sources=sources,
        git_resolver=git_resolver,
        name_filters=name_filters,
        persist=persist,
        refs_of=refs_of,
        exclusion_files=frozenset(file for files in known.values() for file in files),
    )
    return scoped, False


def workspace_refresh(  # noqa: PLR0913
    registry_path: str | Path,
    now_iso: str,
    cwd: str,
    recursive: bool,
    *,
    git_resolver: GitResolver | None = None,
    sources: Iterable[Source] | None = None,
    name_filters: Iterable[str] | None = None,
    persist: bool = True,
    pool: scanner.SourcePool | None = None,
    initial: dict[str, Any] | None = None,
) -> Refreshed:
    reg = registry.load_registry(registry_path) if initial is None else initial
    before = {registry.identity(t) for t in reg["threads"]}
    known = _known_files(reg["threads"], cwd, recursive=recursive)

    def refs_of(src: Source) -> Iterable[RawRef]:
        if not scanner.supports(src, "discovers_scoped", ScopedDiscoverySource):
            return ()
        return cast(ScopedDiscoverySource, src).discover_scoped(
            cwd,
            recursive=recursive,
            since=datetime.min.replace(tzinfo=UTC),
            known=known.get(src.name, frozenset()),
        )

    reg, unavailable = _refresh(
        reg,
        registry_path,
        now_iso,
        sources=sources,
        git_resolver=git_resolver,
        name_filters=name_filters,
        persist=persist,
        refs_of=refs_of,
        pool=pool,
        exclusion_files=frozenset(file for files in known.values() for file in files),
    )
    after = {registry.identity(t) for t in reg["threads"]}
    return Refreshed(reg, delta_note(len(after - before), len(before - after)), unavailable)


def _known_files(
    threads: Iterable[dict[str, Any]], cwd: str | None, *, recursive: bool
) -> dict[str, frozenset[str]]:
    by_source: dict[str, set[str]] = {}
    for t in threads:
        if t.get("file") and in_workspace(t.get("project"), cwd, recursive=recursive):
            by_source.setdefault(source_of(t), set()).add(t["file"])
    return {name: frozenset(files) for name, files in by_source.items()}


def _refresh(  # noqa: PLR0913 -- one keyword per stage of the pipeline both refreshes share
    reg: dict[str, Any],
    registry_path: str | Path,
    now_iso: str,
    *,
    sources: Iterable[Source] | None,
    git_resolver: GitResolver | None,
    name_filters: Iterable[str] | None,
    persist: bool,
    refs_of: Callable[[Source], Iterable[RawRef]] | None = None,
    exclusion_files: frozenset[str] | None = None,
    pool: scanner.SourcePool | None = None,
) -> tuple[dict[str, Any], frozenset[str]]:
    """Scan, reconcile, filter and persist. `refs_of` narrows each source's discovery,
    which makes the scan partial: what it skipped is kept, and cached sessions keep their
    git identity rather than costing a git call per cwd."""
    partial = refs_of is not None
    unavailable_sources: set[str] = set()
    discover = pool.active if pool is not None else scanner.active_sources
    active = (
        list(sources)
        if sources is not None
        else discover(unavailable_sources=unavailable_sources, recorded_threads=reg["threads"])
    )
    scanned = scanner.scan(
        sources=active,
        git_resolver=git_resolver,
        cached_threads=reg["threads"],
        unavailable_sources=unavailable_sources,
        refs_of=refs_of,
        resolve_cached_git=not partial,
        now=_aware(now_iso),
    )
    reg = registry.reconcile(
        reg,
        scanned,
        now_iso,
        unavailable_sources=unavailable_sources,
        partial=partial,
        scanned_sources=[src.name for src in active],
        excluded_sessions=frozenset(
            registry.identity(thread)
            for src in active
            if src.name not in unavailable_sources and isinstance(src, SessionExclusionSource)
            for thread in reg["threads"]
            if source_of(thread) == src.name
            and thread.get("file")
            and (exclusion_files is None or thread["file"] in exclusion_files)
            and src.excludes_session(thread["file"])
        ),
    )
    registry.apply_name_filters(
        reg, load_config().session_filters if name_filters is None else name_filters
    )
    if persist:
        registry.commit_registry(registry_path, reg)
    return reg, frozenset(unavailable_sources)
