from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from .base import NormalizedSession, RawRef, append_jsonl_line, stat_fingerprint


class CodexSessionIndexError(RuntimeError):
    pass


class CodexStateError(RuntimeError):
    pass


def _state_db(codex_home: Path) -> Path | None:
    """Newest `state_<n>.sqlite` in the Codex home. The suffix is a schema
    version Codex bumps when it migrates, so pick the highest rather than
    hardcoding one."""
    best: tuple[int, Path] | None = None
    for p in codex_home.glob("state_*.sqlite"):
        try:
            version = int(p.stem.removeprefix("state_"))
        except ValueError:
            continue
        if best is None or version > best[0]:
            best = (version, p)
    return best[1] if best else None


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


def _thread_id(meta: dict, path: Path) -> str:
    """The rollout's own thread id. `session_id` is the id of the *root* thread,
    so for a subagent rollout it names the parent, not this file — keying on it
    collapses a whole spawn tree onto one id. `id` is always this file's own, and
    matches the UUID in the filename."""
    return meta.get("id") or meta.get("session_id") or path.stem


def _is_interactive(meta: dict) -> bool:
    """Codex writes a rollout per thread: interactive sessions, the subagents they
    spawn, and non-interactive `codex exec` runs alike. Only the first are sessions
    a human owns and can resume, which is also what `codex resume` lists by default.
    `source` is a dict for subagents; rollouts old enough to predate the field are
    all interactive."""
    source = meta.get("source")
    return source is None or source == "cli"


@dataclass
class CodexSource:
    parser_version: ClassVar[int] = 1
    sessions_dir: Path = field(default_factory=default_codex_dir)
    session_index_file: Path | None = None
    tail_turns: int = 30
    name: str = "codex"
    capabilities: frozenset[str] = field(
        default_factory=lambda: frozenset({"applies_names", "hides_from_picker", "shows_in_picker"})
    )
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
                if not _is_interactive(_meta(p)):
                    continue
            except (OSError, json.JSONDecodeError) as e:
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
        meta = _meta(path)
        session_id = _thread_id(meta, path)
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

    def refresh_cached_metadata(self, session: NormalizedSession) -> None:
        session.custom_title = self._thread_names().get(session.session_id)

    def apply_name(self, session_id: str, file_path: str, name: str) -> None:
        path = Path(file_path)
        path.stat()
        if _thread_id(_meta(path), path) != session_id:
            raise ValueError(f"session id mismatch in {file_path}")
        updated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        line = json.dumps(
            {"id": session_id, "thread_name": name.strip(), "updated_at": updated_at},
            ensure_ascii=False,
        )
        append_jsonl_line(self.session_index_file, line)
        self._names_by_id = None

    def hide_from_picker(self, session_id: str, file_path: str) -> bool:
        """Set the thread's `archived` flag in Codex's state DB, which is what
        `codex resume` filters on. Codex's own `archive` subcommand refuses once
        the rollout file is gone, so this has to run before we move or delete it."""
        db = _state_db(Path(self.sessions_dir).parent)
        if db is None:
            return False
        try:
            con = sqlite3.connect(db, timeout=5.0)
        except sqlite3.Error as e:
            raise CodexStateError(f"cannot open Codex state db {db}: {e}") from e
        try:
            columns = {row[1] for row in con.execute("PRAGMA table_info(threads)")}
            if not {"archived", "archived_at", "rollout_path"} <= columns:
                raise CodexStateError(
                    f"{db} has no archivable threads table — Codex's schema moved, "
                    "so sessions cannot be cleared from its resume picker"
                )
            # rollout_path pins the update to this exact transcript: a stale or
            # mis-derived id then archives nothing rather than some other thread.
            cur = con.execute(
                "UPDATE threads SET archived = 1, archived_at = ? "
                "WHERE id = ? AND rollout_path = ? AND archived = 0",
                (int(datetime.now(UTC).timestamp()), session_id, str(file_path)),
            )
            con.commit()
            return cur.rowcount > 0
        except sqlite3.Error as e:
            raise CodexStateError(f"cannot update Codex state db {db}: {e}") from e
        finally:
            con.close()

    def show_in_picker(self, session_id: str, file_path: str) -> bool:
        """Undo `hide_from_picker`, putting the thread back in `codex resume`.

        Ordering is the reverse of hiding: the guard matches on `rollout_path`, which
        hiding never rewrote, so the transcript must already be restored to its
        original location before this runs. Returns False when nothing matched — a
        thread Codex itself archived (it rewrites rollout_path) or a row that is gone."""
        db = _state_db(Path(self.sessions_dir).parent)
        if db is None:
            return False
        try:
            con = sqlite3.connect(db, timeout=5.0)
        except sqlite3.Error as e:
            raise CodexStateError(f"cannot open Codex state db {db}: {e}") from e
        try:
            columns = {row[1] for row in con.execute("PRAGMA table_info(threads)")}
            if not {"archived", "archived_at", "rollout_path"} <= columns:
                raise CodexStateError(
                    f"{db} has no archivable threads table — Codex's schema moved, "
                    "so sessions cannot be restored to its resume picker"
                )
            cur = con.execute(
                "UPDATE threads SET archived = 0, archived_at = NULL "
                "WHERE id = ? AND rollout_path = ? AND archived = 1",
                (session_id, str(file_path)),
            )
            con.commit()
            return cur.rowcount > 0
        except sqlite3.Error as e:
            raise CodexStateError(f"cannot update Codex state db {db}: {e}") from e
        finally:
            con.close()
