from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import ExitStack, closing
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Literal

from .. import paths
from ..sources.base import stat_fingerprint
from ..sources.claude_code import default_projects_dir
from ..sources.codex import default_codex_dir
from ..usage.codex import StartCache
from . import store, taxonomy
from .adapters import ADAPTERS, ARCHIVE_GLOBS, BY_AGENT, Roots
from .codex import start_cache
from .episodes import Episode, episodes_for
from .facts import Agent, Root, TranscriptFacts, TranscriptRef
from .health import ThreadStats, thread_stats
from .signals import SignalHit, detect

COMMIT_EVERY = 200
FAILURE_RATIO_LIMIT = 0.20
FAILURE_RATIO_MIN_ATTEMPTED = 20
SNIFF_LINES = 20
WAITING_NOTICE = "waiting for another rl process to finish the analysis index…"
FOREIGN_FIRST_TYPES = frozenset({"session_start", "session"})


@dataclass(frozen=True)
class Discovery:
    refs: tuple[TranscriptRef, ...]
    unavailable: frozenset[Root]


@dataclass(frozen=True)
class IngestStats:
    path: str
    facts_version: int
    derive_version: int
    transcripts: int
    attempted: int
    changed: int
    removed: int
    failed_now: int
    failed: int
    rederived: bool
    complete: bool
    stale: bool
    writable: bool
    seconds: float


type Extractor = Callable[[TranscriptRef], TranscriptFacts | None]
type Notice = Callable[[str], None]
type _Outcome = Literal["skipped", "changed", "failed"]


class _Unwritable(Exception):
    pass


def default_roots() -> Roots:
    return Roots(default_projects_dir(), default_codex_dir(), paths.archive_root())


def _fingerprint(path: Path) -> str | None:
    try:
        own = stat_fingerprint(path.stat())
    except OSError:
        return None
    if "subagents" not in path.parts:
        return own
    try:
        return f"{own}|{stat_fingerprint(path.with_suffix('.meta.json').stat())}"
    except OSError:
        return own


def _globbed(root: Path, patterns: Iterable[str]) -> list[Path]:
    return sorted({p for pattern in patterns for p in root.glob(pattern) if p.is_file()})


type Known = Mapping[str, tuple[Agent, str]]


def _refs(
    files: Iterable[Path], agent_of: Callable[[Path], Agent | None], root: Root, known: Known
) -> Iterator[TranscriptRef]:
    for path in files:
        fingerprint = _fingerprint(path)
        if fingerprint is None:
            continue
        prior = known.get(str(path))
        agent = prior[0] if prior is not None and prior[1] == fingerprint else agent_of(path)
        if agent:
            yield TranscriptRef(path, agent, root, root == "archive", fingerprint)


def _sniff_record(record: object) -> Agent | Literal["foreign"] | None:
    if not isinstance(record, dict):
        return None
    found = next((a.agent for a in ADAPTERS if a.sniff(record)), None)
    if found is not None:
        return found
    return "foreign" if record.get("type") in FOREIGN_FIRST_TYPES else None


def _parsed(raw: str) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def sniff_agent(path: Path) -> Agent | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            verdicts = (_sniff_record(_parsed(raw)) for raw in islice(fh, SNIFF_LINES))
            found = next((v for v in verdicts if v is not None), None)
    except OSError:
        return None
    return None if found == "foreign" else found


def _listable(root: Path) -> bool:
    try:
        with os.scandir(root):
            return True
    except OSError:
        return False


def _fixed(agent: Agent) -> Callable[[Path], Agent | None]:
    return lambda _path: agent


def discover(roots: Roots, known: Known | None = None) -> Discovery:
    seen: Known = known or {}
    located: tuple[tuple[Root, Path], ...] = (
        *((a.root, a.live_dir(roots)) for a in ADAPTERS),
        ("archive", roots.archive),
    )
    unavailable = frozenset(name for name, d in located if not _listable(d))
    refs = (
        *(
            ref
            for a in ADAPTERS
            for ref in _refs(_globbed(a.live_dir(roots), a.globs), _fixed(a.agent), a.root, seen)
        ),
        *_refs(_globbed(roots.archive, ARCHIVE_GLOBS), sniff_agent, "archive", seen),
    )
    return Discovery(refs=refs, unavailable=unavailable)


def extract(ref: TranscriptRef, starts: StartCache | None = None) -> TranscriptFacts | None:
    adapter = BY_AGENT.get(ref.agent)
    return adapter.extract(ref, starts) if adapter is not None else None


