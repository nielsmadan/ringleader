from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from .. import paths
from . import episodes as episodes_module
from . import facts as facts_module
from . import health, signals, taxonomy
from .episodes import Episode
from .facts import (
    MAIN_THREAD,
    Agent,
    CompactionFact,
    RequestFact,
    RewindFact,
    ToolCallFact,
    TranscriptFacts,
    TranscriptRef,
    UserMessageFact,
)
from .health import ThreadStats
from .signals import SignalHit

FACTS_VERSION: int = 8
DERIVE_VERSION: int = 6
LABELS_SCHEMA_VERSION: int = 1
BUSY_TIMEOUT_SECONDS = 30.0
INDEX_FILE = re.compile(r"^index-v(\d+)\.sqlite(?:-wal|-shm)?$")

FACTS_DDL: tuple[str, ...] = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    (
        "CREATE TABLE transcripts (path TEXT PRIMARY KEY, sort_key TEXT NOT NULL, root TEXT NOT NULL,"
        " agent TEXT NOT NULL, session_id TEXT NOT NULL, thread_id TEXT NOT NULL,"
        " thread_key TEXT NOT NULL, parent_thread_id TEXT, parent_tool_use_id TEXT,"
        " subagent_type TEXT, subagent_description TEXT, archived INTEGER NOT NULL,"
        " fingerprint TEXT NOT NULL, cwd TEXT, git_branch TEXT, first_ts TEXT, last_ts TEXT,"
        " final_text TEXT, last_event TEXT NOT NULL, api_errors INTEGER NOT NULL)"
    ),
    (
        "CREATE TABLE failures (path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,"
        " facts_version INTEGER NOT NULL, error TEXT NOT NULL, failed_at TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE requests (path TEXT NOT NULL REFERENCES transcripts(path) ON DELETE CASCADE,"
        " line INTEGER NOT NULL, dedupe_key TEXT NOT NULL, ts TEXT NOT NULL, day TEXT NOT NULL,"
        " model TEXT, input INTEGER NOT NULL, output INTEGER NOT NULL, cache_read INTEGER NOT NULL,"
        " cache_write INTEGER NOT NULL, cache_write_5m INTEGER NOT NULL,"
        " cache_write_1h INTEGER NOT NULL, reasoning INTEGER NOT NULL,"
        " context_tokens INTEGER NOT NULL, context_window INTEGER, gap_seconds REAL,"
        " rebuild INTEGER NOT NULL, on_main_branch INTEGER NOT NULL, skill TEXT, plugin TEXT,"
        " PRIMARY KEY (path, dedupe_key))"
    ),
    (
        "CREATE TABLE tool_calls (path TEXT NOT NULL REFERENCES transcripts(path) ON DELETE CASCADE,"
        " seq INTEGER NOT NULL, line INTEGER NOT NULL, call_id TEXT NOT NULL,"
        " dedupe_key TEXT NOT NULL, ts TEXT NOT NULL, day TEXT NOT NULL, tool TEXT NOT NULL,"
        " kind TEXT NOT NULL, mcp_server TEXT, mcp_tool TEXT, skill TEXT, command TEXT,"
        " command_class TEXT NOT NULL, file_path TEXT, input_hash TEXT NOT NULL,"
        " input_preview TEXT NOT NULL, edit_old_hash TEXT, edit_new_hash TEXT, is_error INTEGER,"
        " status TEXT NOT NULL, error_category TEXT, error_text TEXT, signature TEXT,"
        " on_main_branch INTEGER NOT NULL, read_offset INTEGER, read_limit INTEGER,"
        " PRIMARY KEY (path, seq))"
    ),
    (
        "CREATE TABLE user_messages (path TEXT NOT NULL REFERENCES transcripts(path)"
        " ON DELETE CASCADE, line INTEGER NOT NULL, before_call INTEGER NOT NULL,"
        " message_id TEXT NOT NULL, dedupe_key TEXT NOT NULL, ts TEXT NOT NULL, origin TEXT NOT NULL,"
        " text TEXT NOT NULL, is_interrupt INTEGER NOT NULL, slash_command TEXT, rule_kind TEXT,"
        " content_hash TEXT NOT NULL, on_main_branch INTEGER NOT NULL, PRIMARY KEY (path, line))"
    ),
    (
        "CREATE TABLE compactions (path TEXT NOT NULL REFERENCES transcripts(path) ON DELETE CASCADE,"
        " line INTEGER NOT NULL, before_call INTEGER NOT NULL, ts TEXT NOT NULL,"
        " trigger TEXT NOT NULL, pre_tokens INTEGER, post_tokens INTEGER, PRIMARY KEY (path, line))"
    ),
    (
        "CREATE TABLE rewinds (path TEXT NOT NULL REFERENCES transcripts(path) ON DELETE CASCADE,"
        " line INTEGER NOT NULL, ts TEXT NOT NULL, branch_uuid TEXT NOT NULL,"
        " before_call INTEGER NOT NULL, abandoned_calls INTEGER NOT NULL, PRIMARY KEY (path, line))"
    ),
)

