from __future__ import annotations

import json
import os
import shutil
import sys
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from itertools import chain
from pathlib import Path
from typing import Any, ClassVar

from ..slug import droid_slug
from .base import (
    FingerprintMemo,
    NormalizedSession,
    RawRef,
    changed_since,
    existing_files,
    iso_mtime,
    iso_timestamp,
    memo_by_fingerprint,
    stat_fingerprint,
    workspace_dirs,
)

_SIDECAR_SUFFIXES = (".settings.json", ".settings.json.bak")


def default_sessions_dir() -> Path:
    # FACTORY_HOME_OVERRIDE replaces HOME, not the .factory dir
    home = os.environ.get("FACTORY_HOME_OVERRIDE")
    return (Path(home) if home else Path.home()) / ".factory" / "sessions"


def _session_start(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            record = json.loads(raw)
            if isinstance(record, dict) and record.get("type") == "session_start":
                return record
            return {}
    return {}


def _is_own_session(start: dict[str, Any]) -> bool:
    """Droid writes a transcript per thread: the interactive session and every
    subagent it spawns alike. Only a thread with no caller is one a human owns and
    can resume, which is also what `droid resume` lists."""
    return bool(start) and start.get("callingSessionId") is None


def _header(path: Path) -> tuple[bool, object]:
    start = _session_start(path)
    return _is_own_session(start), start.get("id")


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


def _session_fields(path: Path, tail_turns: int) -> dict[str, Any]:
    start: dict[str, Any] = {}
    turns: deque[tuple[str, str]] = deque(maxlen=max(tail_turns, 0))
    last_prompt: str | None = None
    message_count = 0
    started_at: str | None = None

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

            if started_at is None:
                started_at = iso_timestamp(o.get("timestamp"))

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

    title = start.get("title")
    manual = bool(start.get("isSessionTitleManuallySet"))
    return {
        "session_id": start.get("id"),
        "started_at": started_at,
        "cwd": start.get("cwd"),
        "custom_title": title if manual else None,
        "ai_title": None if manual else title,
        "last_prompt": last_prompt,
        "message_count": message_count,
        "tail_text": "\n".join(f"{s}: {x}" for s, x in turns),
    }


@dataclass
class DroidSource:
    parser_version: ClassVar[int] = 2
    sessions_dir: Path = field(default_factory=default_sessions_dir)
    tail_turns: int = 30
    name: str = "droid"
    capabilities: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "companion_files",
                "discovers_scoped",
                "filesystem_lifecycle",
                "relocates_workspace",
                "launches_sessions",
            }
        )
    )
    _headers: FingerprintMemo[tuple[bool, object]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self.sessions_dir = Path(self.sessions_dir)

    @property
    def root(self) -> Path:
        return self.sessions_dir

    def resume_argv(self, session_id: str) -> tuple[str, ...]:
        return ("droid", "resume", session_id)

    def fork_argv(self, session_id: str) -> tuple[str, ...]:
        return ("droid", "resume", session_id, "--fork")

    def launch_argv(self, prompt: str) -> tuple[str, ...]:
        return ("droid", prompt)

    def storage_for(self, cwd: str) -> Path:
        return self.sessions_dir / droid_slug(cwd)

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
            return iter(())
        globs = chain(self.sessions_dir.glob("*.jsonl"), self.sessions_dir.glob("*/*.jsonl"))
        return self._refs(sorted(globs), complete=True)

    def discover_scoped(
        self, cwd: str | None, *, recursive: bool, since: datetime, known: frozenset[str]
    ) -> Iterator[RawRef]:
        if not self.sessions_dir.exists():
            return iter(())
        name = self.storage_for(cwd).name if cwd is not None else None
        dirs = workspace_dirs(self.sessions_dir, name, recursive=recursive)
        files = {
            *self.sessions_dir.glob("*.jsonl"),  # a top-level transcript names no workspace
            *(p for d in dirs for p in d.glob("*.jsonl")),
            *existing_files(known),
        }
        return self._refs(sorted(p for p in files if changed_since(p, since)), complete=False)

    def _refs(self, files: Iterable[Path], *, complete: bool) -> Iterator[RawRef]:
        seen: set[str] = set()
        headers: FingerprintMemo[tuple[bool, object]] = {}
        for p in files:
            if p.parent.name == "btw":
                continue  # a btw fork is its own layout, never re-filed under a slug dir
            try:
                st = p.stat()
                fingerprint = stat_fingerprint(st)
                headers[str(p)] = entry = memo_by_fingerprint(
                    self._headers, p, fingerprint, _header
                )
            except (OSError, json.JSONDecodeError) as e:
                print(f"skipped {p}: {e}", file=sys.stderr)
                continue
            own, session_id = entry[1]
            if not own:
                continue
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
                mtime=iso_mtime(st),
                size_bytes=st.st_size,
                fingerprint=fingerprint,
            )
        self._headers = headers if complete else {**self._headers, **headers}

    def parse(self, ref: RawRef) -> NormalizedSession:
        path = ref.file_path()
        fields = _session_fields(path, self.tail_turns)
        return NormalizedSession(
            source=self.name,
            session_id=fields["session_id"] or path.stem,
            cwd=fields["cwd"],
            started_at=fields["started_at"],
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