def run_extractor() -> Extractor:
    starts = start_cache()
    return lambda ref: extract(ref, starts)


def derive(
    facts: TranscriptFacts,
) -> tuple[tuple[SignalHit, ...], tuple[Episode, ...], ThreadStats]:
    hits = detect(facts)
    episodes = episodes_for(facts, hits)
    return hits, episodes, thread_stats(facts, episodes)


def _ingest_one(
    conn: sqlite3.Connection, ref: TranscriptRef, extractor: Extractor, now_iso: str, notice: Notice
) -> _Outcome:
    conn.execute("SAVEPOINT transcript")
    try:
        facts = extractor(ref)
        if facts is not None:
            store.replace_transcript(conn, facts, *derive(facts))
    except Exception as error:  # noqa: BLE001 -- any failure is recorded against its transcript
        conn.execute("ROLLBACK TO transcript")
        conn.execute("RELEASE transcript")
        store.record_failure(conn, ref, f"{type(error).__name__}: {error}", now_iso)
        notice(
            f"rl analyze: could not index {taxonomy.printable(str(ref.path))}:"
            f" {taxonomy.printable(str(error))}"
        )
        return "failed"
    conn.execute("RELEASE transcript")
    return "skipped" if facts is None else "changed"


def _todo(conn: sqlite3.Connection, discovery: Discovery) -> list[TranscriptRef]:
    stored = store.fingerprints(conn)
    failed = store.failed_paths(conn)
    return [
        ref
        for ref in discovery.refs
        if stored.get(str(ref.path)) != ref.fingerprint
        and failed.get(str(ref.path)) != (ref.fingerprint, store.FACTS_VERSION)
    ]


def _missing(path: str) -> bool:
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


def _vanished(conn: sqlite3.Connection, discovery: Discovery) -> tuple[list[str], list[str]]:
    current = {str(ref.path) for ref in discovery.refs}
    stored = store.stored_roots(conn)
    gone = sorted(
        path
        for path, root in stored.items()
        if path not in current and root not in discovery.unavailable
    )
    stale_failures = sorted(
        path
        for path in store.failed_paths(conn)
        if path not in current and path not in stored and _missing(path)
    )
    return gone, stale_failures