FACTS_INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX transcripts_session ON transcripts(agent, session_id)",
    "CREATE INDEX transcripts_thread ON transcripts(agent, thread_id)",
    "CREATE INDEX requests_dedupe ON requests(dedupe_key)",
    "CREATE INDEX requests_day ON requests(day)",
    "CREATE INDEX tool_calls_dedupe ON tool_calls(dedupe_key)",
    "CREATE INDEX tool_calls_day ON tool_calls(day)",
    "CREATE INDEX tool_calls_signature ON tool_calls(signature) WHERE signature IS NOT NULL",
    "CREATE INDEX user_messages_dedupe ON user_messages(dedupe_key)",
)

DERIVED_DDL: tuple[str, ...] = (
    (
        "CREATE TABLE signals (path TEXT NOT NULL REFERENCES transcripts(path) ON DELETE CASCADE,"
        " kind TEXT NOT NULL, start_seq INTEGER NOT NULL, end_seq INTEGER NOT NULL,"
        " detail TEXT NOT NULL, PRIMARY KEY (path, kind, start_seq))"
    ),
    (
        "CREATE TABLE episodes (episode_id TEXT PRIMARY KEY, anchor TEXT NOT NULL,"
        " path TEXT NOT NULL REFERENCES transcripts(path) ON DELETE CASCADE, agent TEXT NOT NULL,"
        " session_id TEXT NOT NULL, thread_id TEXT NOT NULL, thread_key TEXT NOT NULL,"
        " kinds TEXT NOT NULL, start_seq INTEGER NOT NULL, end_seq INTEGER NOT NULL,"
        " start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, start_ts TEXT NOT NULL,"
        " end_ts TEXT NOT NULL, day TEXT NOT NULL, calls INTEGER NOT NULL, severity INTEGER NOT NULL,"
        " tokens INTEGER NOT NULL, content_hash TEXT NOT NULL, excerpt TEXT NOT NULL)"
    ),
    "CREATE INDEX episodes_session ON episodes(agent, session_id)",
    "CREATE INDEX episodes_path ON episodes(path)",
    (
        "CREATE TABLE thread_stats (path TEXT PRIMARY KEY REFERENCES transcripts(path)"
        " ON DELETE CASCADE, calls INTEGER NOT NULL, live_calls INTEGER NOT NULL,"
        " errors INTEGER NOT NULL, writes INTEGER NOT NULL, episode_tokens INTEGER NOT NULL)"
    ),
)
DERIVED_TABLES = ("signals", "episodes", "thread_stats")

LABELS_DDL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    (
        "CREATE TABLE IF NOT EXISTS episode_labels (episode_id TEXT NOT NULL,"
        " content_hash TEXT NOT NULL, prompt_version INTEGER NOT NULL, category TEXT NOT NULL,"
        " friction TEXT NOT NULL, cause TEXT NOT NULL, engine TEXT NOT NULL, model TEXT,"
        " labelled_at TEXT NOT NULL, PRIMARY KEY (episode_id, content_hash, prompt_version))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS message_labels (message_key TEXT NOT NULL,"
        " content_hash TEXT NOT NULL, prompt_version INTEGER NOT NULL, kind TEXT NOT NULL,"
        " engine TEXT NOT NULL, model TEXT, labelled_at TEXT NOT NULL,"
        " PRIMARY KEY (message_key, content_hash, prompt_version))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS issue_names (issue_id TEXT NOT NULL, content_hash TEXT NOT NULL,"
        " prompt_version INTEGER NOT NULL, name TEXT NOT NULL, summary TEXT NOT NULL,"
        " engine TEXT NOT NULL, model TEXT, named_at TEXT NOT NULL,"
        " PRIMARY KEY (issue_id, content_hash, prompt_version))"
    ),
)

