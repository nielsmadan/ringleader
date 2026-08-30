from __future__ import annotations

import sys

from . import registry
from .gitinfo import resolve_git_project
from .sources.base import (
    CachedMetadataSource,
    IndexUpdate,
    NormalizedSession,
    PickerHidingSource,
    PickerShowingSource,
    WorkspaceRebindingSource,
)
from .sources.claude_code import ClaudeCodeSource
from .sources.codex import CodexSource

SOURCE_TYPES = (ClaudeCodeSource, CodexSource)


def _new_sources():
    return [source_type() for source_type in SOURCE_TYPES]


SOURCES = _new_sources()


def active_sources() -> list:
    # Fresh instances (not SOURCES) so each call re-reads the env-based default
    # roots — SOURCES is constructed once at import time and would otherwise
    # keep whatever root was current then, which breaks under monkeypatched env.
    return [source for source in _new_sources() if source.root.exists()]


def source_by_name(name: str):
    return next((source for source in _new_sources() if source.name == name), None)


def supports(source, capability: str, protocol: type) -> bool:
    """Both halves of an opt-in capability: the string says the source means to offer
    it, the protocol says it actually can. They are declared separately, so checking
    only one lets a source with a typo'd capability be dropped in silence."""
    return bool(source) and capability in source.capabilities and isinstance(source, protocol)


def sources_with(capability: str, protocol: type) -> dict:
    """Every active source offering `capability`, keyed by name."""
    return {s.name: s for s in active_sources() if supports(s, capability, protocol)}


def hide_from_picker(thread: dict) -> IndexUpdate:
    """Clear the agent's own resume-picker entry for a thread, for sources that
    keep an index alongside the transcripts. Raises if the source has such an
    index but cannot be updated — callers must not then move or delete the
    transcript, or the picker is left pointing at a file that is gone."""
    src = source_by_name(thread.get("source") or "claude-code")
    if not supports(src, "hides_from_picker", PickerHidingSource):
        return IndexUpdate.NO_INDEX
    return src.hide_from_picker(thread["sessionId"], thread["file"])


def show_in_picker(thread: dict, original_path: str) -> IndexUpdate:
    """Restore a picker entry hidden by `hide_from_picker`. `original_path` is where
    the transcript lived when it was hidden — the index still points there — so the
    file must be back at that path before calling this."""
    src = source_by_name(thread.get("source") or "claude-code")
    if not supports(src, "shows_in_picker", PickerShowingSource):
        return IndexUpdate.NO_INDEX
    return src.show_in_picker(thread["sessionId"], original_path)


def rebind_workspace(thread: dict, file_path: str, new_cwd: str) -> IndexUpdate:
    """Point the agent's own index at a session's new workspace, for sources that
    record it separately from the transcript. `file_path` is where the index believes
    the transcript lives, which after an archive is not where it is now."""
    src = source_by_name(thread.get("source") or "claude-code")
    if not supports(src, "rebinds_workspace", WorkspaceRebindingSource):
        return IndexUpdate.NO_INDEX
    return src.rebind_workspace(thread["sessionId"], file_path, new_cwd)


def scan(sources=None, git_resolver=None, cached_threads=None) -> list[NormalizedSession]:
    if sources is None:
        sources = SOURCES
    if git_resolver is None:
        git_resolver = resolve_git_project
    threads_by_ref = {
        (thread.get("source") or "claude-code", thread.get("file")): thread
        for thread in cached_threads or []
        if thread.get("file")
    }
    cache: dict[str, object] = {}  # cwd -> GitProject|None, resolved once per cwd
    out: list[NormalizedSession] = []
    for src in sources:
        for ref in src.discover():
            thread = threads_by_ref.get((ref.source, ref.file))
            parser_version = getattr(src, "parser_version", None)
            ns = registry.cached_session(thread, ref, parser_version)
            if ns is None:
                try:
                    ns = src.parse(ref)
                except OSError as e:
                    print(f"skipped {ref.file}: {e}", file=sys.stderr)
                    continue
                ns.cache_version = parser_version
                ns.file_fingerprint = ref.fingerprint
            elif isinstance(src, CachedMetadataSource):
                src.refresh_cached_metadata(ns)
            if ns.cwd is not None:
                if ns.cwd not in cache:
                    cache[ns.cwd] = git_resolver(ns.cwd)
                gp = cache[ns.cwd]
                if gp is not None:
                    ns.git_project_key = gp.key
                    ns.git_project_label = gp.label
                    ns.git_root = gp.root
                    ns.git_remote = gp.remote
                    ns.git_clone_url = gp.clone_url
                    ns.git_resolved = True
            out.append(ns)
    return out
