from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


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
    subagent_count: int
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

    def discover(self) -> Iterator[RawRef]: ...  # cheap: paths + mtime/size, no parse
    def parse(self, ref: RawRef) -> NormalizedSession: ...  # read tail, extract fields