def refresh(
    conn: sqlite3.Connection,
    discovery: Discovery,
    *,
    now_iso: str,
    notice: Notice,
    extractor: Extractor | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> IngestStats:
    start = time.monotonic()
    run = extractor or run_extractor()
    cold = store.is_cold(conn)
    todo = _todo(conn, discovery)
    outcomes: list[_Outcome] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        vanished, stale_failures = _vanished(conn, discovery)
        store.drop_transcripts(conn, [*vanished, *stale_failures])
        for ref in todo:
            outcomes.append(_ingest_one(conn, ref, run, now_iso, notice))
            if len(outcomes) % COMMIT_EVERY == 0:
                conn.execute("COMMIT")
                conn.execute("BEGIN IMMEDIATE")
            if progress is not None:
                progress(len(outcomes), len(todo))
        for adapter in ADAPTERS:
            if adapter.after_ingest is not None:
                adapter.after_ingest(conn)
        if not cold:
            store.mark_complete(conn, now_iso)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    if cold:
        store.finish_cold_build(conn)
        with store.transaction(conn):
            store.mark_complete(conn, now_iso)
    transcripts, failed = store.counts(conn)
    facts_version, derive_version = store.index_versions(conn)
    return IngestStats(
        path=str(_db_path(conn)),
        facts_version=facts_version or 0,
        derive_version=derive_version or 0,
        transcripts=transcripts,
        attempted=sum(1 for o in outcomes if o != "skipped"),
        changed=outcomes.count("changed"),
        removed=len(vanished),
        failed_now=outcomes.count("failed"),
        failed=failed,
        rederived=False,
        complete=True,
        stale=False,
        writable=True,
        seconds=time.monotonic() - start,
    )


def _db_path(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    return str(row["file"])


def _current(stats: IngestStats) -> bool:
    return stats.complete and (stats.facts_version, stats.derive_version) == (
        store.FACTS_VERSION,
        store.DERIVE_VERSION,
    )


def _unreadable(path: Path, error: sqlite3.Error) -> store.IndexUnavailable:
    return store.IndexUnavailable(
        f"cannot read the analysis index at {path}: {error}; if the file is damaged, delete it",
        reason="unreadable",
    )


def _snapshot(path: Path) -> IngestStats | None:
    if not path.is_file():
        return None
    try:
        with closing(store.connect(path, read_only=True)) as conn:
            facts_version, derive_version = store.index_versions(conn)
            if facts_version is None:
                return None
            transcripts, failed = store.counts(conn)
            complete = store.is_complete(conn)
    except sqlite3.Error as error:
        raise _unreadable(path, error) from error
    return IngestStats(
        path=str(path),
        facts_version=facts_version,
        derive_version=derive_version or 0,
        transcripts=transcripts,
        attempted=0,
        changed=0,
        removed=0,
        failed_now=0,
        failed=failed,
        rederived=False,
        complete=complete,
        stale=False,
        writable=True,
        seconds=0.0,
    )


def _mismatch(path: Path) -> store.IndexUnavailable:
    return store.IndexUnavailable(
        f"analysis index {path} was built by a different rl version; run without --no-refresh",
        reason="mismatch",
    )


def _wait_for_writer(lock_path: Path, notice: Notice) -> None:
    try:
        handle = lock_path.open("rb")
    except OSError:
        return
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            notice(WAITING_NOTICE)
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_without_refresh(path: Path, lock_path: Path, notice: Notice) -> IngestStats:
    found = _snapshot(path)
    if found is None or not found.complete:
        _wait_for_writer(lock_path, notice)
        found = _snapshot(path)
    if found is not None and found.complete and not _current(found):
        _wait_for_writer(lock_path, notice)
        found = _snapshot(path)
    if found is not None and found.complete:
        if _current(found):
            return found
        raise _mismatch(path)
    raise store.IndexUnavailable(
        f"no complete analysis index at {path}; run without --no-refresh to build it",
        reason="missing",
    )


def _read_unwritable(path: Path, error: BaseException) -> IngestStats:
    try:
        found = _snapshot(path)
    except store.IndexUnavailable:
        found = None
    if found is not None and _current(found):
        return replace(found, writable=False)
    raise store.IndexUnavailable(
        f"cannot write the analysis index at {path}: {error}", reason="unwritable"
    )


def _acquire(stack: ExitStack, index_dir: Path, *, wait: bool) -> bool:
    try:
        paths.ensure_analysis_dir(index_dir)
        return stack.enter_context(
            store.index_write_lock(paths.analysis_lock_path(index_dir), wait=wait)
        )
    except OSError as error:
        raise _Unwritable(error) from error


@dataclass(frozen=True)
class _Build:
    index_dir: Path
    rebuild: bool
    roots: Roots
    now_iso: str
    notice: Notice
    extractor: Extractor | None
    progress: Callable[[int, int], None] | None

    def __call__(self) -> IngestStats:
        start = time.monotonic()
        try:
            conn, _ = store.open_index_for_write(self.index_dir, self.now_iso)
        except (OSError, sqlite3.OperationalError) as error:
            raise _Unwritable(error) from error
        except sqlite3.DatabaseError as error:
            raise _unreadable(
                paths.analysis_index_path(store.FACTS_VERSION, self.index_dir), error
            ) from error
        with closing(conn):
            if self.rebuild:
                store.reset_fingerprints(conn)
            rederived = store.index_versions(conn)[1] != store.DERIVE_VERSION
            if rederived:
                store.rederive(conn, derive, self.now_iso, self.notice)
            stats = refresh(
                conn,
                discover(self.roots, store.known_agents(conn)),
                now_iso=self.now_iso,
                notice=self.notice,
                extractor=self.extractor,
                progress=self.progress,
            )
        return replace(stats, rederived=rederived, seconds=time.monotonic() - start)


def prepare_index(  # noqa: PLR0913 -- injection points the suite drives
    index_dir: Path,
    *,
    refresh: bool,
    rebuild: bool,
    roots: Roots,
    now: datetime,
    notice: Notice,
    extractor: Extractor | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> IngestStats:
    path = paths.analysis_index_path(store.FACTS_VERSION, index_dir)
    if not refresh:
        return _read_without_refresh(path, paths.analysis_lock_path(index_dir), notice)
    build = _Build(index_dir, rebuild, roots, now.isoformat(), notice, extractor, progress)
    try:
        with ExitStack() as stack:
            if _acquire(stack, index_dir, wait=False):
                return build()
        found = _snapshot(path)
        if found is not None and _current(found) and not rebuild:
            return replace(found, stale=True)
        notice(WAITING_NOTICE)
        with ExitStack() as stack:
            _acquire(stack, index_dir, wait=True)
            return build()
    except _Unwritable as error:
        return _read_unwritable(path, error.__cause__ or error)
