import fcntl
import sqlite3
import stat
from contextlib import closing
from dataclasses import replace

import pytest
from ringleader.analysis import ingest, store
from ringleader.analysis.claude import extract_claude
from ringleader.analysis.facts import TranscriptRef

from .factories import (
    SID,
    calls,
    compaction,
    edit,
    facts,
    fail,
    message,
    read,
    request,
    rewind,
    write_claude,
)

NOW_ISO = "2026-10-18T12:00:00+00:00"


def _rich():
    return facts(
        calls(
            read("/w/a.py", read_offset=40, read_limit=20),
            fail("make"),
            fail("make"),
            fail("make"),
            edit("/w/a.py", old="o1", new="n1"),
            {"tool": "Bash", "command": "ls", "status": "unknown", "on_main_branch": False},
        ),
        requests=(request(1, 1000), request(5, 50_000), request(9, 52_000)),
        user_messages=(message(0, "fix it"), message(3, "[Request interrupted by user]")),
        compactions=(compaction(4),),
        rewinds=(rewind(2, 1),),
        final_text="all done",
        api_errors=2,
    )


@pytest.fixture
def conn(tmp_path):
    opened, cold = store.open_index_for_write(tmp_path / "idx", NOW_ISO)
    assert cold is True
    with closing(opened):
        yield opened


def _replace(conn, f):
    with store.transaction(conn):
        store.replace_transcript(conn, f, *ingest.derive(f))


def test_fresh_index_is_private_versioned_and_cold(tmp_path, conn):
    path = tmp_path / "idx" / f"index-v{store.FACTS_VERSION}.sqlite"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert store.index_versions(conn) == (store.FACTS_VERSION, store.DERIVE_VERSION)
    assert store.is_complete(conn) is False
    created = conn.execute("SELECT value FROM meta WHERE key = 'created_at'").fetchone()[0]
    assert created == NOW_ISO
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"transcripts", "requests", "signals", "episodes", "thread_stats"} <= tables


