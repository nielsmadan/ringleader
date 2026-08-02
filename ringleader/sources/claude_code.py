from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..transcript import parse_transcript
from .base import NormalizedSession, RawRef


def default_projects_dir() -> Path:
    env = os.environ.get("SESSION_TRACKER_PROJECTS_DIR")
    return Path(env) if env else Path.home() / ".claude" / "projects"


def _iter_session_files(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    # top-level session files only: <project>/<uuid>.jsonl
    for p in sorted(root.glob("*/*.jsonl")):
        if "subagents" in p.parts:
            continue
        yield p


def _iso_mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat()


@dataclass
class ClaudeCodeSource:
    projects_dir: Path = field(default_factory=default_projects_dir)
    tail_turns: int = 30
    name: str = "claude-code"

    def __post_init__(self) -> None:
        self.projects_dir = Path(self.projects_dir)

    def discover(self) -> Iterator[RawRef]:
        for p in _iter_session_files(self.projects_dir):
            try:
                st = p.stat()
            except OSError as e:
                print(f"skipped {p}: {e}", file=sys.stderr)
                continue
            yield RawRef(source=self.name, file=str(p), mtime=_iso_mtime(p), size_bytes=st.st_size)

    def parse(self, ref: RawRef) -> NormalizedSession:
        path = Path(ref.file)
        info = parse_transcript(path, tail_turns=self.tail_turns)
        return NormalizedSession(
            source=self.name,
            session_id=info.session_id,
            cwd=info.cwd,
            mtime=ref.mtime,
            size_bytes=ref.size_bytes,
            custom_title=info.custom_title,
            summary=info.summary,
            last_prompt=info.last_prompt,
            file=ref.file,
            ai_title=info.ai_title,
            message_count=info.message_count,
            tail_text=info.tail_text,
            git_branch=info.git_branch,
        )
