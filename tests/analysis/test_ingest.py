import fcntl
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ringleader.analysis import adapters, ingest, signals, store
from ringleader.analysis.facts import TranscriptRef
from ringleader.sources.base import stat_fingerprint

from .factories import claude_records, write_claude, write_jsonl

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
SIDS = tuple(f"0000000{i}-2222-3333-4444-555555555555" for i in range(3))
NEXT_DERIVE = store.DERIVE_VERSION + 1


class Counting:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, ref):
        self.calls.append(ref.path)
        if self.fail_on is not None:
            self.fail_on(ref, len(self.calls))
        return ingest.extract(ref)


def _no_notice(message):
    raise AssertionError(f"unexpected notice: {message}")


@pytest.fixture
def roots(tmp_path):
    made = ingest.Roots(tmp_path / "claude", tmp_path / "codex", tmp_path / "archive")
    for d in (made.claude, made.codex, made.archive):
        d.mkdir()
    return made


@pytest.fixture
def index_dir(tmp_path):
    return tmp_path / "index"


def _prepare(index_dir, roots, **kwargs):
    options = {"refresh": True, "rebuild": False, "notice": _no_notice, **kwargs}
    return ingest.prepare_index(index_dir, roots=roots, now=NOW, **options)


def _read(stats, sql, params=()):
    with closing(store.connect(Path(stats.path), read_only=True)) as conn:
        return [tuple(r) for r in conn.execute(sql, params)]


def _paths(stats, table="transcripts"):
    return sorted(r[0] for r in _read(stats, f"SELECT DISTINCT path FROM {table}"))


def _hold(lock_path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _release(handle):
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def test_cold_build_indexes_every_claude_transcript(roots, index_dir):
    written = [str(write_claude(roots.claude, sid)) for sid in SIDS]
    stats = _prepare(index_dir, roots)
    assert (stats.transcripts, stats.attempted, stats.changed) == (3, 3, 3)
    assert (stats.complete, stats.stale, stats.writable) == (True, False, True)
    assert stats.path == str(index_dir / f"index-v{store.FACTS_VERSION}.sqlite")
    assert _paths(stats) == sorted(written)


def test_no_change_refresh_extracts_nothing(roots, index_dir):
    for sid in SIDS:
        write_claude(roots.claude, sid)
    _prepare(index_dir, roots)
    counting = Counting()
    stats = _prepare(index_dir, roots, extractor=counting)
    assert counting.calls == []
    assert (stats.attempted, stats.changed, stats.transcripts) == (0, 0, 3)


def test_appended_line_re_extracts_exactly_that_transcript(roots, index_dir):
    paths = [write_claude(roots.claude, sid) for sid in SIDS]
    _prepare(index_dir, roots)
    with paths[1].open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "summary", "sessionId": SIDS[1]}) + "\n")
    counting = Counting()
    stats = _prepare(index_dir, roots, extractor=counting)
    assert counting.calls == [paths[1]]
    assert stats.changed == 1


def test_touching_a_subagent_meta_re_extracts_it(roots, index_dir):
    write_claude(roots.claude, SIDS[0])
    sub = write_jsonl(
        roots.claude / "-w" / SIDS[0] / "subagents" / "agent-abc.jsonl",
        claude_records(SIDS[0]),
    )
    meta = sub.with_suffix(".meta.json")
    meta.write_text(json.dumps({"agentType": "Explore"}), encoding="utf-8")
    first = _prepare(index_dir, roots)
    assert first.transcripts == 2
    os.utime(meta, ns=(1_000_000_000, 1_000_000_000))
    counting = Counting()
    _prepare(index_dir, roots, extractor=counting)
    assert counting.calls == [sub]


def test_workflow_subagents_are_discovered(roots):
    write_claude(roots.claude, SIDS[0])
    flow = write_jsonl(
        roots.claude / "-w" / SIDS[0] / "subagents" / "workflows" / "wf_1" / "agent-d.jsonl",
        claude_records(SIDS[0]),
    )
    found = ingest.discover(roots)
    assert flow in {ref.path for ref in found.refs}


