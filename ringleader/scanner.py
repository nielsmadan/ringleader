from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .transcript import parse_transcript
from .gitinfo import resolve_git_project


@dataclass
class SessionMeta:
    session_id: str
    file: str
    project: str | None
    title: str | None        # user-set custom title
    ai_title: str | None
    summary: str | None
    last_prompt: str | None
    mtime: str               # tz-aware ISO
    size_bytes: int
    message_count: int
    tail_text: str           # transient; used for classification, not persisted
    git_branch: str | None = None
    git_project_key: str | None = None
    git_project_label: str | None = None
    git_root: str | None = None
    git_remote: str | None = None


def projects_dir() -> Path:
    env = os.environ.get("SESSION_TRACKER_PROJECTS_DIR")
    return Path(env) if env else Path.home() / ".claude" / "projects"


def iter_session_files(root) -> Iterator[Path]:
    root = Path(root)
    if not root.exists():
        return
    # top-level session files only: <project>/<uuid>.jsonl
    for p in sorted(root.glob("*/*.jsonl")):
        if "subagents" in p.parts:
            continue
        yield p


def _iso_mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()


def scan(root, tail_turns: int = 30, git_resolver=None) -> list[SessionMeta]:
    if git_resolver is None:
        git_resolver = resolve_git_project
    cache: dict[str, object] = {}   # cwd -> GitProject|None, resolved once per cwd
    out: list[SessionMeta] = []
    for p in iter_session_files(root):
        try:
            info = parse_transcript(p, tail_turns=tail_turns)
            st = p.stat()
        except OSError as e:
            print(f"skipped {p}: {e}", file=sys.stderr)
            continue
        gp = None
        if info.cwd is not None:
            if info.cwd not in cache:
                cache[info.cwd] = git_resolver(info.cwd)
            gp = cache[info.cwd]
        out.append(SessionMeta(
            session_id=info.session_id,
            file=str(p),
            project=info.cwd,
            title=info.custom_title,
            ai_title=info.ai_title,
            summary=info.summary,
            last_prompt=info.last_prompt,
            mtime=_iso_mtime(p),
            size_bytes=st.st_size,
            message_count=info.message_count,
            tail_text=info.tail_text,
            git_branch=info.git_branch,
            git_project_key=gp.key if gp else None,
            git_project_label=gp.label if gp else None,
            git_root=gp.root if gp else None,
            git_remote=gp.remote if gp else None,
        ))
    return out