FACTS_INDEX_NAMES = tuple(ddl.split()[2] for ddl in FACTS_INDEX_DDL)


class LabelsTooNew(RuntimeError):
    pass


type UnavailableReason = Literal["missing", "mismatch", "unreadable", "unwritable"]
EXIT_NO_INDEX = 3
_NO_INDEX_REASONS: frozenset[UnavailableReason] = frozenset({"missing", "mismatch"})


class IndexUnavailable(RuntimeError):
    def __init__(self, message: str, *, reason: UnavailableReason) -> None:
        super().__init__(message)
        self.reason: UnavailableReason = reason
        self.exit_code = EXIT_NO_INDEX if reason in _NO_INDEX_REASONS else 1


def _sha256(material: object) -> str:
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def facts_fingerprint() -> str:
    caps = {
        "main_thread": facts_module.MAIN_THREAD,
        "command": facts_module.COMMAND_CHARS,
        "error_head": facts_module.ERROR_HEAD_CHARS,
        "error_tail": facts_module.ERROR_TAIL_CHARS,
        "final_text": facts_module.FINAL_TEXT_CHARS,
        "message": facts_module.MESSAGE_CHARS,
        "rebuild_read_ratio": facts_module.REBUILD_READ_RATIO,
    }
    return _sha256(
        {
            "facts_ddl": FACTS_DDL,
            "facts_index_ddl": FACTS_INDEX_DDL,
            "taxonomy": taxonomy.fingerprint_material(),
            "caps": caps,
        }
    )


def derive_fingerprint() -> str:
    return _sha256(
        {
            "derived_ddl": DERIVED_DDL,
            "signals": signals.fingerprint_material(),
            "episodes": episodes_module.fingerprint_material(),
            "health": health.fingerprint_material(),
        }
    )


def labels_fingerprint() -> str:
    return _sha256({"labels_ddl": LABELS_DDL})


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(
            path.absolute().as_uri() + "?mode=ro",
            uri=True,
            timeout=BUSY_TIMEOUT_SECONDS,
            isolation_level=None,
        )
    else:
        conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    try:
        if read_only:
            conn.execute("PRAGMA query_only = ON")
        else:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
    except BaseException:
        conn.close()
        raise
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def index_write_lock(lock_path: Path, *, wait: bool) -> Iterator[bool]:
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _has(conn: sqlite3.Connection, kind: str, name: str) -> bool:
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?", (kind, name)
    ).fetchone()
    return found is not None


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    if not _has(conn, "table", "meta"):
        return {}
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value is not None and value.isdigit() else None


def index_versions(conn: sqlite3.Connection) -> tuple[int | None, int | None]:
    meta = _meta(conn)
    return _int_or_none(meta.get("facts_version")), _int_or_none(meta.get("derive_version"))


def is_complete(conn: sqlite3.Connection) -> bool:
    return "complete_at" in _meta(conn)


def is_cold(conn: sqlite3.Connection) -> bool:
    return not all(_has(conn, "index", name) for name in FACTS_INDEX_NAMES)


def prune_old_versions(index_dir: Path, current: int) -> tuple[Path, ...]:
    if not index_dir.is_dir():
        return ()
    old = tuple(
        sorted(
            p
            for p in index_dir.iterdir()
            if (m := INDEX_FILE.match(p.name)) is not None and int(m.group(1)) < current
        )
    )
    for p in old:
        p.unlink(missing_ok=True)
    return old


def _create(conn: sqlite3.Connection, now_iso: str) -> None:
    with transaction(conn):
        for ddl in (*FACTS_DDL, *DERIVED_DDL):
            conn.execute(ddl)
        _set_meta(conn, "facts_version", str(FACTS_VERSION))
        _set_meta(conn, "derive_version", str(DERIVE_VERSION))
        _set_meta(conn, "created_at", now_iso)


def open_index_for_write(index_dir: Path, now_iso: str) -> tuple[sqlite3.Connection, bool]:
    prune_old_versions(index_dir, FACTS_VERSION)
    path = paths.analysis_index_path(FACTS_VERSION, paths.ensure_analysis_dir(index_dir))
    os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    conn = connect(path)
    try:
        if not _has(conn, "table", "meta"):
            _create(conn, now_iso)
        return conn, is_cold(conn)
    except BaseException:
        conn.close()
        raise


