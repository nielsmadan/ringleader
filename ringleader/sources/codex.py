from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .base import NormalizedSession, RawRef


def default_codex_dir() -> Path:
    env = os.environ.get("CODEX_SESSIONS_DIR")
    return Path(env) if env else Path.home() / ".codex" / "sessions"


def _iso_mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat()


def _is_preamble(text: str) -> bool:
    t = text.lstrip()
    return t.startswith("# AGENTS.md") or "<INSTRUCTIONS>" in text


def _message_stats(path: Path) -> tuple[str | None, str | None, int]:
    """Return (first user prompt, last user prompt, message_count).

    message_count is non-preamble user prompts + assistant messages, so it is
    comparable to Claude's turn count (which counts both roles) rather than
    only counting the human side of the conversation.
    """
    prompts: list[str] = []
    first_seen = False
    assistant_count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") != "response_item":
            continue
        p = o.get("payload", {})
        if p.get("type") != "message":
            continue
        role = p.get("role")
        if role == "assistant":
            assistant_count += 1
            continue
        if role != "user":
            continue
        text = " ".join(
            c.get("text", "") for c in p.get("content", []) if isinstance(c, dict)
        ).strip()
        if not text:
            continue
        if not first_seen:
            first_seen = True
            if _is_preamble(text):
                continue  # drop only the injected first-message preamble
        prompts.append(text)
    first = prompts[0] if prompts else None
    last = prompts[-1] if prompts else None
    return first, last, len(prompts) + assistant_count


def _meta(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            o = json.loads(line)
            if o.get("type") == "session_meta":
                return o.get("payload", {})
            return {}
    return {}


@dataclass
class CodexSource:
    sessions_dir: Path = field(default_factory=default_codex_dir)
    tail_turns: int = 30
    name: str = "codex"
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        self.sessions_dir = Path(self.sessions_dir)

    @property
    def root(self) -> Path:
        return self.sessions_dir

    def discover(self) -> Iterator[RawRef]:
        if not self.sessions_dir.exists():
            return
        for p in sorted(self.sessions_dir.glob("**/*.jsonl")):
            try:
                st = p.stat()
            except OSError as e:
                print(f"skipped {p}: {e}", file=sys.stderr)
                continue
            yield RawRef(source=self.name, file=str(p), mtime=_iso_mtime(p), size_bytes=st.st_size)

    def parse(self, ref: RawRef) -> NormalizedSession:
        path = Path(ref.file)
        meta = _meta(path)
        _first, last, count = _message_stats(path)
        tail = last or ""
        return NormalizedSession(
            source=self.name,
            session_id=meta.get("session_id") or path.stem,
            cwd=meta.get("cwd"),
            mtime=ref.mtime,
            size_bytes=ref.size_bytes,
            custom_title=None,
            summary=None,
            last_prompt=last,
            file=ref.file,
            ai_title=None,
            message_count=count,
            tail_text=tail[:6000],
        )
