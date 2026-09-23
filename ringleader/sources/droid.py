from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path
from typing import ClassVar

from ..slug import normalize, slugify
from .base import NormalizedSession, RawRef, stat_fingerprint

_SIDECAR_SUFFIXES = (".settings.json", ".settings.json.bak")


def default_sessions_dir() -> Path:
    # FACTORY_HOME_OVERRIDE replaces HOME, not the .factory dir
    home = os.environ.get("FACTORY_HOME_OVERRIDE")
    return (Path(home) if home else Path.home()) / ".factory" / "sessions"


def _iso_mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat()


def _session_start(path: Path) -> dict:
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            record = json.loads(raw)
            if isinstance(record, dict) and record.get("type") == "session_start":
                return record
            return {}
    return {}


def _is_own_session(start: dict) -> bool:
    """Droid writes a transcript per thread: the interactive session and every
    subagent it spawns alike. Only a thread with no caller is one a human owns and
    can resume, which is also what `droid resume` lists."""
    return bool(start) and start.get("callingSessionId") is None


def _text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return " ".join(parts).strip()


def _session_fields(path: Path, tail_turns: int) -> dict:
    start: dict = {}
    turns: list[tuple[str, str]] = []
    last_prompt: str | None = None
    message_count = 0

    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(o, dict):
                continue

            if o.get("type") == "session_start":
                if not start:
                    start = o
            elif o.get("type") == "message":
                msg = o.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = _text_from_content(msg.get("content"))
                # droid files tool results as user-role messages; counting those would put
                # message_count out of scale with every other source's conversation turns
                if not text:
                    continue
                message_count += 1
                if role == "user":
                    last_prompt = text[:500]
                    turns.append(("USER", text))
                else:
                    turns.append(("ASSISTANT", text))

    tail = turns[-tail_turns:] if tail_turns else []
    title = start.get("title")
    manual = bool(start.get("isSessionTitleManuallySet"))
    return {
        "session_id": start.get("id"),
        "cwd": start.get("cwd"),
        "custom_title": title if manual else None,
        "ai_title": None if manual else title,
        "last_prompt": last_prompt,
        "message_count": message_count,
        "tail_text": "\n".join(f"{s}: {x}" for s, x in tail),
    }


@dataclass
class DroidSource:
    parser_version: ClassVar[int] = 1
    sessions_dir: Path = field(default_factory=default_sessions_dir)
    tail_turns: int = 30
    name: str = "droid"
    capabilities: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "companion_files",
                "filesystem_lifecycle",
                "relocates_workspace",
                "launches_sessions",
            }
        )
    )

    def __post_init__(self) -> None:
        self.sessions_dir = Path(self.sessions_dir)

    @property
    def root(self) -> Path:
        return self.sessions_dir

    def resume_argv(self, session_id: str) -> tuple[str, ...]:
        return ("droid", "resume", session_id)

    def launch_argv(self, prompt: str) -> tuple[str, ...]:
        return ("droid", prompt)

    def storage_for(self, cwd: str) -> Path:
        # droid encodes the launch directory exactly as Claude Code does
        return self.sessions_dir / slugify(normalize(cwd))

    def companion_paths(self, file_path: str) -> tuple[Path, ...]:
        src = Path(file_path)
        return tuple(src.with_name(src.stem + suffix) for suffix in _SIDECAR_SUFFIXES)

    def relocate_storage(self, old_cwd: str, new_cwd: str) -> dict[str, str]:
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
        src = Path(file_path)
        dst_dir = self.storage_for(new_cwd)
        dst = dst_dir / src.name
        sidecars = [p for p in self.companion_paths(file_path) if p.is_file()]
        if dst.exists() or any((dst_dir / p.name).exists() for p in sidecars):
            raise RuntimeError(f"{dst} already exists")
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        for sidecar in sidecars:
            shutil.move(str(sidecar), str(dst_dir / sidecar.name))
        return str(dst)

    def discover(self) -> Iterator[RawRef]:
        if not self.sessions_dir.exists():
            return
        seen: set[str] = set()
        globs = chain(self.sessions_dir.glob("*.jsonl"), self.sessions_dir.glob("*/*.jsonl"))
        for p in sorted(globs):
            if p.parent.name == "btw":
                continue  # a btw fork is its own layout, never re-filed under a slug dir
            try:
                st = p.stat()
                start = _session_start(p)
            except (OSError, json.JSONDecodeError) as e:
                print(f"skipped {p}: {e}", file=sys.stderr)
                continue
            if not _is_own_session(start):
                continue
            session_id = start.get("id")
            if not isinstance(session_id, str) or not session_id:
                print(f"skipped {p}: session_start has no usable id", file=sys.stderr)
                continue
            if session_id in seen:
                continue
            seen.add(session_id)
            yield RawRef(
                source=self.name,
                source_ref=session_id,
                file=str(p),
                mtime=_iso_mtime(p),
                size_bytes=st.st_size,
                fingerprint=stat_fingerprint(st),
            )

    def parse(self, ref: RawRef) -> NormalizedSession:
        path = ref.file_path()
        fields = _session_fields(path, self.tail_turns)
        return NormalizedSession(
            source=self.name,
            session_id=fields["session_id"] or path.stem,
            cwd=fields["cwd"],
            mtime=ref.mtime,
            size_bytes=ref.size_bytes,
            custom_title=fields["custom_title"],
            summary=None,
            last_prompt=fields["last_prompt"],
            file=ref.file,
            ai_title=fields["ai_title"],
            message_count=fields["message_count"],
            tail_text=fields["tail_text"][:6000],
            git_branch=None,
        )