def open_labels(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    conn = connect(path)
    try:
        found = labels_version(conn)
        if found is not None and found > LABELS_SCHEMA_VERSION:
            raise LabelsTooNew(
                f"{path} was written by a newer rl (labels schema {found}); upgrade rl to label"
            )
        with transaction(conn):
            for ddl in LABELS_DDL:
                conn.execute(ddl)
            _set_meta(conn, "schema_version", str(LABELS_SCHEMA_VERSION))
        return conn
    except BaseException:
        conn.close()
        raise


def labels_version(conn: sqlite3.Connection) -> int | None:
    return _int_or_none(_meta(conn).get("schema_version"))


def sort_key(path: Path) -> str:
    return str(path)


def fingerprints(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["path"]: r["fingerprint"]
        for r in conn.execute("SELECT path, fingerprint FROM transcripts")
    }


def stored_roots(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["path"]: r["root"] for r in conn.execute("SELECT path, root FROM transcripts")}


def known_agents(conn: sqlite3.Connection) -> dict[str, tuple[Agent, str]]:
    return {
        r["path"]: (r["agent"], r["fingerprint"])
        for r in conn.execute("SELECT path, agent, fingerprint FROM transcripts")
    }


def failed_paths(conn: sqlite3.Connection) -> dict[str, tuple[str, int]]:
    rows = conn.execute("SELECT path, fingerprint, facts_version FROM failures")
    return {r["path"]: (r["fingerprint"], r["facts_version"]) for r in rows}


def counts(conn: sqlite3.Connection) -> tuple[int, int]:
    transcripts = conn.execute("SELECT count(*) FROM transcripts").fetchone()[0]
    failures = conn.execute("SELECT count(*) FROM failures").fetchone()[0]
    return int(transcripts), int(failures)


def _day(ts: str) -> str:
    return ts[:10]


def _transcript_row(facts: TranscriptFacts) -> tuple[Any, ...]:
    ref = facts.ref
    return (
        str(ref.path),
        sort_key(ref.path),
        ref.root,
        ref.agent,
        facts.session_id,
        facts.thread_id,
        facts.thread_key,
        facts.parent_thread_id,
        facts.parent_tool_use_id,
        facts.subagent_type,
        facts.subagent_description,
        int(ref.archived),
        ref.fingerprint,
        facts.cwd,
        facts.git_branch,
        facts.first_ts,
        facts.last_ts,
        facts.final_text,
        facts.last_event,
        facts.api_errors,
    )


def _request_row(path: str, r: RequestFact) -> tuple[Any, ...]:
    return (
        path,
        r.line,
        r.dedupe_key,
        r.timestamp,
        _day(r.timestamp),
        r.model,
        r.input,
        r.output,
        r.cache_read,
        r.cache_write,
        r.cache_write_5m,
        r.cache_write_1h,
        r.reasoning,
        r.context_tokens,
        r.context_window,
        r.gap_seconds,
        int(r.rebuild),
        int(r.on_main_branch),
        r.skill,
        r.plugin,
    )


def _call_row(path: str, c: ToolCallFact) -> tuple[Any, ...]:
    return (
        path,
        c.seq,
        c.line,
        c.call_id,
        c.dedupe_key,
        c.timestamp,
        _day(c.timestamp),
        c.tool,
        c.kind,
        c.mcp_server,
        c.mcp_tool,
        c.skill,
        c.command,
        c.command_class,
        c.file_path,
        c.input_hash,
        c.input_preview,
        c.edit_old_hash,
        c.edit_new_hash,
        None if c.is_error is None else int(c.is_error),
        c.status,
        c.error_category,
        c.error_text,
        c.signature,
        int(c.on_main_branch),
        c.read_offset,
        c.read_limit,
    )


def _message_row(path: str, m: UserMessageFact) -> tuple[Any, ...]:
    return (
        path,
        m.line,
        m.before_call,
        m.message_id,
        m.dedupe_key,
        m.timestamp,
        m.origin,
        m.text,
        int(m.is_interrupt),
        m.slash_command,
        m.rule_kind,
        m.content_hash,
        int(m.on_main_branch),
    )


def _episode_row(e: Episode) -> tuple[Any, ...]:
    return (
        e.episode_id,
        e.anchor,
        e.path,
        e.agent,
        e.session_id,
        e.thread_id,
        e.thread_key,
        ",".join(e.kinds),
        e.start_seq,
        e.end_seq,
        e.start_line,
        e.end_line,
        e.start_ts,
        e.end_ts,
        _day(e.start_ts),
        e.calls,
        e.severity,
        e.tokens,
        e.content_hash,
        e.excerpt,
    )


_EPISODE_COLUMNS = (
    "anchor",
    "path",
    "agent",
    "session_id",
    "thread_id",
    "thread_key",
    "kinds",
    "start_seq",
    "end_seq",
    "start_line",
    "end_line",
    "start_ts",
    "end_ts",
    "day",
    "calls",
    "severity",
    "tokens",
    "content_hash",
    "excerpt",
)
_EPISODE_UPSERT = (
    " ON CONFLICT(episode_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _EPISODE_COLUMNS)
    + " WHERE excluded.path < episodes.path"
)


def _placeholders(n: int) -> str:
    return ", ".join("?" * n)


def _insert_derived(
    conn: sqlite3.Connection,
    path: str,
    hits: Sequence[SignalHit],
    episodes: Sequence[Episode],
    stats: ThreadStats,
) -> None:
    conn.executemany(
        "INSERT INTO signals VALUES (?, ?, ?, ?, ?)",
        [(path, h.kind, h.start_seq, h.end_seq, h.detail) for h in hits],
    )
    conn.executemany(
        f"INSERT INTO episodes VALUES ({_placeholders(20)}){_EPISODE_UPSERT}",
        [_episode_row(e) for e in episodes],
    )
    conn.execute(
        "INSERT INTO thread_stats VALUES (?, ?, ?, ?, ?, ?)",
        (path, stats.calls, stats.live_calls, stats.errors, stats.writes, stats.episode_tokens),
    )


def replace_transcript(
    conn: sqlite3.Connection,
    facts: TranscriptFacts,
    hits: Sequence[SignalHit],
    episodes: Sequence[Episode],
    stats: ThreadStats,
) -> None:
    path = str(facts.ref.path)
    prior = conn.execute("SELECT session_id FROM transcripts WHERE path = ?", (path,)).fetchone()
    conn.execute("DELETE FROM transcripts WHERE path = ?", (path,))
    conn.execute("DELETE FROM failures WHERE path = ?", (path,))
    conn.execute(f"INSERT INTO transcripts VALUES ({_placeholders(20)})", _transcript_row(facts))
    conn.executemany(
        f"INSERT INTO requests VALUES ({_placeholders(20)})",
        [_request_row(path, r) for r in facts.requests],
    )
    conn.executemany(
        f"INSERT INTO tool_calls VALUES ({_placeholders(27)})",
        [_call_row(path, c) for c in facts.tool_calls],
    )
    conn.executemany(
        f"INSERT INTO user_messages VALUES ({_placeholders(13)})",
        [_message_row(path, m) for m in facts.user_messages],
    )
    conn.executemany(
        "INSERT INTO compactions VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (path, c.line, c.before_call, c.timestamp, c.trigger, c.pre_tokens, c.post_tokens)
            for c in facts.compactions
        ],
    )
    conn.executemany(
        "INSERT INTO rewinds VALUES (?, ?, ?, ?, ?, ?)",
        [
            (path, r.line, r.timestamp, r.branch_uuid, r.before_call, r.abandoned_calls)
            for r in facts.rewinds
        ],
    )
    _insert_derived(conn, path, hits, episodes, stats)
    _keep_rolled_up(conn, facts, prior[0] if prior else None)


