from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import sys
from collections import deque
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from ..paths import cursor_dir
from ..slug import cursor_chat_key, cursor_slug, normalize
from ..stateio import atomic_write_text
from .base import NormalizedSession, RawRef, iso_mtime, stat_fingerprint

_USER_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)


def default_config_dir() -> Path:
    return cursor_dir()


def default_data_dir() -> Path:
    explicit = os.environ.get("CURSOR_DATA_DIR", "").strip()
    return Path(explicit) if explicit else Path.home() / ".cursor"


def read_meta(chat_dir: Path) -> dict[str, Any]:
    data = json.loads((chat_dir / "meta.json").read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _is_subagent(chat_dir: Path) -> bool:
    store = chat_dir / "store.db"
    if not store.is_file():
        return False
    try:
        with closing(sqlite3.connect(f"file:{store}?mode=ro", uri=True)) as db:
            row = db.execute("select value from meta where key = '0'").fetchone()
    except sqlite3.Error:
        return False
    try:
        meta = json.loads(bytes.fromhex(row[0])) if row else {}
    except (ValueError, TypeError):
        return False
    return isinstance(meta, dict) and meta.get("subagentInfo") is not None


def _iso_ms(value: object) -> str | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _text_from_content(content: object) -> str:
    if not isinstance(content, list):
        return ""
    return " ".join(
        text
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(text := block.get("text"), str)
        and text
    ).strip()


def prompt_text(text: str) -> str:
    query = _USER_QUERY.search(text)
    return query.group(1).strip() if query else text


def _session_fields(path: Path, tail_turns: int) -> dict[str, Any]:
    turns: deque[tuple[str, str]] = deque(maxlen=max(tail_turns, 0))
    last_prompt: str | None = None
    message_count = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("role") not in ("user", "assistant"):
                continue
            message = record.get("message")
            text = _text_from_content(message.get("content") if isinstance(message, dict) else None)
            if not text:
                continue
            message_count += 1
            if record["role"] == "user":
                text = prompt_text(text)
                last_prompt = text[:500]
                turns.append(("USER", text))
            else:
                turns.append(("ASSISTANT", text))
    return {
        "last_prompt": last_prompt,
        "message_count": message_count,
        "tail_text": "\n".join(f"{speaker}: {text}" for speaker, text in turns),
    }


def _rewrite_cwd(chat_dir: Path, new_cwd: str) -> None:
    path = chat_dir / "meta.json"
    meta = read_meta(chat_dir)
    meta["cwd"] = normalize(new_cwd)
    mode = stat.S_IMODE(path.stat().st_mode)
    atomic_write_text(path, json.dumps(meta, separators=(",", ":")), mode=mode)


@dataclass
class CursorSource:
    """Cursor's CLI agent keeps each chat twice: its state in
    `<config>/chats/<md5(cwd)>/<chatId>/`, and a JSONL rendering of it in
    `<data>/projects/<slug(cwd)>/agent-transcripts/<chatId>/<chatId>.jsonl`. The
    transcript is the session file; the chat directory is its companion."""

    parser_version: ClassVar[int] = 1
    config_dir: Path = field(default_factory=default_config_dir)
    data_dir: Path = field(default_factory=default_data_dir)
    tail_turns: int = 30
    name: str = "cursor"
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
    _subagents: dict[str, bool] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.config_dir = Path(self.config_dir)
        self.data_dir = Path(self.data_dir)

    @property
    def root(self) -> Path:
        return self.data_dir

    @property
    def store_roots(self) -> tuple[Path, ...]:
        return (self.projects_dir, self.chats_dir)

    @property
    def chats_dir(self) -> Path:
        return self.config_dir / "chats"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    def resume_argv(self, session_id: str) -> tuple[str, ...]:
        return ("cursor-agent", "--resume", session_id)

    def fork_argv(self, session_id: str) -> tuple[str, ...]:
        raise RuntimeError(f"cursor-agent cannot fork a session ({session_id})")

    def launch_argv(self, prompt: str | None = None) -> tuple[str, ...]:
        if prompt is None:
            return ("cursor-agent",)
        return ("cursor-agent", prompt)

    def chats_for(self, cwd: str) -> Path:
        return self.chats_dir / cursor_chat_key(cwd)

    def storage_for(self, cwd: str) -> Path:
        return self.projects_dir / cursor_slug(cwd)

    def storage_dir(self, file_path: str) -> Path:
        # <slug>/agent-transcripts/<chatId>/<chatId>.jsonl
        return Path(file_path).parents[2]

    def stored_transcripts(self, storage: Path) -> list[Path]:
        return sorted(storage.glob("agent-transcripts/*/*.jsonl"))

    def _live(self, file_path: str | Path) -> bool:
        return Path(file_path).is_relative_to(self.projects_dir)

    def chat_dir(self, chat_id: str) -> Path | None:
        return next(iter(sorted(self.chats_dir.glob(f"*/{chat_id}"))), None)

    def transcript_for(self, chat_id: str, cwd: object) -> Path | None:
        if isinstance(cwd, str) and cwd:
            path = self.storage_for(cwd) / "agent-transcripts" / chat_id / f"{chat_id}.jsonl"
            if path.is_file():
                return path
        found = sorted(self.projects_dir.glob(f"*/agent-transcripts/{chat_id}/{chat_id}.jsonl"))
        return found[0] if found else None

    def companion_paths(self, file_path: str) -> tuple[Path, ...]:
        path = Path(file_path)
        if not self._live(path):
            return (path.with_suffix(""),)
        chat = self.chat_dir(path.stem)
        return (chat,) if chat is not None else ()

    def companion_destination(self, companion: Path, file_path: str) -> Path:
        if not self._live(file_path):
            return Path(file_path).with_suffix("")
        try:
            cwd = read_meta(companion).get("cwd")
        except (OSError, ValueError) as e:
            raise RuntimeError(f"cannot tell where {companion} belongs: {e}") from e
        if not isinstance(cwd, str) or not cwd:
            raise RuntimeError(f"cannot tell where {companion} belongs: meta.json has no cwd")
        return self.chats_for(cwd) / companion.name

    def relocate_storage(self, old_cwd: str, new_cwd: str) -> dict[str, str]:
        """Move every chat launched at `old_cwd`: its chats dir, its projects dir, and
        the cwd each chat records. Refuses a projects dir that also holds another
        directory's transcripts, since the slug cannot tell two such paths apart."""
        src_projects, dst_projects = self.storage_for(old_cwd), self.storage_for(new_cwd)
        src_chats, dst_chats = self.chats_for(old_cwd), self.chats_for(new_cwd)
        if not src_projects.is_dir() and not src_chats.is_dir():
            return {}
        if dst_projects.exists() or dst_chats.exists():
            raise RuntimeError(f"{dst_projects} or {dst_chats} already exists")
        ours = {d.name for d in src_chats.iterdir()} if src_chats.is_dir() else set()
        transcripts = self.stored_transcripts(src_projects)
        if strays := [p.parent.name for p in transcripts if p.parent.name not in ours]:
            raise RuntimeError(
                f"{src_projects} also holds sessions launched elsewhere ({', '.join(strays)})"
            )
        if src_projects.is_dir():
            dst_projects.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_projects), str(dst_projects))
        if src_chats.is_dir():
            dst_chats.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_chats), str(dst_chats))
            for chat in sorted(p for p in dst_chats.iterdir() if (p / "meta.json").is_file()):
                _rewrite_cwd(chat, new_cwd)
        return {str(p): str(dst_projects / p.relative_to(src_projects)) for p in transcripts}

    def relocate_session(self, file_path: str, new_cwd: str) -> str:
        src = Path(file_path)
        chat_id = src.stem
        dst_dir = self.storage_for(new_cwd) / "agent-transcripts" / chat_id
        chat = self.chat_dir(chat_id)
        dst_chat = self.chats_for(new_cwd) / chat_id
        if dst_dir.exists() or (chat is not None and chat != dst_chat and dst_chat.exists()):
            raise RuntimeError(f"{dst_dir} or {dst_chat} already exists")
        dst_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src.parent), str(dst_dir))
        if chat is not None and chat != dst_chat:
            dst_chat.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(chat), str(dst_chat))
        if (dst_chat / "meta.json").is_file():
            _rewrite_cwd(dst_chat, new_cwd)
        return str(dst_dir / src.name)

    def _excluded(self, chat: Path) -> bool:
        if chat.name not in self._subagents:
            self._subagents[chat.name] = _is_subagent(chat)
        return self._subagents[chat.name]

    def discover(self) -> Iterator[RawRef]:
        if not self.chats_dir.is_dir():
            return
        seen: set[str] = set()
        for meta_path in sorted(self.chats_dir.glob("*/*/meta.json")):
            chat = meta_path.parent
            try:
                meta = read_meta(chat)
            except (OSError, ValueError) as e:
                print(f"skipped {meta_path}: {e}", file=sys.stderr)
                continue
            if not meta.get("hasConversation") or chat.name in seen or self._excluded(chat):
                continue
            transcript = self.transcript_for(chat.name, meta.get("cwd"))
            if transcript is None:
                continue  # no transcript rendered for this chat yet
            try:
                st = transcript.stat()
            except OSError as e:
                print(f"skipped {transcript}: {e}", file=sys.stderr)
                continue
            seen.add(chat.name)
            yield RawRef(
                source=self.name,
                source_ref=chat.name,
                file=str(transcript),
                mtime=iso_mtime(st),
                size_bytes=st.st_size,
                fingerprint=stat_fingerprint(st),
            )

    def _meta_for(self, chat_id: str) -> dict[str, Any]:
        chat = self.chat_dir(chat_id)
        if chat is None:
            return {}
        try:
            return read_meta(chat)
        except (OSError, ValueError):
            return {}

    def refresh_cached_metadata(self, session: NormalizedSession) -> None:
        title = self._meta_for(session.session_id).get("title")
        session.ai_title = title if isinstance(title, str) and title else session.ai_title

    def parse(self, ref: RawRef) -> NormalizedSession:
        path = ref.file_path()
        chat_id = ref.source_ref or path.stem
        meta = self._meta_for(chat_id)
        fields = _session_fields(path, self.tail_turns)
        title = meta.get("title")
        cwd = meta.get("cwd")
        return NormalizedSession(
            source=self.name,
            session_id=chat_id,
            cwd=cwd if isinstance(cwd, str) and cwd else None,
            started_at=_iso_ms(meta.get("createdAtMs")),
            mtime=ref.mtime,
            size_bytes=ref.size_bytes,
            custom_title=None,
            summary=None,
            last_prompt=fields["last_prompt"],
            file=ref.file,
            ai_title=title if isinstance(title, str) and title else None,
            message_count=fields["message_count"],
            tail_text=fields["tail_text"][:6000],
            git_branch=None,
        )
