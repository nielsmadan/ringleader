from __future__ import annotations

from . import registry, scanner
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


def auto_refresh(
    registry_path, now_iso, *, tail=30, projects_root=None, git_resolver=None, sources=None
):
    """Scan installed sources, reconcile into the registry, persist, and report the
    membership delta. This is the cheap, pure-code refresh the read commands run so
    their counts are never stale; classification/naming stays a separate agentic step."""
    reg = registry.load_registry(registry_path)
    before = {t["sessionId"] for t in reg["threads"]}
    if sources is None:
        if projects_root is not None:
            sources = [ClaudeCodeSource(tail_turns=tail, projects_dir=projects_root)]
        else:
            sources = scanner.active_sources()
    scanned = scanner.scan(sources=sources, git_resolver=git_resolver)
    reg = registry.reconcile(reg, scanned, now_iso)
    registry.save_registry(registry_path, reg)
    after = {t["sessionId"] for t in reg["threads"]}
    return reg, delta_note(len(after - before), len(before - after))