def test_deleted_transcript_rows_are_dropped(roots, index_dir):
    paths = [write_claude(roots.claude, sid) for sid in SIDS]
    _prepare(index_dir, roots)
    paths[0].unlink()
    stats = _prepare(index_dir, roots)
    assert stats.removed == 1
    assert _paths(stats) == sorted(str(p) for p in paths[1:])
    assert _paths(stats, "tool_calls") == sorted(str(p) for p in paths[1:])


def test_rows_survive_an_unavailable_root(roots, index_dir):
    paths = [write_claude(roots.claude, sid) for sid in SIDS]
    _prepare(index_dir, roots)
    for p in paths:
        p.unlink()
    (roots.claude / "-w").rmdir()
    roots.claude.rmdir()
    stats = _prepare(index_dir, roots)
    assert stats.removed == 0
    assert _paths(stats) == sorted(str(p) for p in paths)


def test_older_index_files_are_pruned_and_newer_ones_kept(roots, index_dir):
    index_dir.mkdir()
    old = [index_dir / name for name in ("index-v0.sqlite", "index-v0.sqlite-wal")]
    newer = index_dir / "index-v9.sqlite"
    for p in (*old, newer):
        p.write_bytes(b"x")
    _prepare(index_dir, roots)
    assert sorted(p.name for p in index_dir.iterdir()) == [
        f"index-v{store.FACTS_VERSION}.sqlite",
        "index-v9.sqlite",
        "index.lock",
    ]
    assert newer.read_bytes() == b"x"


def test_derive_version_change_rederives_from_stored_facts(roots, index_dir, monkeypatch):
    path = write_claude(roots.claude, SIDS[0], ("make", "make"))
    first = _prepare(index_dir, roots)
    kinds = "SELECT kind FROM signals WHERE path = ?"
    assert ("retry_run",) not in _read(first, kinds, (str(path),))
    monkeypatch.setattr(store, "DERIVE_VERSION", NEXT_DERIVE)
    monkeypatch.setattr(signals, "RETRY_RUN_MIN", 2)
    counting = Counting()
    stats = _prepare(index_dir, roots, extractor=counting)
    assert counting.calls == []
    assert (stats.rederived, stats.derive_version) == (True, NEXT_DERIVE)
    assert ("retry_run",) in _read(stats, kinds, (str(path),))
    assert _read(stats, "SELECT path FROM thread_stats") == [(str(path),)]


def test_extractor_failure_is_recorded_skipped_then_retried(roots, index_dir):
    path = write_claude(roots.claude, SIDS[0])

    def boom(ref, n):
        raise ValueError("bad transcript")

    notices = []
    stats = _prepare(index_dir, roots, extractor=Counting(boom), notice=notices.append)
    assert (stats.failed_now, stats.failed, stats.transcripts) == (1, 1, 0)
    assert _read(stats, "SELECT path, error FROM failures") == [
        (str(path), "ValueError: bad transcript")
    ]
    assert notices == [f"rl analyze: could not index {path}: bad transcript"]
    again = Counting(boom)
    assert _prepare(index_dir, roots, extractor=again).failed == 1
    assert again.calls == []
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n")
    retried = Counting()
    stats = _prepare(index_dir, roots, extractor=retried)
    assert retried.calls == [path]
    assert (stats.failed, stats.transcripts) == (0, 1)


