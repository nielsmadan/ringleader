from __future__ import annotations

import shutil
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast

from . import registry
from .gitinfo import GitProject, resolve_git_project
from .sources.base import (
    CachedMetadataSource,
    CompanionFileSource,
    IndexUpdate,
    NormalizedSession,
    PickerHidingSource,
    PickerShowingSource,
    RawRef,
    Source,
    SourceUnavailableError,
    WorkspaceRebindingSource,
    WorkspaceRelocatingSource,
)
from .sources.claude_code import ClaudeCodeSource
from .sources.codex import CodexSource
from .sources.droid import DroidSource
from .sources.opencode import OpenCodeSource
from .sources.pi import PiSource

SOURCE_TYPES = (ClaudeCodeSource, CodexSource, DroidSource, OpenCodeSource, PiSource)


def _new_sources() -> list[Source]:
    return [source_type() for source_type in SOURCE_TYPES]


SOURCES = _new_sources()


def active_sources(
    *,
    unavailable_sources: set[str] | None = None,
    recorded_threads: Iterable[dict[str, Any]] = (),
) -> list[Source]:
    # Fresh instances (not SOURCES) so each call re-reads the env-based default
    # roots — SOURCES is constructed once at import time and would otherwise
    # keep whatever root was current then, which breaks under monkeypatched env.
    active = []
    candidates = _new_sources()

    def source_key(source: Source) -> tuple[str, Path]:
        return source.name, cast(Path, getattr(source, "root", None)).resolve()

    known = {source_key(source) for source in candidates}
    for thread in recorded_threads:
        if not thread.get("nativeRoots"):
            continue
        recorded_source = source_for_thread(thread)
        if recorded_source is not None and source_key(recorded_source) not in known:
            candidates.append(recorded_source)
            known.add(source_key(recorded_source))
    for source in candidates:
        available = getattr(source, "available", None)
        # `root` is not part of the Source protocol: a source that only parses
        # transcripts has no session storage of its own to report.
        root = cast(Path, getattr(source, "root", None))
        try:
            root.stat()
        except FileNotFoundError:
            continue
        except OSError as error:
            if unavailable_sources is not None:
                unavailable_sources.add(source.name)
            print(f"skipped {source.name} source at {root}: {error}", file=sys.stderr)
            continue
        try:
            present = available() if callable(available) else True
        except OSError as error:
            if unavailable_sources is not None:
                unavailable_sources.add(source.name)
            print(f"skipped {source.name} source at {root}: {error}", file=sys.stderr)
            continue
        if present:
            active.append(source)
            continue
        if unavailable_sources is not None:
            unavailable_sources.add(source.name)
        print(f"skipped {source.name} source at {root}: unavailable", file=sys.stderr)
    return active


def source_by_name(name: str) -> Any:
    source_type = next((st for st in SOURCE_TYPES if st.name == name), None)
    return source_type() if source_type else None


def storage_slug(name: str, cwd: str) -> str:
    """The directory `name`'s agent files a session launched in `cwd` under."""
    source = source_by_name(name)
    if not supports(source, "relocates_workspace", WorkspaceRelocatingSource):
        raise ValueError(f"{name} does not file sessions under a workspace directory")
    return cast(WorkspaceRelocatingSource, source).storage_for(cwd).name


def stored_under(file: str | Path, cwd: str, name: str) -> bool:
    return Path(file).parent.name == storage_slug(name, cwd)


def source_for_thread(thread: dict[str, Any]) -> Source | None:
    name = thread.get("source") or "claude-code"
    stored = thread.get("nativeRoots")
    if not stored:
        return cast(Source | None, source_by_name(name))
    store = Path(stored["store"])
    if not store.is_absolute():
        raise ValueError("Recorded native store must be absolute")
    factories: dict[str, Callable[[], Source]] = {
        "claude-code": lambda: ClaudeCodeSource(projects_dir=store / "projects"),
        "codex": lambda: CodexSource(sessions_dir=store / "sessions"),
        "pi": lambda: PiSource(sessions_dir=store / "sessions"),
        "opencode": lambda: OpenCodeSource(db_path=store / "opencode.db"),
    }
    factory = factories.get(name)
    return factory() if factory else cast(Source | None, source_by_name(name))


