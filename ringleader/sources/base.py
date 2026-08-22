from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


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


@runtime_checkable
class Source(Protocol):
    name: str
    capabilities: frozenset[str]

    def discover(self) -> Iterator[RawRef]: ...  # cheap: paths + mtime/size, no parse
    def parse(self, ref: RawRef) -> NormalizedSession: ...  # read tail, extract fields


@runtime_checkable
class NameApplyingSource(Source, Protocol):
    def apply_name(self, session_id: str, file_path: str, name: str) -> None: ...


@runtime_checkable
class PickerHidingSource(Source, Protocol):
    """A source whose agent keeps its own index of sessions, separate from the
    transcript files. Moving or deleting a transcript leaves such an index
    listing a session that no longer exists, so those sources get a hook to
    clear their entry first. Returns True when an entry was actually cleared."""

    def hide_from_picker(self, session_id: str, file_path: str) -> bool: ...


@runtime_checkable
class PickerShowingSource(Source, Protocol):
    """The inverse of `PickerHidingSource`: restore an entry the tool previously
    hid, so a session put back on disk becomes resumable again. `file_path` is the
    transcript's ORIGINAL location — the index still points there — so the file has
    to be back in place before this runs. Returns True when an entry was restored."""

    def show_in_picker(self, session_id: str, file_path: str) -> bool: ...
