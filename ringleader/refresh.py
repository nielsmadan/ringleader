from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from . import registry, scanner
from .config import load_config
from .gitinfo import GitProject
from .sources.base import Source
from .sources.claude_code import ClaudeCodeSource


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


def auto_refresh(  # noqa: PLR0913
    registry_path: str | Path,
    now_iso: str,
    *,
    tail: int = 30,
    claude_projects_dir: Path | None = None,
    projects_root: Path | None = None,
    git_resolver: Callable[[str | None], GitProject | None] | None = None,
    sources: Iterable[Source] | None = None,
    name_filters: Iterable[str] | None = None,
    persist: bool = True,
) -> tuple[dict[str, Any], str | None]:
    """Scan installed sources, reconcile into the registry, persist, and report the
    membership delta. Read commands run this before rendering; an unavailable source
    retains its cached sessions. Classification and naming remain separate agentic steps.

    `persist=False` scans into memory only, for a preview that must leave no trace."""
    if projects_root is not None:
        if claude_projects_dir is not None:
            raise ValueError("pass claude_projects_dir, not both")
        claude_projects_dir = projects_root
    reg = registry.load_registry(registry_path)
    before = {registry.identity(t) for t in reg["threads"]}
    unavailable_sources: set[str] = set()
    if sources is None:
        if claude_projects_dir is not None:
            sources = [ClaudeCodeSource(tail_turns=tail, projects_dir=claude_projects_dir)]
        else:
            sources = scanner.active_sources(
                unavailable_sources=unavailable_sources, recorded_threads=reg["threads"]
            )
    scanned = scanner.scan(
        sources=sources,
        git_resolver=git_resolver,
        cached_threads=reg["threads"],
        unavailable_sources=unavailable_sources,
    )
    reg = registry.reconcile(
        reg,
        scanned,
        now_iso,
        unavailable_sources=unavailable_sources,
    )
    if name_filters is None:
        name_filters = load_config().session_filters
    registry.apply_name_filters(reg, name_filters)
    if persist:
        registry.commit_registry(registry_path, reg)
    after = {registry.identity(t) for t in reg["threads"]}
    return reg, delta_note(len(after - before), len(before - after))
