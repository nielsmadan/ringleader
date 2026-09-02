from __future__ import annotations

import sys
from pathlib import Path

from . import registry
from .gitinfo import GitProject, resolve_git_project
from .sources.base import (
    CachedMetadataSource,
    IndexUpdate,
    NormalizedSession,
    PickerHidingSource,
    PickerShowingSource,
    RawRef,
    SourceUnavailableError,
    WorkspaceRebindingSource,
)
from .sources.claude_code import ClaudeCodeSource
from .sources.codex import CodexSource
from .sources.opencode import OpenCodeSource
from .sources.pi import PiSource

SOURCE_TYPES = (ClaudeCodeSource, CodexSource, OpenCodeSource, PiSource)


def _new_sources():
    return [source_type() for source_type in SOURCE_TYPES]


SOURCES = _new_sources()


def active_sources(*, unavailable_sources: set[str] | None = None) -> list:
    # Fresh instances (not SOURCES) so each call re-reads the env-based default
    # roots — SOURCES is constructed once at import time and would otherwise
    # keep whatever root was current then, which breaks under monkeypatched env.
    active = []
    for source in _new_sources():
        available = getattr(source, "available", None)
        try:
            source.root.stat()
        except FileNotFoundError:
            continue
        except OSError as error:
            if unavailable_sources is not None:
                unavailable_sources.add(source.name)
            print(f"skipped {source.name} source at {source.root}: {error}", file=sys.stderr)
            continue
        try:
            present = available() if callable(available) else True
        except OSError as error:
            if unavailable_sources is not None:
                unavailable_sources.add(source.name)
            print(f"skipped {source.name} source at {source.root}: {error}", file=sys.stderr)
            continue
        if present:
            active.append(source)
            continue
        if unavailable_sources is not None:
            unavailable_sources.add(source.name)
        print(f"skipped {source.name} source at {source.root}: unavailable", file=sys.stderr)
    return active


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


def filesystem_path(thread: dict, operation: str) -> Path:
    source_name = thread.get("source") or "claude-code"
    source = source_by_name(source_name)
    file_path = thread.get("file")
    if (
        source is None
        or "filesystem_lifecycle" not in source.capabilities
        or not isinstance(file_path, str)
        or not file_path
    ):
        raise RuntimeError(
            f"cannot {operation} {source_name} session {thread.get('sessionId')}: "
            "the source has no filesystem transcript lifecycle"
        )
    return Path(file_path)


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


def _load_session(src, ref: RawRef, threads_by_ref: dict) -> NormalizedSession | None:
    source_ref = ref.identity
    if not source_ref:
        print(f"skipped {ref.source} ref with no identity", file=sys.stderr)
        return None
    thread = threads_by_ref.get((ref.source, source_ref))
    if thread is None and ref.file:
        thread = threads_by_ref.get((ref.source, ref.file))
    parser_version = getattr(src, "parser_version", None)
    session = registry.cached_session(thread, ref, parser_version)
    if session is None:
        try:
            session = src.parse(ref)
        except SourceUnavailableError:
            raise
        except OSError as error:
            print(
                f"skipped {ref.file or f'{ref.source}:{source_ref}'}: {error}",
                file=sys.stderr,
            )
            return None
        session.cache_version = parser_version
        session.file_fingerprint = ref.fingerprint
    elif isinstance(src, CachedMetadataSource):
        src.refresh_cached_metadata(session)
    session.source_ref = source_ref
    return session


def _attach_git(session: NormalizedSession, cache: dict, git_resolver) -> None:
    if session.cwd is None:
        return
    if session.cwd not in cache:
        cache[session.cwd] = git_resolver(session.cwd)
    project = cache[session.cwd]
    if project is None:
        return
    session.git_project_key = project.key
    session.git_project_label = project.label
    session.git_root = project.root
    session.git_remote = project.remote
    session.git_clone_url = project.clone_url
    session.git_resolved = True


def scan(
    sources=None,
    git_resolver=None,
    cached_threads=None,
    *,
    unavailable_sources: set[str] | None = None,
) -> list[NormalizedSession]:
    if sources is None:
        sources = SOURCES
    if git_resolver is None:
        git_resolver = resolve_git_project
    threads_by_ref: dict[tuple[str, str], dict] = {}
    for thread in cached_threads or []:
        source_name = thread.get("source") or "claude-code"
        for source_ref in (thread.get("sourceRef"), thread.get("file")):
            if source_ref:
                threads_by_ref[(source_name, source_ref)] = thread
    cache: dict[str, GitProject | None] = {}  # resolved once per cwd
    out: list[NormalizedSession] = []
    for src in sources:
        try:
            for ref in src.discover():
                session = _load_session(src, ref, threads_by_ref)
                if session is None:
                    continue
                _attach_git(session, cache, git_resolver)
                out.append(session)
        except SourceUnavailableError as error:
            if unavailable_sources is not None:
                unavailable_sources.add(src.name)
            print(f"skipped {src.name} source: {error}", file=sys.stderr)
    return out
