from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, NamedTuple, cast

from ..compressed import (
    is_transcript,
    is_zstd,
    moved_form,
    open_text,
    other_form,
    prefer_plain,
    uncompressed_path,
)
from .base import (
    FingerprintMemo,
    IndexUpdate,
    NormalizedSession,
    RawRef,
    append_jsonl_line,
    changed_since,
    in_workspace,
    iso_mtime,
    iso_timestamp,
    memo_by_fingerprint,
    stat_fingerprint,
)

_ARCHIVE_COLUMNS = frozenset({"archived", "archived_at", "rollout_path"})
_REBIND_COLUMNS = frozenset({"cwd", "rollout_path"})
# `codex resume` lists both by default; a TUI attached to the app-server records `vscode`.
_INTERACTIVE_SOURCES = frozenset({"cli", "vscode"})


class CodexSessionIndexError(RuntimeError):
    pass


class CodexStateError(RuntimeError):
    pass


type _Header = tuple[str, bool, str | None]


class _Located(NamedTuple):
    path: Path
    stat: os.stat_result
    entry: tuple[str, _Header]


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
    return (
        Path(env)
        if env
        else Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    )


def rollout_files(root: Path) -> list[Path]:
    """Every rollout under `root`, plain or compressed in place to `.jsonl.zst`."""
    return prefer_plain(p for p in root.glob("**/*.jsonl*") if is_transcript(p))


def _forms(path: Path) -> Iterator[Path]:
    """The path, then its other form: built only if the first is gone."""
    yield path
    if (alternate := other_form(path)) is not None:
        yield alternate


def _rollout_path_forms(file_path: str | Path) -> tuple[str, str]:
    """Codex's state db keeps naming the `.jsonl` path after compressing the rollout."""
    return str(file_path), str(uncompressed_path(file_path))


def is_preamble(text: str) -> bool:
    t = text.lstrip()
    return t.startswith("# AGENTS.md") or "<INSTRUCTIONS>" in text


def _message_stats(path: Path) -> tuple[str | None, str | None, int]:
    """Return (first user prompt, last user prompt, message_count).

    message_count is non-preamble user prompts + assistant messages, so it is
    comparable to Claude's turn count (which counts both roles) rather than
    only counting the human side of the conversation.
    """
    first: str | None = None
    last: str | None = None
    message_count = 0
    first_seen = False
    with open_text(path) as fh:
        for line in fh:
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
                message_count += 1
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
                if is_preamble(text):
                    continue  # drop only the injected first-message preamble
            message_count += 1
            first = first if first is not None else text
            last = text
    return first, last, message_count