def test_database_error_while_writing_is_recorded_as_a_failure(roots, index_dir, monkeypatch):
    path = write_claude(roots.claude, SIDS[0])

    def broken(*args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_insert_derived", broken)
    notices = []
    stats = _prepare(index_dir, roots, notice=notices.append)
    assert notices == [f"rl analyze: could not index {path}: disk I/O error"]
    assert (stats.failed_now, stats.transcripts) == (1, 0)
    assert _read(stats, "SELECT path, error FROM failures") == [
        (str(path), "OperationalError: disk I/O error")
    ]
    assert _read(stats, "SELECT count(*) FROM tool_calls") == [(0,)]


def test_held_lock_with_a_complete_index_reads_stale(roots, index_dir):
    write_claude(roots.claude, SIDS[0])
    built = _prepare(index_dir, roots)
    write_claude(roots.claude, SIDS[1])
    handle = _hold(index_dir / "index.lock")
    try:
        counting = Counting()
        stats = _prepare(index_dir, roots, extractor=counting)
    finally:
        _release(handle)
    assert (stats.stale, stats.complete, stats.transcripts) == (True, True, 1)
    assert counting.calls == []
    assert _paths(stats) == _paths(built)


def test_held_lock_without_a_complete_index_waits_then_builds(roots, index_dir):
    write_claude(roots.claude, SIDS[0])
    handle = _hold(index_dir / "index.lock")
    notices = []

    def notice(message):
        notices.append(message)
        _release(handle)

    stats = _prepare(index_dir, roots, notice=notice)
    assert notices == [ingest.WAITING_NOTICE]
    assert (stats.complete, stats.stale, stats.transcripts) == (True, False, 1)


def test_no_refresh_waits_for_another_process_to_finish_a_cold_build(roots, index_dir, monkeypatch):
    writer, _cold = store.open_index_for_write(index_dir, NOW.isoformat())
    handle = _hold(index_dir / "index.lock")
    waiting = threading.Event()

    class Watched:
        LOCK_SH, LOCK_UN, LOCK_NB = fcntl.LOCK_SH, fcntl.LOCK_UN, fcntl.LOCK_NB

        @staticmethod
        def flock(fd, operation):
            if operation == fcntl.LOCK_SH:
                waiting.set()
            fcntl.flock(fd, operation)

    monkeypatch.setattr(ingest, "fcntl", Watched)
    found = {}
    notices = []
    reader = threading.Thread(
        target=lambda: found.update(
            stats=_prepare(index_dir, roots, refresh=False, notice=notices.append)
        )
    )
    reader.start()
    try:
        assert waiting.wait(5)
        with closing(writer), store.transaction(writer):
            store.mark_complete(writer, NOW.isoformat())
    finally:
        _release(handle)
        reader.join(5)
    assert (found["stats"].complete, found["stats"].transcripts) == (True, 0)
    assert notices == [ingest.WAITING_NOTICE]


def test_no_refresh_without_an_index_exits_3(roots, index_dir):
    with pytest.raises(store.IndexUnavailable) as raised:
        _prepare(index_dir, roots, refresh=False)
    assert (raised.value.exit_code, raised.value.reason) == (3, "missing")


def test_no_refresh_reads_a_complete_index(roots, index_dir):
    write_claude(roots.claude, SIDS[0])
    _prepare(index_dir, roots)
    write_claude(roots.claude, SIDS[1])
    stats = _prepare(index_dir, roots, refresh=False, extractor=Counting(_no_notice))
    assert (stats.complete, stats.transcripts) == (True, 1)


def test_no_refresh_refuses_an_index_from_another_derive_version(roots, index_dir, monkeypatch):
    _prepare(index_dir, roots)
    monkeypatch.setattr(store, "DERIVE_VERSION", NEXT_DERIVE)
    with pytest.raises(store.IndexUnavailable) as raised:
        _prepare(index_dir, roots, refresh=False)
    assert (raised.value.exit_code, raised.value.reason) == (3, "mismatch")


def test_no_refresh_waits_for_a_writer_rederiving_an_index_from_another_version(
    roots, index_dir, monkeypatch
):
    _prepare(index_dir, roots)
    current = store.DERIVE_VERSION
    monkeypatch.setattr(store, "DERIVE_VERSION", NEXT_DERIVE)
    handle = _hold(index_dir / "index.lock")
    notices = []

    def notice(message):
        notices.append(message)
        monkeypatch.setattr(store, "DERIVE_VERSION", current)
        _release(handle)

    stats = _prepare(index_dir, roots, refresh=False, notice=notice)
    assert notices == [ingest.WAITING_NOTICE]
    assert stats.complete is True


def test_unwritable_index_dir_reads_an_index_that_has_wal_files(roots, index_dir):
    write_claude(roots.claude, SIDS[0])
    built = _prepare(index_dir, roots)
    _read(built, "SELECT 1")
    files = sorted(index_dir.iterdir())
    for p in files:
        p.chmod(0o400)
    index_dir.chmod(0o500)
    try:
        stats = _prepare(index_dir, roots)
    finally:
        index_dir.chmod(0o700)
        for p in files:
            p.chmod(0o600)
    assert (stats.writable, stats.complete, stats.transcripts) == (False, True, 1)


def test_unwritable_index_dir_without_an_index_names_the_path(roots, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    with pytest.raises(store.IndexUnavailable) as raised:
        _prepare(blocker / "index", roots)
    assert (raised.value.exit_code, raised.value.reason) == (1, "unwritable")
    assert str(blocker / "index" / f"index-v{store.FACTS_VERSION}.sqlite") in str(raised.value)


def test_interrupted_cold_build_is_incomplete_then_resumes(roots, index_dir, monkeypatch):
    for sid in SIDS:
        write_claude(roots.claude, sid)
    monkeypatch.setattr(ingest, "COMMIT_EVERY", 1)

    def interrupt(ref, n):
        if n == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _prepare(index_dir, roots, extractor=Counting(interrupt))
    path = index_dir / f"index-v{store.FACTS_VERSION}.sqlite"
    with closing(store.connect(path, read_only=True)) as conn:
        assert store.is_complete(conn) is False
        assert store.is_cold(conn) is True
        assert conn.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 1
    counting = Counting()
    stats = _prepare(index_dir, roots, extractor=counting)
    assert len(counting.calls) == 2
    assert (stats.complete, stats.transcripts) == (True, 3)


def test_cold_build_creates_the_facts_indexes(roots, index_dir):
    write_claude(roots.claude, SIDS[0])
    stats = _prepare(index_dir, roots)
    names = {r[0] for r in _read(stats, "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert set(store.FACTS_INDEX_NAMES) <= names


def _archived(roots, name, records):
    return write_jsonl(roots.archive / "slug" / name, records)


def test_sniff_agent_routes_archive_files(roots):
    claude = _archived(
        roots,
        "claude.jsonl",
        [{"type": "file-history-snapshot", "messageId": "m"}, *claude_records(SIDS[0])],
    )
    codex = _archived(roots, "codex.jsonl", [{"type": "session_meta", "payload": {"id": "x"}}])
    droid = _archived(
        roots,
        "droid.jsonl",
        [{"type": "session_start", "id": "d", "callingSessionId": "c"}, {"type": "message"}],
    )
    pi = _archived(roots, "pi.jsonl", [{"type": "session", "id": "p"}, {"type": "model_change"}])
    assert [ingest.sniff_agent(p) for p in (claude, codex, droid, pi)] == [
        "claude-code",
        "codex",
        None,
        None,
    ]
    found = {ref.path: (ref.agent, ref.root, ref.archived) for ref in ingest.discover(roots).refs}
    assert found == {
        claude: ("claude-code", "archive", True),
        codex: ("codex", "archive", True),
    }


def test_archived_subagents_are_discovered(roots):
    sub = write_jsonl(
        roots.archive / "slug" / SIDS[0] / "subagents" / "agent-a.jsonl", claude_records(SIDS[0])
    )
    assert sub in {ref.path for ref in ingest.discover(roots).refs}


def test_codex_transcripts_are_discovered_and_extracted(roots, index_dir):
    rollout = write_jsonl(
        roots.codex / "2026" / "10" / "rollout-x.jsonl", [{"type": "session_meta"}]
    )
    found = ingest.discover(roots)
    assert [(r.path, r.agent, r.root) for r in found.refs] == [(rollout, "codex", "codex")]
    stats = _prepare(index_dir, roots)
    assert (stats.attempted, stats.transcripts, stats.failed) == (1, 1, 0)
    assert _read(stats, "SELECT agent, session_id FROM transcripts") == [("codex", "rollout-x")]


def test_ingest_leaves_transcripts_untouched(roots, index_dir):
    paths = [write_claude(roots.claude, sid) for sid in SIDS]
    before = [stat_fingerprint(p.stat()) for p in paths]
    _prepare(index_dir, roots)
    assert [stat_fingerprint(p.stat()) for p in paths] == before


def test_a_wal_reader_opened_mid_refresh_sees_committed_rows(roots, index_dir):
    write_claude(roots.claude, SIDS[0])
    _prepare(index_dir, roots)
    write_claude(roots.claude, SIDS[1])
    seen = []

    def read_mid_refresh(done, total):
        with closing(
            store.connect(index_dir / f"index-v{store.FACTS_VERSION}.sqlite", read_only=True)
        ) as conn:
            seen.append(conn.execute("SELECT count(*) FROM transcripts").fetchone()[0])

    stats = _prepare(index_dir, roots, progress=read_mid_refresh)
    assert seen == [1]
    assert stats.transcripts == 2


def test_rebuild_re_extracts_every_transcript(roots, index_dir):
    for sid in SIDS:
        write_claude(roots.claude, sid)
    _prepare(index_dir, roots)
    counting = Counting()
    stats = _prepare(index_dir, roots, rebuild=True, extractor=counting)
    assert len(counting.calls) == 3
    assert (stats.changed, stats.transcripts) == (3, 3)


def test_extract_dispatches_claude_and_codex(roots):
    path = write_claude(roots.claude, SIDS[0])
    rollout = write_jsonl(
        roots.codex / f"rollout-2026-10-02T10-00-00-{SIDS[1]}.jsonl", [{"type": "session_meta"}]
    )
    claude = ingest.extract(TranscriptRef(path, "claude-code", "claude", False, "f"))
    codex = ingest.extract(TranscriptRef(rollout, "codex", "codex", False, "f"))
    assert claude is not None
    assert codex is not None
    assert (claude.session_id, codex.session_id) == (SIDS[0], SIDS[1])


def test_no_change_refresh_over_500_transcripts_is_fast(roots, index_dir):
    for i in range(500):
        write_claude(roots.claude, f"{i:08d}-2222-3333-4444-555555555555", project=f"-p{i % 10}")
    built = _prepare(index_dir, roots)
    assert built.transcripts == 500
    start = time.monotonic()
    stats = _prepare(index_dir, roots)
    assert time.monotonic() - start < 3.0
    assert stats.attempted == 0


def test_unreadable_root_keeps_its_rows(roots, index_dir):
    write_claude(roots.claude, SIDS[0])
    _prepare(index_dir, roots)
    roots.claude.chmod(0o000)
    try:
        stats = _prepare(index_dir, roots)
    finally:
        roots.claude.chmod(0o700)
    assert (stats.transcripts, stats.removed) == (1, 0)


def test_failure_row_of_a_deleted_transcript_is_cleared(roots, index_dir):
    gone = write_claude(roots.claude, SIDS[0])
    _prepare(index_dir, roots, extractor=Counting(fail_on=_boom), notice=lambda message: None)
    gone.unlink()
    stats = _prepare(index_dir, roots)
    assert (stats.failed, stats.removed) == (0, 0)


def _boom(ref, n):
    raise ValueError("boom")


def test_garbage_index_file_is_reported_with_its_path(roots, index_dir):
    index_dir.mkdir()
    bad = index_dir / f"index-v{store.FACTS_VERSION}.sqlite"
    bad.write_bytes(b"not a database" * 100)
    with pytest.raises(store.IndexUnavailable) as raised:
        _prepare(index_dir, roots, rebuild=True)
    assert str(bad) in str(raised.value)
    assert (raised.value.exit_code, raised.value.reason) == (1, "unreadable")


def test_index_dir_that_is_not_ours_keeps_its_mode(roots, index_dir):
    index_dir.mkdir(mode=0o755)
    index_dir.chmod(0o755)
    _prepare(index_dir, roots)
    assert index_dir.stat().st_mode & 0o777 == 0o755


def test_rederive_failure_keeps_facts_and_records_a_failure(roots, index_dir, monkeypatch):
    path = write_claude(roots.claude, SIDS[0])
    _prepare(index_dir, roots)
    monkeypatch.setattr(store, "DERIVE_VERSION", NEXT_DERIVE)

    def broken(facts):
        raise ValueError("derive broke")

    monkeypatch.setattr(ingest, "derive", broken)
    notices = []
    stats = _prepare(index_dir, roots, notice=notices.append)
    assert notices == [f"rl analyze: could not derive {path}: derive broke"]
    assert (stats.rederived, stats.transcripts, stats.failed) == (True, 1, 1)
    assert _read(stats, "SELECT count(*) FROM thread_stats") == [(0,)]


def test_the_adapter_list_derives_the_archive_globs_and_token_only_sources():
    assert adapters.ARCHIVE_GLOBS == ("*/*.jsonl", "*/*/subagents/**/agent-*.jsonl")
    assert adapters.TOKENS_ONLY_SOURCES == ("droid", "opencode", "pi")
    assert [a.agent for a in adapters.ADAPTERS] == ["claude-code", "codex"]
