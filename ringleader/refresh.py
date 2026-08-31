from __future__ import annotations

from . import registry, scanner
from .config import load_config
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
    registry_path,
    now_iso,
    *,
    tail=30,
    claude_projects_dir=None,
    projects_root=None,
    git_resolver=None,
    sources=None,
    name_filters=None,
):
    """Scan installed sources, reconcile into the registry, persist, and report the
    membership delta. Read commands run this before rendering; an unavailable source
    retains its cached sessions. Classification and naming remain separate agentic steps."""
    if projects_root is not None:
        if claude_projects_dir is not None:
            raise ValueError("pass claude_projects_dir, not both")
        claude_projects_dir = projects_root
    reg = registry.load_registry(registry_path)
    before = {t["sessionId"] for t in reg["threads"]}
    unavailable_sources: set[str] = set()
    if sources is None:
        if claude_projects_dir is not None:
            sources = [ClaudeCodeSource(tail_turns=tail, projects_dir=claude_projects_dir)]
        else:
            sources = scanner.active_sources(unavailable_sources=unavailable_sources)
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
    registry.save_registry(registry_path, reg)
    after = {t["sessionId"] for t in reg["threads"]}
    return reg, delta_note(len(after - before), len(before - after))