def _meta(path: Path) -> dict[str, Any]:
    with open_text(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            o = json.loads(line)
            if o.get("type") == "session_meta":
                return cast(dict[str, Any], o.get("payload", {}))
            return {}
    return {}


def _thread_id(meta: dict[str, Any], path: Path) -> str:
    """The rollout's own thread id. `session_id` is the id of the *root* thread,
    so for a subagent rollout it names the parent, not this file — keying on it
    collapses a whole spawn tree onto one id. `id` is always this file's own, and
    matches the UUID in the filename."""
    return meta.get("id") or meta.get("session_id") or uncompressed_path(path).stem


def _is_interactive(meta: dict[str, Any]) -> bool:
    """Codex writes a rollout per thread: interactive sessions, the subagents they
    spawn, and non-interactive `codex exec` runs alike. Only the first are sessions
    a human owns and can resume, which is also what `codex resume` lists by default.
    `source` is a dict for subagents; rollouts old enough to predate the field are
    all interactive."""
    source = meta.get("source")
    return source is None or (isinstance(source, str) and source in _INTERACTIVE_SOURCES)


def _header(path: Path) -> _Header:
    meta = _meta(path)
    return _thread_id(meta, path), _is_interactive(meta), meta.get("cwd")


@dataclass
class CodexSource:
    parser_version: ClassVar[int] = 2
    sessions_dir: Path = field(default_factory=default_codex_dir)
    session_index_file: Path | None = None
    tail_turns: int = 30
    name: str = "codex"
    capabilities: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "applies_names",
                "compresses_in_place",
                "discovers_scoped",
                "seeds_discovery",
                "filesystem_lifecycle",
                "hides_from_picker",
                "shows_in_picker",
                "rebinds_workspace",
                "launches_sessions",
                "forks_sessions",
            }
        )
    )
    _names_by_id: dict[str, str | None] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _headers: FingerprintMemo[_Header] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self.sessions_dir = Path(self.sessions_dir)
        if self.session_index_file is None:
            self.session_index_file = self.sessions_dir.parent / "session_index.jsonl"
        else:
            self.session_index_file = Path(self.session_index_file)

    @property
    def root(self) -> Path:
        return self.sessions_dir

    def resume_argv(self, session_id: str) -> tuple[str, ...]:
        return ("codex", "resume", session_id)

    def fork_argv(self, session_id: str) -> tuple[str, ...]:
        return ("codex", "fork", session_id)

    def launch_argv(self, prompt: str | None = None) -> tuple[str, ...]:
        if prompt is None:
            return ("codex",)
        return ("codex", prompt)

    def _session_index_path(self) -> Path:
        if self.session_index_file is None:
            raise RuntimeError("Codex session index is not configured")
        return self.session_index_file

    def discover(self) -> Iterator[RawRef]:
        return self._refs(lambda _: True, lambda _p, _cwd: True)

    def discover_scoped(
        self, cwd: str | None, *, recursive: bool, since: datetime, known: frozenset[str]
    ) -> Iterator[RawRef]:
        def newly_compressed(p: Path) -> bool:
            return is_zstd(p) and str(uncompressed_path(p)) in known

        def in_scope(p: Path, session_cwd: str | None) -> bool:
            return (
                str(p) in known
                or newly_compressed(p)
                or in_workspace(session_cwd, cwd, recursive=recursive)
            )

        # rollouts are filed by date: mtime first, since it skips the oversized meta read.
        # Compressing keeps the old mtime, so a registered rollout's new `.zst` is let through.
        return self._refs(lambda p: changed_since(p, since) or newly_compressed(p), in_scope)

    def seed_discovery(self, sessions: Iterable[NormalizedSession]) -> None:
        self._headers = {
            **{
                session.file: (
                    session.file_fingerprint,
                    (session.session_id, True, session.cwd),
                )
                for session in sessions
                if session.file is not None and session.file_fingerprint is not None
            },
            **self._headers,
        }

    def _refs(
        self,
        wanted: Callable[[Path], bool],
        wanted_meta: Callable[[Path, str | None], bool],
    ) -> Iterator[RawRef]:
        self._names_by_id = None
        if not self.sessions_dir.exists():
            return
        headers: FingerprintMemo[_Header] = {}
        for p in rollout_files(self.sessions_dir):
            if not wanted(p):
                if str(p) in self._headers:
                    headers[str(p)] = self._headers[str(p)]
                continue
            located = self._located(p)
            if isinstance(located, str):
                print(f"skipped {p}: {located}", file=sys.stderr)
                continue
            path, st, (fingerprint, header) = located
            headers[str(path)] = (fingerprint, header)
            thread_id, interactive, session_cwd = header
            if not interactive or not wanted_meta(path, session_cwd):
                continue
            yield RawRef(
                source=self.name,
                source_ref=thread_id,
                file=str(path),
                mtime=iso_mtime(st),
                size_bytes=st.st_size,
                fingerprint=fingerprint,
            )
        self._headers = headers

    def _located(self, p: Path) -> _Located | str:
        """The rollout and its header, or why it was skipped. Codex compresses and
        decompresses rollouts in place, so one that vanished mid-scan is looked for
        under its other name."""
        reason = "no such file"
        for candidate in _forms(p):
            try:
                st = candidate.stat()
                entry = memo_by_fingerprint(self._headers, candidate, stat_fingerprint(st), _header)
            except FileNotFoundError as e:
                reason = str(e)
                continue
            except (OSError, json.JSONDecodeError) as e:
                return str(e)
            return _Located(candidate, st, entry)
        return reason

    def parse(self, ref: RawRef) -> NormalizedSession:
        path = ref.file_path()
        meta = _meta(path)
        session_id = _thread_id(meta, path)
        _first, last, count = _message_stats(path)
        tail = last or ""
        return NormalizedSession(
            source=self.name,
            session_id=session_id,
            cwd=meta.get("cwd"),
            started_at=iso_timestamp(meta.get("timestamp")),
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
            lines = self._session_index_path().read_text(encoding="utf-8").splitlines()
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
        path = moved_form(file_path) or Path(file_path)
        path.stat()
        if _thread_id(_meta(path), path) != session_id:
            raise ValueError(f"session id mismatch in {file_path}")
        updated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        line = json.dumps(
            {"id": session_id, "thread_name": name.strip(), "updated_at": updated_at},
            ensure_ascii=False,
        )
        append_jsonl_line(self._session_index_path(), line)
        self._names_by_id = None

    def _update_thread(  # noqa: PLR0913 -- the guarded statement and its bindings
        self,
        sql: str,
        before: tuple[Any, ...],
        *,
        session_id: str,
        file_path: str,
        after: tuple[Any, ...] = (),
        required: frozenset[str],
        note: str,
    ) -> IndexUpdate:
        """Run one guarded UPDATE against Codex's state db. Every caller pins its
        WHERE on `rollout_path` as well as the id, so a stale or mis-derived id
        touches nothing rather than some other thread. When the UPDATE matches no
        row, the guard re-queries for the thread itself, which is what separates a
        session Codex has no record of from one already in the desired state.
        `sql` binds `before`, then the id and both rollout path forms, then `after`."""
        key = (session_id, *_rollout_path_forms(file_path))
        db = _state_db(Path(self.sessions_dir).parent)
        if db is None:
            return IndexUpdate.NO_INDEX
        try:
            con = sqlite3.connect(db, timeout=5.0)
        except sqlite3.Error as e:
            raise CodexStateError(f"cannot open Codex state db {db}: {e}") from e
        try:
            columns = {row[1] for row in con.execute("PRAGMA table_info(threads)")}
            if not required <= columns:
                raise CodexStateError(f"{db} {note}")
            cur = con.execute(sql, (*before, *key, *after))
            con.commit()
            if cur.rowcount > 0:
                return IndexUpdate.UPDATED
            row = con.execute(
                "SELECT 1 FROM threads WHERE id = ? AND rollout_path IN (?, ?)", key
            ).fetchone()
            return IndexUpdate.ALREADY_CURRENT if row else IndexUpdate.NO_ENTRY
        except sqlite3.Error as e:
            raise CodexStateError(f"cannot update Codex state db {db}: {e}") from e
        finally:
            con.close()

    def hide_from_picker(self, session_id: str, file_path: str) -> IndexUpdate:
        """Set the thread's `archived` flag in Codex's state DB, which is what
        `codex resume` filters on. Codex's own `archive` subcommand refuses once
        the rollout file is gone, so this has to run before we move or delete it."""
        return self._update_thread(
            "UPDATE threads SET archived = 1, archived_at = ? "
            "WHERE id = ? AND rollout_path IN (?, ?) AND archived = 0",
            (int(datetime.now(UTC).timestamp()),),
            session_id=session_id,
            file_path=file_path,
            required=_ARCHIVE_COLUMNS,
            note="has no archivable threads table — Codex's schema moved, so sessions "
            "cannot be cleared from its resume picker",
        )

    def show_in_picker(self, session_id: str, file_path: str) -> IndexUpdate:
        """Undo `hide_from_picker`, putting the thread back in `codex resume`.

        Ordering is the reverse of hiding: the guard matches on `rollout_path`, which
        hiding never rewrote, so the transcript must already be restored to its
        original location before this runs. A thread Codex itself archived has had its
        `rollout_path` rewritten, so it reports `NO_ENTRY`."""
        return self._update_thread(
            "UPDATE threads SET archived = 0, archived_at = NULL "
            "WHERE id = ? AND rollout_path IN (?, ?) AND archived = 1",
            (),
            session_id=session_id,
            file_path=file_path,
            required=_ARCHIVE_COLUMNS,
            note="has no archivable threads table — Codex's schema moved, so sessions "
            "cannot be restored to its resume picker",
        )

    def rebind_workspace(self, session_id: str, file_path: str, new_cwd: str) -> IndexUpdate:
        """Point the thread at the workspace's new location. Codex files rollouts by
        date rather than by cwd, so nothing moves on disk — but `codex resume` filters
        the picker on this column, and the cwd baked into the rollout file at launch is
        never revisited. Left alone, the session stays resumable only by its id.

        `IS NOT` rather than `!=` so a NULL cwd is rebound rather than read as unchanged."""
        return self._update_thread(
            "UPDATE threads SET cwd = ? WHERE id = ? AND rollout_path IN (?, ?) AND cwd IS NOT ?",
            (str(new_cwd),),
            session_id=session_id,
            file_path=file_path,
            after=(str(new_cwd),),
            required=_REBIND_COLUMNS,
            note="has no threads.cwd — Codex's schema moved, so sessions cannot follow "
            "a workspace to its new path",
        )