def supports(source: Source | None, capability: str, protocol: type) -> bool:
    """Both halves of an opt-in capability: the string says the source means to offer
    it, the protocol says it actually can. They are declared separately, so checking
    only one lets a source with a typo'd capability be dropped in silence."""
    if not source:
        return False
    return capability in source.capabilities and isinstance(source, protocol)


def sources_with(capability: str, protocol: type) -> dict[str, Any]:
    """Every active source offering `capability`, keyed by name."""
    return {s.name: s for s in active_sources() if supports(s, capability, protocol)}


def filesystem_path(thread: dict[str, Any], operation: str) -> Path:
    source_name = thread.get("source") or "claude-code"
    source = source_for_thread(thread)
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


def companion_paths(source_name: str | None, file_path: str | Path) -> tuple[Path, ...]:
    """The per-transcript files that belong with `file_path` — Claude Code's
    `<sessionId>/` directory, droid's `.settings.json` sidecars. Empty for a source
    that keeps none, so archive/delete/prune can treat every source the same."""
    src = source_by_name(source_name or "claude-code")
    if not supports(src, "companion_files", CompanionFileSource):
        return ()
    return tuple(cast(CompanionFileSource, src).companion_paths(str(file_path)))


def remove_companions(paths: Iterable[Path]) -> None:
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def hide_from_picker(thread: dict[str, Any]) -> IndexUpdate:
    """Clear the agent's own resume-picker entry for a thread, for sources that
    keep an index alongside the transcripts. Raises if the source has such an
    index but cannot be updated — callers must not then move or delete the
    transcript, or the picker is left pointing at a file that is gone."""
    src = source_for_thread(thread)
    if not supports(src, "hides_from_picker", PickerHidingSource):
        return IndexUpdate.NO_INDEX
    return cast(PickerHidingSource, src).hide_from_picker(thread["sessionId"], thread["file"])


def show_in_picker(thread: dict[str, Any], original_path: str) -> IndexUpdate:
    """Restore a picker entry hidden by `hide_from_picker`. `original_path` is where
    the transcript lived when it was hidden — the index still points there — so the
    file must be back at that path before calling this."""
    src = source_for_thread(thread)
    if not supports(src, "shows_in_picker", PickerShowingSource):
        return IndexUpdate.NO_INDEX
    return cast(PickerShowingSource, src).show_in_picker(thread["sessionId"], original_path)


def rebind_workspace(thread: dict[str, Any], file_path: str, new_cwd: str) -> IndexUpdate:
    """Point the agent's own index at a session's new workspace, for sources that
    record it separately from the transcript. `file_path` is where the index believes
    the transcript lives, which after an archive is not where it is now."""
    src = source_for_thread(thread)
    if not supports(src, "rebinds_workspace", WorkspaceRebindingSource):
        return IndexUpdate.NO_INDEX
    return cast(WorkspaceRebindingSource, src).rebind_workspace(
        thread["sessionId"], file_path, new_cwd
    )


def _load_session(
    src: Source, ref: RawRef, threads_by_ref: dict[tuple[str, str], dict[str, Any]]
) -> NormalizedSession | None:
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


def _attach_git(
    session: NormalizedSession,
    cache: dict[str, GitProject | None],
    git_resolver: Callable[[str], GitProject | None],
) -> None:
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
    sources: Iterable[Source] | None = None,
    git_resolver: Callable[[str], GitProject | None] | None = None,
    cached_threads: Iterable[dict[str, Any]] | None = None,
    *,
    unavailable_sources: set[str] | None = None,
) -> list[NormalizedSession]:
    if sources is None:
        sources = SOURCES
    if git_resolver is None:
        git_resolver = resolve_git_project
    threads_by_ref: dict[tuple[str, str], dict[str, Any]] = {}
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
