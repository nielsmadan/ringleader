from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from ..naming import title_line
from ..slug import normalize, slugify
from ..transcript import parse_transcript
from .base import (
    NormalizedSession,
    RawRef,
    append_jsonl_line,
    changed_since,
    existing_files,
    iso_mtime,
    stat_fingerprint,
    workspace_dirs,
)


def default_projects_dir() -> Path:
    env = os.environ.get("SESSION_TRACKER_PROJECTS_DIR")
    return (
        Path(env)
        if env
        else Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    )


def _iter_session_files(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    # top-level session files only: <project>/<uuid>.jsonl
    for p in sorted(root.glob("*/*.jsonl")):
        if "subagents" in p.parts:
            continue
        yield p


@dataclass
class ClaudeCodeSource:
    parser_version: ClassVar[int] = 2
    projects_dir: Path = field(default_factory=default_projects_dir)
    tail_turns: int = 30
    name: str = "claude-code"
    capabilities: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "applies_names",
                "companion_files",
                "discovers_scoped",
                "filesystem_lifecycle",
                "relocates_workspace",
                "launches_sessions",
                "forks_sessions",
            }
        )
    )

    def __post_init__(self) -> None:
        self.projects_dir = Path(self.projects_dir)

    @property
    def root(self) -> Path:
        return self.projects_dir

    def resume_argv(self, session_id: str) -> tuple[str, ...]:
        return ("claude", "--resume", session_id)

    def fork_argv(self, session_id: str) -> tuple[str, ...]:
        return ("claude", "--resume", session_id, "--fork-session")

    def launch_argv(self, prompt: str | None = None) -> tuple[str, ...]:
        if prompt is None:
            return ("claude",)
        return ("claude", prompt)

    def storage_for(self, cwd: str) -> Path:
        return self.projects_dir / slugify(normalize(cwd))

    def companion_paths(self, file_path: str) -> tuple[Path, ...]:
        return (Path(file_path).with_suffix(""),)  # <sessionId>.jsonl -> <sessionId>/

    def companion_destination(self, companion: Path, file_path: str) -> Path:
        return Path(file_path).parent / companion.name

    def storage_dir(self, file_path: str) -> Path:
        return Path(file_path).parent

    def stored_transcripts(self, storage: Path) -> list[Path]:
        return sorted(storage.glob("*.jsonl"))

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
        sidecar = self.companion_paths(file_path)[0]
        if dst.exists() or (sidecar.is_dir() and (dst_dir / sidecar.name).exists()):
            raise RuntimeError(f"{dst} already exists")
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        if sidecar.is_dir():
            shutil.move(str(sidecar), str(dst_dir / sidecar.name))
        return str(dst)

    def discover(self) -> Iterator[RawRef]:
        return self._refs(_iter_session_files(self.projects_dir))

    def discover_scoped(
        self, cwd: str | None, *, recursive: bool, since: datetime, known: frozenset[str]
    ) -> Iterator[RawRef]:
        name = self.storage_for(cwd).name if cwd is not None else None
        dirs = workspace_dirs(self.projects_dir, name, recursive=recursive)
        files = {p for d in dirs for p in d.glob("*.jsonl")} | existing_files(known)
        return self._refs(sorted(p for p in files if changed_since(p, since)))

    def _refs(self, files: Iterable[Path]) -> Iterator[RawRef]:
        for p in files:
            try:
                st = p.stat()
            except OSError as e:
                print(f"skipped {p}: {e}", file=sys.stderr)
                continue
            yield RawRef(
                source=self.name,
                source_ref=p.stem,
                file=str(p),
                mtime=iso_mtime(st),
                size_bytes=st.st_size,
                fingerprint=stat_fingerprint(st),
            )

    def parse(self, ref: RawRef) -> NormalizedSession:
        path = ref.file_path()
        info = parse_transcript(path, tail_turns=self.tail_turns)
        return NormalizedSession(
            source=self.name,
            session_id=info.session_id,
            cwd=info.cwd,
            mtime=ref.mtime,
            started_at=info.started_at,
            size_bytes=ref.size_bytes,
            custom_title=info.custom_title,
            summary=info.summary,
            last_prompt=info.last_prompt,
            file=ref.file,
            ai_title=info.ai_title,
            message_count=info.message_count,
            tail_text=info.tail_text[:6000],
            git_branch=info.git_branch,
        )

    def apply_name(self, session_id: str, file_path: str, name: str) -> None:
        path = Path(file_path)
        path.stat()
        append_jsonl_line(path, title_line(session_id, name))
