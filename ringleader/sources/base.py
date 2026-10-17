from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable


class IndexUpdate(Enum):
    """Outcome of writing to an agent's own session index. A bare bool cannot
    separate the three non-writes, and they need opposite handling: `NO_INDEX`
    means the agent keeps no index here, so a caller that reports failure is
    wrong; `NO_ENTRY` means it does and we could not find the session, which is
    the only one worth warning about; `ALREADY_CURRENT` is success on a re-run."""

    UPDATED = "updated"
    ALREADY_CURRENT = "already_current"
    NO_ENTRY = "no_entry"
    NO_INDEX = "no_index"


class SourceUnavailableError(OSError):
    pass


def append_jsonl_line(path: Path, line: str) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    if size == 0:
        lead = b""
    else:
        with path.open("rb") as fh:
            fh.seek(-1, 2)
            lead = b"" if fh.read(1) == b"\n" else b"\n"
    with path.open("ab") as fh:
        fh.write(lead + line.encode("utf-8") + b"\n")


def in_workspace(project: str | None, cwd: str | None, *, recursive: bool) -> bool:
    """Whether a session recorded at `project` belongs to `cwd` (or lies under it, with
    `recursive`; anywhere, with no `cwd`)."""
    if cwd is None:
        return True
    if not project:
        return False
    project, root = project.rstrip("/"), cwd.rstrip("/")
    return project == root or (recursive and project.startswith(root + "/"))


def workspace_dirs(root: Path, name: str | None, *, recursive: bool) -> list[Path]:
    """`name`'s storage dir, plus — with `recursive` — every dir whose name extends it,
    which over-matches siblings sharing a prefix; the caller filters on the parsed cwd.
    No `name` means every workspace."""
    if not root.is_dir():
        return []
    return sorted(
        d
        for d in root.iterdir()
        if d.is_dir()
        and (name is None or d.name == name or (recursive and d.name.startswith(name + "-")))
    )


def changed_since(path: Path, since: datetime) -> bool:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return True  # let discovery report it rather than drop it silently
    return datetime.fromtimestamp(mtime, tz=UTC) >= since


def existing_files(paths: frozenset[str]) -> set[Path]:
    return {Path(p) for p in paths if Path(p).is_file()}


@dataclass(frozen=True)
class RawRef:
    source: str
    file: str | None
    mtime: str
    size_bytes: int
    fingerprint: str | None = None
    source_ref: str | None = None

    @property
    def identity(self) -> str | None:
        return self.source_ref or self.file

    def file_path(self) -> Path:
        if self.file is None:
            raise ValueError(f"{self.source} reference has no filesystem path")
        return Path(self.file)


@dataclass
class NormalizedSession:
    source: str
    session_id: str
    cwd: str | None
    mtime: str
    size_bytes: int
    custom_title: str | None
    summary: str | None
    last_prompt: str | None
    file: str | None
    # Below: Claude-Code-observed extras beyond the cross-harness-generic core above.
    # registry.reconcile still persists these (messageCount drives prune eligibility,
    # gitBranch/aiTitle are display + classification context) — a source that can't
    # supply them leaves the defaults.
    ai_title: str | None = None
    message_count: int = 0
    tail_text: str = ""  # transient; used for classification, not persisted
    git_branch: str | None = None
    git_project_key: str | None = None
    git_project_label: str | None = None
    git_root: str | None = None
    git_remote: str | None = None
    git_clone_url: str | None = None
    cache_version: int | None = None
    file_fingerprint: str | None = None
    git_resolved: bool = False
    source_ref: str | None = None


@runtime_checkable
class Source(Protocol):
    name: str
    capabilities: frozenset[str]

    def discover(self) -> Iterator[RawRef]: ...
    def parse(self, ref: RawRef) -> NormalizedSession: ...