def _keep_rolled_up(conn: sqlite3.Connection, facts: TranscriptFacts, prior: str | None) -> None:
    path = str(facts.ref.path)
    if (
        facts.ref.agent != "codex"
        or facts.thread_id == MAIN_THREAD
        or prior is None
        or prior == facts.session_id
    ):
        return
    parent_known = conn.execute(
        "SELECT 1 FROM transcripts WHERE agent = 'codex' AND thread_key = ? AND path != ?",
        (facts.session_id, path),
    ).fetchone()
    if parent_known is None:
        conn.execute("UPDATE transcripts SET session_id = ? WHERE path = ?", (prior, path))
        conn.execute("UPDATE episodes SET session_id = ? WHERE path = ?", (prior, path))


def record_failure(conn: sqlite3.Connection, ref: TranscriptRef, error: str, now_iso: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO failures VALUES (?, ?, ?, ?, ?)",
        (str(ref.path), ref.fingerprint, FACTS_VERSION, error, now_iso),
    )


def drop_transcripts(conn: sqlite3.Connection, paths: Iterable[str]) -> None:
    keys = [(p,) for p in paths]
    conn.executemany("DELETE FROM transcripts WHERE path = ?", keys)
    conn.executemany("DELETE FROM failures WHERE path = ?", keys)


