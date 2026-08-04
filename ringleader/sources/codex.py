from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .base import NormalizedSession, RawRef, append_jsonl_line


class CodexSessionIndexError(RuntimeError):
    pass


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
    session_index_file: Path | None = None
    tail_turns: int = 30
    name: str = "codex"
    capabilities: frozenset[str] = field(default_factory=lambda: frozenset({"applies_names"}))
    _names_by_id: dict[str, str | None] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.sessions_dir = Path(self.sessions_dir)
        if self.session_index_file is None:
            self.session_index_file = self.sessions_dir.parent / "session_index.jsonl"
        else:
            self.session_index_file = Path(self.session_index_file)

    @property
    def root(self) -> Path:
        return self.sessions_dir

    def discover(self) -> Iterator[RawRef]:
        self._names_by_id = None
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
        session_id = meta.get("session_id") or path.stem
        _first, last, count = _message_stats(path)
        tail = last or ""
        return NormalizedSession(
            source=self.name,
            session_id=session_id,
            cwd=meta.get("cwd"),
            mtime=ref.mtime,
            size_bytes=ref.size_bytes,
            custom_title=self._thread_names().get(session_id),
            summary=None,
            last_prompt=last,
            file=ref.file,
            ai_title=None,
            message_count=count,
            tail_text=tail[:6000],
        )

    def _thread_names(self) -> dict[str, str | None]:
        if self._names_by_id is not None:
            return self._names_by_id
        names: dict[str, str | None] = {}
        try:
            lines = self.session_index_file.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        except OSError as e:
            raise CodexSessionIndexError(f"cannot read Codex session index: {e}") from e
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = entry.get("id")
            thread_name = entry.get("thread_name")
            if isinstance(session_id, str) and isinstance(thread_name, str):
                names[session_id] = thread_name.strip() or None
        self._names_by_id = names
        return names

    def apply_name(self, session_id: str, file_path: str, name: str) -> None:
        path = Path(file_path)
        path.stat()
        actual_session_id = _meta(path).get("session_id")
        if actual_session_id != session_id:
            raise ValueError(f"session id mismatch in {file_path}")
        updated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        line = json.dumps(
            {"id": session_id, "thread_name": name.strip(), "updated_at": updated_at},
            ensure_ascii=False,
        )
        append_jsonl_line(self.session_index_file, line)
        self._names_by_id = None