def test_writer_pragmas(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert conn.isolation_level is None
    assert isinstance(conn.execute("SELECT 1 AS one").fetchone(), sqlite3.Row)


def test_read_only_connection_refuses_writes(tmp_path, conn):
    with closing(
        store.connect(tmp_path / "idx" / f"index-v{store.FACTS_VERSION}.sqlite", read_only=True)
    ) as ro:
        assert ro.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.execute("INSERT INTO meta VALUES ('x', 'y')")


def test_facts_round_trip_through_rows(conn):
    original = _rich()
    _replace(conn, original)
    restored = store.facts_from_rows(conn, str(original.ref.path))
    assert restored == original
    assert ingest.derive(restored) == ingest.derive(original)


def test_extracted_transcript_round_trips(tmp_path, conn):
    path = write_claude(tmp_path / "claude", SID, ("make", "make", "make"), failing=True)
    original = extract_claude(TranscriptRef(path, "claude-code", "claude", False, "fp"))
    _replace(conn, original)
    assert store.facts_from_rows(conn, str(path)) == original


def test_replace_transcript_keeps_one_copy_and_clears_its_failure(conn):
    original = _rich()
    with store.transaction(conn):
        store.record_failure(conn, original.ref, "boom", NOW_ISO)
    _replace(conn, original)
    _replace(conn, replace(original, ref=replace(original.ref, fingerprint="g")))
    assert store.fingerprints(conn) == {str(original.ref.path): "g"}
    assert conn.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == 6
    assert store.failed_paths(conn) == {}


def test_derived_rows_follow_the_facts(conn):
    original = _rich()
    _replace(conn, original)
    hits, episodes, stats = ingest.derive(original)
    kinds = {r[0] for r in conn.execute("SELECT kind FROM signals")}
    assert kinds == {h.kind for h in hits}
    stored = conn.execute("SELECT episode_id, kinds, day FROM episodes").fetchall()
    assert [tuple(r) for r in stored] == [
        (e.episode_id, ",".join(e.kinds), e.start_ts[:10]) for e in episodes
    ]
    row = conn.execute("SELECT calls, live_calls, errors, writes FROM thread_stats").fetchone()
    assert tuple(row) == (stats.calls, stats.live_calls, stats.errors, stats.writes)


def test_drop_transcripts_cascades(conn):
    original = _rich()
    _replace(conn, original)
    with store.transaction(conn):
        store.drop_transcripts(conn, [str(original.ref.path)])
    counts = [
        conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("requests", "tool_calls", "user_messages", "signals", "episodes", "thread_stats")
    ]
    assert counts == [0, 0, 0, 0, 0, 0]


def test_rederive_rebuilds_the_derived_layer_and_version(conn, monkeypatch):
    original = _rich()
    _replace(conn, original)
    conn.execute("DELETE FROM signals")
    monkeypatch.setattr(store, "DERIVE_VERSION", 7)
    assert store.rederive(conn, ingest.derive, NOW_ISO, print) == 1
    assert store.index_versions(conn) == (store.FACTS_VERSION, 7)
    kinds = {r[0] for r in conn.execute("SELECT kind FROM signals")}
    assert kinds == {h.kind for h in ingest.derive(original)[0]}


def test_finish_cold_build_and_mark_complete(conn):
    store.finish_cold_build(conn)
    with store.transaction(conn):
        store.mark_complete(conn, NOW_ISO)
    assert store.is_cold(conn) is False
    assert store.is_complete(conn) is True


def test_prune_old_versions_removes_only_older_files(tmp_path):
    names = [
        "index-v0.sqlite",
        "index-v0.sqlite-shm",
        "index-v1.sqlite",
        "index-v2.sqlite-wal",
        "labels.sqlite",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"")
    removed = store.prune_old_versions(tmp_path, 1)
    assert [p.name for p in removed] == ["index-v0.sqlite", "index-v0.sqlite-shm"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "index-v1.sqlite",
        "index-v2.sqlite-wal",
        "labels.sqlite",
    ]


def test_write_lock_without_waiting_reports_busy(tmp_path):
    lock = tmp_path / "index.lock"
    with lock.open("a+b") as other:
        fcntl.flock(other.fileno(), fcntl.LOCK_EX)
        with store.index_write_lock(lock, wait=False) as held:
            assert held is False
        fcntl.flock(other.fileno(), fcntl.LOCK_UN)
    with store.index_write_lock(lock, wait=False) as held:
        assert held is True


def test_open_labels_creates_the_label_tables(tmp_path):
    path = tmp_path / "analysis" / "labels.sqlite"
    with closing(store.open_labels(path)) as labels:
        tables = {r[0] for r in labels.execute("SELECT name FROM sqlite_master")}
    assert {"meta", "episode_labels", "message_labels", "issue_names"} <= tables
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_open_labels_records_its_schema_version(tmp_path):
    path = tmp_path / "labels.sqlite"
    with closing(store.open_labels(path)) as labels:
        assert store.labels_version(labels) == store.LABELS_SCHEMA_VERSION


def test_open_labels_upgrades_a_file_without_a_schema_version(tmp_path):
    path = tmp_path / "labels.sqlite"
    with closing(sqlite3.connect(path)) as raw:
        raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        raw.commit()
    with closing(store.open_labels(path)) as labels:
        assert store.labels_version(labels) == store.LABELS_SCHEMA_VERSION


def test_open_labels_refuses_a_file_from_a_newer_rl(tmp_path):
    path = tmp_path / "labels.sqlite"
    with closing(store.open_labels(path)) as labels:
        labels.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(store.LABELS_SCHEMA_VERSION + 1),),
        )
    with pytest.raises(store.LabelsTooNew, match="newer rl"):
        store.open_labels(path)


def _at(f, name):
    return replace(f, ref=replace(f.ref, path=f.ref.path.with_name(name)))


@pytest.mark.parametrize("order", [("a.jsonl", "z.jsonl"), ("z.jsonl", "a.jsonl")])
def test_a_shared_episode_belongs_to_the_smallest_path_whatever_the_order(conn, order):
    base = _rich()
    for name in order:
        _replace(conn, _at(base, name))
    owners = {r[0].rsplit("/", 1)[-1] for r in conn.execute("SELECT path FROM episodes")}
    assert owners == {"a.jsonl"}


def test_episodes_are_indexed_by_path(conn):
    plan = " ".join(
        r[-1] for r in conn.execute("EXPLAIN QUERY PLAN SELECT * FROM episodes WHERE path = 'x'")
    )
    assert "episodes_path" in plan