def _root_of(session_id: str, parents: dict[str, str]) -> str:
    seen = {session_id}
    while (parent := parents.get(session_id)) is not None and parent not in seen:
        seen.add(parent)
        session_id = parent
    return session_id


def rooted_sessions(rows: Iterable[tuple[str, str, str]]) -> dict[str, str]:
    listed = list(rows)
    parents = {thread: session for _, session, thread in listed if thread != MAIN_THREAD}
    return {
        path: root for path, session, _ in listed if (root := _root_of(session, parents)) != session
    }


def roll_up_codex_sessions(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT path, session_id, thread_id FROM transcripts WHERE agent = 'codex'"
    ).fetchall()
    moved = [(root, path) for path, root in rooted_sessions(tuple(r) for r in rows).items()]
    conn.executemany("UPDATE transcripts SET session_id = ? WHERE path = ?", moved)
    conn.executemany("UPDATE episodes SET session_id = ? WHERE path = ?", moved)
    return len(moved)


def reset_fingerprints(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        conn.execute("UPDATE transcripts SET fingerprint = ''")
        conn.execute("DELETE FROM failures")


def _bool_or_none(value: object) -> bool | None:
    return None if value is None else bool(value)


def _request_fact(r: sqlite3.Row) -> RequestFact:
    return RequestFact(
        line=r["line"],
        dedupe_key=r["dedupe_key"],
        timestamp=r["ts"],
        model=r["model"],
        input=r["input"],
        output=r["output"],
        cache_read=r["cache_read"],
        cache_write=r["cache_write"],
        cache_write_5m=r["cache_write_5m"],
        cache_write_1h=r["cache_write_1h"],
        reasoning=r["reasoning"],
        context_tokens=r["context_tokens"],
        context_window=r["context_window"],
        gap_seconds=r["gap_seconds"],
        rebuild=bool(r["rebuild"]),
        on_main_branch=bool(r["on_main_branch"]),
        skill=r["skill"],
        plugin=r["plugin"],
    )


def _call_fact(r: sqlite3.Row) -> ToolCallFact:
    return ToolCallFact(
        seq=r["seq"],
        line=r["line"],
        call_id=r["call_id"],
        dedupe_key=r["dedupe_key"],
        timestamp=r["ts"],
        tool=r["tool"],
        kind=r["kind"],
        mcp_server=r["mcp_server"],
        mcp_tool=r["mcp_tool"],
        skill=r["skill"],
        command=r["command"],
        command_class=r["command_class"],
        file_path=r["file_path"],
        input_hash=r["input_hash"],
        input_preview=r["input_preview"],
        edit_old_hash=r["edit_old_hash"],
        edit_new_hash=r["edit_new_hash"],
        is_error=_bool_or_none(r["is_error"]),
        status=r["status"],
        error_category=r["error_category"],
        error_text=r["error_text"],
        signature=r["signature"],
        on_main_branch=bool(r["on_main_branch"]),
        read_offset=r["read_offset"],
        read_limit=r["read_limit"],
    )


def _message_fact(r: sqlite3.Row) -> UserMessageFact:
    return UserMessageFact(
        before_call=r["before_call"],
        line=r["line"],
        message_id=r["message_id"],
        dedupe_key=r["dedupe_key"],
        timestamp=r["ts"],
        origin=r["origin"],
        text=r["text"],
        is_interrupt=bool(r["is_interrupt"]),
        slash_command=r["slash_command"],
        rule_kind=r["rule_kind"],
        content_hash=r["content_hash"],
        on_main_branch=bool(r["on_main_branch"]),
    )


def _compaction_fact(r: sqlite3.Row) -> CompactionFact:
    return CompactionFact(
        before_call=r["before_call"],
        line=r["line"],
        timestamp=r["ts"],
        trigger=r["trigger"],
        pre_tokens=r["pre_tokens"],
        post_tokens=r["post_tokens"],
    )


def _rewind_fact(r: sqlite3.Row) -> RewindFact:
    return RewindFact(
        line=r["line"],
        timestamp=r["ts"],
        branch_uuid=r["branch_uuid"],
        before_call=r["before_call"],
        abandoned_calls=r["abandoned_calls"],
    )


def _rows(conn: sqlite3.Connection, table: str, path: str, order: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM {table} WHERE path = ? ORDER BY {order}", (path,)
    ).fetchall()


def facts_from_rows(conn: sqlite3.Connection, path: str) -> TranscriptFacts:
    t = conn.execute("SELECT * FROM transcripts WHERE path = ?", (path,)).fetchone()
    if t is None:
        raise KeyError(path)
    return TranscriptFacts(
        ref=TranscriptRef(
            path=Path(t["path"]),
            agent=t["agent"],
            root=t["root"],
            archived=bool(t["archived"]),
            fingerprint=t["fingerprint"],
        ),
        session_id=t["session_id"],
        thread_id=t["thread_id"],
        thread_key=t["thread_key"],
        parent_thread_id=t["parent_thread_id"],
        parent_tool_use_id=t["parent_tool_use_id"],
        subagent_type=t["subagent_type"],
        subagent_description=t["subagent_description"],
        cwd=t["cwd"],
        git_branch=t["git_branch"],
        first_ts=t["first_ts"],
        last_ts=t["last_ts"],
        final_text=t["final_text"],
        last_event=t["last_event"],
        api_errors=t["api_errors"],
        requests=tuple(_request_fact(r) for r in _rows(conn, "requests", path, "line")),
        tool_calls=tuple(_call_fact(r) for r in _rows(conn, "tool_calls", path, "seq")),
        user_messages=tuple(_message_fact(r) for r in _rows(conn, "user_messages", path, "line")),
        compactions=tuple(_compaction_fact(r) for r in _rows(conn, "compactions", path, "line")),
        rewinds=tuple(_rewind_fact(r) for r in _rows(conn, "rewinds", path, "line")),
    )


def episode_from_row(row: sqlite3.Row) -> Episode:
    return Episode(
        episode_id=row["episode_id"],
        anchor=row["anchor"],
        agent=row["agent"],
        session_id=row["session_id"],
        thread_id=row["thread_id"],
        thread_key=row["thread_key"],
        path=row["path"],
        kinds=tuple(
            signals.SIGNAL_KINDS[k] for k in row["kinds"].split(",") if k in signals.SIGNAL_KINDS
        ),
        start_seq=row["start_seq"],
        end_seq=row["end_seq"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        start_ts=row["start_ts"],
        end_ts=row["end_ts"],
        calls=row["calls"],
        severity=row["severity"],
        tokens=row["tokens"],
        content_hash=row["content_hash"],
        excerpt=row["excerpt"],
    )


type Deriver = Callable[
    [TranscriptFacts], tuple[Sequence[SignalHit], Sequence[Episode], ThreadStats]
]


def rederive(
    conn: sqlite3.Connection, derive: Deriver, now_iso: str, notice: Callable[[str], None]
) -> int:
    with transaction(conn):
        for table in DERIVED_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        for ddl in DERIVED_DDL:
            conn.execute(ddl)
        stored = [r["path"] for r in conn.execute("SELECT path FROM transcripts ORDER BY path")]
        for path in stored:
            facts = facts_from_rows(conn, path)
            try:
                derived = derive(facts)
            except Exception as error:  # noqa: BLE001 -- any failure is recorded against its transcript
                record_failure(conn, facts.ref, f"{type(error).__name__}: {error}", now_iso)
                notice(
                    f"rl analyze: could not derive {taxonomy.printable(path)}:"
                    f" {taxonomy.printable(str(error))}"
                )
                continue
            conn.execute("DELETE FROM failures WHERE path = ?", (path,))
            _insert_derived(conn, path, *derived)
        _set_meta(conn, "derive_version", str(DERIVE_VERSION))
    return len(stored)


def finish_cold_build(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        for ddl in FACTS_INDEX_DDL:
            conn.execute(ddl)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def mark_complete(conn: sqlite3.Connection, now_iso: str) -> None:
    _set_meta(conn, "complete_at", now_iso)
