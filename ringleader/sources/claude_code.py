from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from ..naming import title_line
from ..slug import normalize, slugify
from ..transcript import parse_transcript
from .base import NormalizedSession, RawRef, append_jsonl_line, stat_fingerprint


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
    parser_version: ClassVar[int] = 1
    projects_dir: Path = field(default_factory=default_projects_dir)
    tail_turns: int = 30
    name: str = "claude-code"
    capabilities: frozenset[str] = field(
        default_factory=lambda: frozenset({"applies_names", "relocates_workspace"})
    )

    def __post_init__(self) -> None:
        self.projects_dir = Path(self.projects_dir)

    @property
    def root(self) -> Path:
        return self.projects_dir

    def storage_for(self, cwd: str) -> Path:
        return self.projects_dir / slugify(normalize(cwd))

    def relocate_storage(self, old_cwd: str, new_cwd: str) -> dict[str, str]:
        """Rename the whole slug directory for `old_cwd`. Every session launched
        there moves with it, so the caller must own all of them."""
        src, dst = self.storage_for(old_cwd), self.storage_for(new_cwd)
        if not src.is_dir():
            return {}
        if dst.exists():
            raise RuntimeError(f"{dst} already exists")
        names = [p.name for p in src.glob("*.jsonl")]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return {str(src / n): str(dst / n) for n in names}

    def relocate_session(self, file_path: str, new_cwd: str) -> str:
        """Move one transcript, and the `<sessionId>/` sidecar dir if it has one,
        into the slug directory for `new_cwd`."""
        src = Path(file_path)
        dst_dir = self.storage_for(new_cwd)
        dst = dst_dir / src.name
        sidecar = src.with_suffix("")
        if dst.exists() or (sidecar.is_dir() and (dst_dir / sidecar.name).exists()):
            raise RuntimeError(f"{dst} already exists")
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        if sidecar.is_dir():
            shutil.move(str(sidecar), str(dst_dir / sidecar.name))
        return str(dst)

    def discover(self) -> Iterator[RawRef]:
        for p in _iter_session_files(self.projects_dir):
            try:
                st = p.stat()
            except OSError as e:
                print(f"skipped {p}: {e}", file=sys.stderr)
                continue
            yield RawRef(
                source=self.name,
                file=str(p),
                mtime=_iso_mtime(p),
                size_bytes=st.st_size,
                fingerprint=stat_fingerprint(st),
            )

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

    def apply_name(self, session_id: str, file_path: str, name: str) -> None:
        path = Path(file_path)
        path.stat()
        append_jsonl_line(path, title_line(session_id, name))
