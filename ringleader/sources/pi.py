from __future__ import annotations

import json
import os
import secrets
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from .base import NormalizedSession, RawRef, append_jsonl_line, stat_fingerprint


def default_sessions_dir() -> Path:
    env = os.environ.get("PI_SESSIONS_DIR")
    return Path(env) if env else Path.home() / ".pi" / "agent" / "sessions"


def _iso_mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat()


def _session_id(path: Path) -> str:
    return path.stem.split("_", 1)[-1]


def _text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                if isinstance(t, str) and t:
                    parts.append(t)
        return " ".join(parts).strip()
    return ""


def _session_fields(path: Path, tail_turns: int) -> dict:
    cwd: str | None = None
    session_id: str | None = None
    custom_title: str | None = None
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

            t = o.get("type")

            if t == "session":
                if session_id is None and o.get("id"):
                    session_id = o["id"]
                if cwd is None and o.get("cwd"):
                    cwd = o["cwd"]

            elif t == "session_info":
                name = o.get("name")
                if isinstance(name, str):
                    sanitized = name.replace("\r", " ").replace("\n", " ").strip()
                    custom_title = sanitized if sanitized else None

            elif t == "message":
                msg = o.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue
                message_count += 1
                text = _text_from_content(msg.get("content")).strip()
                if not text:
                    continue
                if role == "user":
                    last_prompt = text[:500]
                    turns.append(("USER", text))
                else:
                    turns.append(("ASSISTANT", text))

    tail = turns[-tail_turns:] if tail_turns else []
    tail_text = "\n".join(f"{s}: {x}" for s, x in tail)

    return {
        "cwd": cwd,
        "session_id": session_id,
        "custom_title": custom_title,
        "last_prompt": last_prompt,
        "message_count": message_count,
        "tail_text": tail_text,
    }


@dataclass
class PiSource:
    parser_version: ClassVar[int] = 1
    sessions_dir: Path = field(default_factory=default_sessions_dir)
    tail_turns: int = 30
    name: str = "pi"
    capabilities: frozenset[str] = field(
        default_factory=lambda: frozenset({"applies_names", "filesystem_lifecycle"})
    )

    def __post_init__(self) -> None:
        self.sessions_dir = Path(self.sessions_dir)

    @property
    def root(self) -> Path:
        return self.sessions_dir

    def discover(self) -> Iterator[RawRef]:
        if not self.sessions_dir.exists():
            return
        for p in sorted(self.sessions_dir.glob("*/*.jsonl")):
            # One directory level includes sessions but excludes nested subagent runs.
            try:
                st = p.stat()
            except OSError as e:
                print(f"skipped {p}: {e}", file=sys.stderr)
                continue
            yield RawRef(
                source=self.name,
                source_ref=_session_id(p),
                file=str(p),
                mtime=_iso_mtime(p),
                size_bytes=st.st_size,
                fingerprint=stat_fingerprint(st),
            )

    def parse(self, ref: RawRef) -> NormalizedSession:
        path = ref.file_path()
        fields = _session_fields(path, self.tail_turns)
        session_id = fields["session_id"] or _session_id(path)
        return NormalizedSession(
            source=self.name,
            session_id=session_id,
            cwd=fields["cwd"],
            mtime=ref.mtime,
            size_bytes=ref.size_bytes,
            custom_title=fields["custom_title"],
            summary=None,
            last_prompt=fields["last_prompt"],
            file=ref.file,
            ai_title=None,
            message_count=fields["message_count"],
            tail_text=fields["tail_text"][:6000],
        )

    def apply_name(self, session_id: str, file_path: str, name: str) -> None:
        path = Path(file_path)
        header_id: str | None = None
        last_id: str | None = None
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if o.get("type") == "session" and o.get("id"):
                        header_id = o.get("id") if header_id is None else header_id
                    entry_id = o.get("id")
                    if isinstance(entry_id, str) and entry_id:
                        last_id = entry_id
        except OSError:
            raise
        expected = header_id or _session_id(path)
        if expected != session_id:
            raise ValueError(f"session id mismatch in {file_path}")
        sanitized = name.replace("\r", " ").replace("\n", " ").strip()
        parent_id = last_id
        entry_id = secrets.token_hex(4)
        line = json.dumps(
            {
                "type": "session_info",
                "id": entry_id,
                "parentId": parent_id,
                "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "name": sanitized,
            },
            ensure_ascii=False,
        )
        path.stat()
        append_jsonl_line(path, line)
