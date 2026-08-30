from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
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


@dataclass(frozen=True)
class RawRef:
    source: str
    file: str  # absolute transcript path
    mtime: str
    size_bytes: int
    fingerprint: str | None = None


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
    file: str
    # Below: Claude-Code-observed extras beyond the cross-harness-generic core above.
    # registry.reconcile still persists these (messageCount drives prune eligibility,
    # gitBranch/aiTitle are display + classification context) — a future source that
    # can't supply them just leaves the defaults.
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


@runtime_checkable
class Source(Protocol):
    name: str
    capabilities: frozenset[str]

    def discover(self) -> Iterator[RawRef]: ...  # cheap: paths + mtime/size, no parse
    def parse(self, ref: RawRef) -> NormalizedSession: ...  # read tail, extract fields


def stat_fingerprint(st) -> str:
    return ":".join(
        str(value) for value in (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    )


@runtime_checkable
class CachedMetadataSource(Protocol):
    def refresh_cached_metadata(self, session: NormalizedSession) -> None: ...


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
class WorkspaceRebindingSource(Source, Protocol):
    """A source whose agent stores every transcript in one flat place and records
    the workspace separately, in its own index. Moving the workspace moves no
    transcript, so unlike `WorkspaceRelocatingSource` there is no destination to
    collide with and no new path for the registry to follow — only the recorded
    cwd, rewritten in place."""

    def rebind_workspace(self, session_id: str, file_path: str, new_cwd: str) -> IndexUpdate: ...


@runtime_checkable
class PickerShowingSource(Source, Protocol):
    """The inverse of `PickerHidingSource`: restore an entry the tool previously
    hid, so a session put back on disk becomes resumable again. `file_path` is the
    transcript's ORIGINAL location — the index still points there — so the file has
    to be back in place before this runs."""

    def show_in_picker(self, session_id: str, file_path: str) -> IndexUpdate: ...