def stat_fingerprint(st: os.stat_result) -> str:
    return ":".join(
        str(value) for value in (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    )


@runtime_checkable
class CachedMetadataSource(Protocol):
    def refresh_cached_metadata(self, session: NormalizedSession) -> None: ...


@runtime_checkable
class ConversationRenderingSource(Source, Protocol):
    def render_conversation(self, source_ref: str) -> str: ...


@runtime_checkable
class NameApplyingSource(Source, Protocol):
    def apply_name(self, session_id: str, file_path: str, name: str) -> None: ...


@runtime_checkable
class PickerHidingSource(Source, Protocol):
    """A source whose agent keeps its own index of sessions, separate from the
    transcript files. Moving or deleting a transcript leaves such an index
    listing a session that no longer exists, so those sources get a hook to
    clear their entry first."""

    def hide_from_picker(self, session_id: str, file_path: str) -> IndexUpdate: ...


@runtime_checkable
class CompanionFileSource(Source, Protocol):
    """A source whose agent keeps per-transcript state beside the transcript —
    Claude Code's `<sessionId>/` directory, droid's `<sessionId>.settings.json`
    sidecars. Archive, delete and prune have to carry or remove them with the
    transcript, or the session is split from its own state.

    The return is a set of candidate paths derived from the transcript's, not a
    listing of what is there: callers check existence themselves, and the same
    derivation names the destination once a transcript has moved."""

    def companion_paths(self, file_path: str) -> tuple[Path, ...]: ...


@runtime_checkable
class WorkspaceRelocatingSource(Source, Protocol):
    """A source whose agent keys session storage by the directory a session was
    launched in, so moving a workspace leaves its sessions unreachable from the new
    path until the storage moves too. Both methods raise RuntimeError rather than
    merge when the destination is occupied — the caller checks up front and refuses,
    so a move never half-completes.

    `relocate_storage` moves every session launched at `old_cwd`; `relocate_session`
    moves one, for a session whose recorded cwd has drifted from its launch dir.
    Both return the old->new transcript paths the registry must follow."""

    def storage_for(self, cwd: str) -> Path: ...
    def relocate_storage(self, old_cwd: str, new_cwd: str) -> dict[str, str]: ...
    def relocate_session(self, file_path: str, new_cwd: str) -> str: ...


@runtime_checkable
class ScopedDiscoverySource(Source, Protocol):
    """A source that can find what changed in one workspace without discovering every
    session it holds. The refs cover the sessions launched at `cwd` (or under it, with
    `recursive`; anywhere, with no `cwd`) plus the files in `known` — the registry's own
    in-scope transcripts, which a drifted or relocated session is not filed by — that
    changed after `since`. Extra refs are fine: the caller filters on the parsed project."""

    def discover_scoped(
        self, cwd: str | None, *, recursive: bool, since: datetime, known: frozenset[str]
    ) -> Iterator[RawRef]: ...


@runtime_checkable
class WorkspaceRebindingSource(Source, Protocol):
    """A source whose agent stores every transcript in one flat place and records
    the workspace separately, in its own index. Moving the workspace moves no
    transcript, so unlike `WorkspaceRelocatingSource` there is no destination to
    collide with and no new path for the registry to follow — only the recorded
    cwd, rewritten in place."""

    def rebind_workspace(self, session_id: str, file_path: str, new_cwd: str) -> IndexUpdate: ...


@runtime_checkable
class SessionLaunchingSource(Source, Protocol):
    """A source whose agent can resume or fork its own sessions, or start fresh with
    an opening prompt. All return argv whose first element is the binary, never a
    shell string — and none carries a directory, because every
    agent resolves a session id relative to the directory it is launched in, so the
    caller chdirs first."""

    def resume_argv(self, session_id: str) -> tuple[str, ...]: ...
    def fork_argv(self, session_id: str) -> tuple[str, ...]: ...
    def launch_argv(self, prompt: str) -> tuple[str, ...]: ...


@runtime_checkable
class PickerShowingSource(Source, Protocol):
    """The inverse of `PickerHidingSource`: restore an entry the tool previously
    hid, so a session put back on disk becomes resumable again. `file_path` is the
    transcript's ORIGINAL location — the index still points there — so the file has
    to be back in place before this runs."""

    def show_in_picker(self, session_id: str, file_path: str) -> IndexUpdate: ...
