import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict

import pytest
from ringleader.paths import local_usage_path
from ringleader.usage.checkpoint import VERSION, Checkpoint
from ringleader.usage.claude import ClaudeUsageReader
from ringleader.usage.codex import CodexUsageReader
from ringleader.usage.droid import DroidUsageReader
from ringleader.usage.opencode import OpenCodeUsageReader
from ringleader.usage.pi import PiUsageReader

from test_usage_claude import _assistant, _write
from test_usage_codex import _token_count
from test_usage_codex import _write as _codex_file
from test_usage_droid import _session, _sidecar
from test_usage_opencode_pi import _oc_db, _oc_session
from test_usage_window_pushdown import _pi_file


def _single(reader):
    (record,) = reader.records()
    return record


def _cache_path():
    return local_usage_path(VERSION)


@pytest.mark.parametrize("source", ["claude", "codex", "pi", "droid", "opencode"])
def test_new_reader_reuses_checkpoint_without_parsing(tmp_path, monkeypatch, source):
    if source == "claude":
        _write(tmp_path, "project", "session", [_assistant("m", "r")])
        cls, kwargs, parser = ClaudeUsageReader, {"projects_dir": tmp_path}, "_file_rows"
    elif source == "codex":
        _codex_file(tmp_path, [_token_count(10, 0, 0, 2, 0, ts="2026-08-17T10:00:00Z")])
        cls, kwargs, parser = CodexUsageReader, {"sessions_dir": tmp_path}, "_records"
    elif source == "pi":
        _pi_file(tmp_path, "session", "2026-08-17")
        cls, kwargs, parser = PiUsageReader, {"sessions_dir": tmp_path}, "_file_records"
    elif source == "droid":
        _session(tmp_path, "session")
        cls, kwargs, parser = DroidUsageReader, {"sessions_dir": tmp_path}, None
    else:
        db = _oc_db(tmp_path)
        _oc_session(db)
        cls, kwargs, parser = OpenCodeUsageReader, {"db_path": db}, "_records"
    expected = [asdict(record) for record in cls(**kwargs).records()]
    cls(**kwargs).latest_day()
    assert expected

    def refuse(*args, **kwargs):
        pytest.fail("unchanged usage was reparsed")

    if parser is None:
        monkeypatch.setattr("ringleader.usage.droid._thread_rows", refuse)
    else:
        monkeypatch.setattr(cls, parser, refuse)
        if source == "opencode":
            monkeypatch.setattr(cls, "_latest_records", refuse)
    reader = cls(**kwargs)
    assert [asdict(record) for record in reader.records()] == expected
    assert reader.latest_day() == max(record["timestamp"][:10] for record in expected)


def test_only_changed_and_new_files_are_parsed_and_deletions_disappear(tmp_path, monkeypatch):
    first = _write(tmp_path, "project", "a", [_assistant("m1", "r1")])
    second = _write(tmp_path, "project", "b", [_assistant("m2", "r2")])
    expected = list(ClaudeUsageReader(projects_dir=tmp_path).records())
    parsed = []
    original = ClaudeUsageReader._file_rows

    def track(self, path):
        parsed.append(path.name)
        yield from original(self, path)

    monkeypatch.setattr(ClaudeUsageReader, "_file_rows", track)
    first.write_text(first.read_text() + json.dumps(_assistant("m3", "r3")) + "\n")
    _write(tmp_path, "project", "c", [_assistant("m4", "r4")])
    records = list(ClaudeUsageReader(projects_dir=tmp_path).records())
    assert parsed == ["a.jsonl", "c.jsonl"]
    assert sum(r.total for r in records) == 2 * sum(r.total for r in expected)
    second.unlink()
    parsed.clear()
    assert len(list(ClaudeUsageReader(projects_dir=tmp_path).records())) == 3
    assert parsed == []
    with closing(sqlite3.connect(_cache_path())) as connection:
        assert connection.execute("SELECT count(*) FROM entries").fetchone()[0] == 2


def test_dedupe_is_recomputed_across_cached_and_changed_files(tmp_path):
    _write(tmp_path, "project", "a", [_assistant("m", "r")])
    other = _write(tmp_path, "project", "b", [_assistant("m", "r")])
    assert [r.session_id for r in ClaudeUsageReader(projects_dir=tmp_path).records()] == ["a"]
    _write(tmp_path, "project", "a", [_assistant("new", "new")])
    records = list(ClaudeUsageReader(projects_dir=tmp_path).records())
    assert [r.session_id for r in records] == ["a", other.stem]


def test_same_size_rewrite_with_restored_mtime_invalidates(tmp_path):
    path = _write(tmp_path, "project", "a", [_assistant("m", "r", inp=100)])
    before = path.stat()
    assert _single(ClaudeUsageReader(projects_dir=tmp_path)).input == 100
    path.write_text(path.read_text().replace('"input_tokens": 100', '"input_tokens": 200'))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert _single(ClaudeUsageReader(projects_dir=tmp_path)).input == 200


def test_sidecar_only_update_invalidates_droid_checkpoint(tmp_path):
    transcript = _session(tmp_path, "session")
    original = _single(DroidUsageReader(sessions_dir=tmp_path))
    sidecar = _sidecar(transcript)
    data = json.loads(sidecar.read_text())
    data["tokenUsage"]["inputTokens"] = original.input + 17
    sidecar.write_text(json.dumps(data))
    assert _single(DroidUsageReader(sessions_dir=tmp_path)).input == original.input + 17


def test_wal_only_update_invalidates_opencode_checkpoint(tmp_path):
    db = _oc_db(tmp_path)
    _oc_session(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        first = _single(OpenCodeUsageReader(db_path=db))
        stamp = db.stat().st_mtime_ns
        raw = json.loads(connection.execute("SELECT data FROM message").fetchone()[0])
        raw["tokens"]["input"] = first.input + 23
        connection.execute("UPDATE message SET data=?", (json.dumps(raw),))
        connection.commit()
        assert db.stat().st_mtime_ns == stamp
        assert _single(OpenCodeUsageReader(db_path=db)).input == first.input + 23
    finally:
        connection.close()


def test_changed_during_read_is_not_checkpointed(tmp_path):
    path = tmp_path / "session"
    path.write_text("old")
    control = tmp_path / "stable"
    control.write_text("stable")
    calls = []

    def read():
        calls.append(True)
        value = path.read_text()
        path.write_text("new")
        return [{"value": value}]

    with Checkpoint("fixture", tmp_path, [path, control]) as checkpoint:
        assert list(checkpoint.rows(control, lambda: [{"value": "stable"}])) == [
            {"value": "stable"}
        ]
        assert list(checkpoint.rows(path, read)) == [{"value": "old"}]
    with closing(sqlite3.connect(_cache_path())) as connection:
        assert [json.loads(row[0]) for row in connection.execute("SELECT key FROM entries")] == [
            str(control)
        ]
    with Checkpoint("fixture", tmp_path, [path]) as checkpoint:
        assert list(checkpoint.rows(path, read)) == [{"value": "new"}]
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["database", "payload", "locked", "unwritable"])
def test_cache_failures_preserve_usage(tmp_path, failure):
    _write(tmp_path, "project", "a", [_assistant("m", "r")])
    expected = list(ClaudeUsageReader(projects_dir=tmp_path).records())
    path = _cache_path()
    connection = None
    if failure == "database":
        path.write_bytes(b"not a sqlite database")
    elif failure == "unwritable":
        path.unlink()
        path.mkdir()
    else:
        connection = sqlite3.connect(path)
        if failure == "payload":
            connection.execute("UPDATE entries SET payload='[]'")
            connection.commit()
        else:
            connection.execute("BEGIN EXCLUSIVE")
    try:
        assert list(ClaudeUsageReader(projects_dir=tmp_path).records()) == expected
    finally:
        if connection is not None:
            connection.close()


def test_codex_window_checkpoint_reuses_and_refreshes(tmp_path, monkeypatch):
    limits = {"primary": {"used_percent": 10, "window_minutes": 10080, "resets_at": 1787213354}}
    event = _token_count(10, 0, 0, 2, 0, ts="2026-08-17T10:00:00Z", rate_limits=limits)
    path = _codex_file(tmp_path, [event])
    (first,) = CodexUsageReader(sessions_dir=tmp_path).latest_windows()
    reads = []
    original = CodexUsageReader._file_payloads

    def track(self, path):
        reads.append(path)
        yield from original(self, path)

    monkeypatch.setattr(CodexUsageReader, "_file_payloads", track)
    assert CodexUsageReader(sessions_dir=tmp_path).latest_windows() == [first]
    assert reads == []
    event["payload"]["rate_limits"]["primary"]["used_percent"] = 30
    path.write_text(json.dumps(event) + "\n")
    (updated,) = CodexUsageReader(sessions_dir=tmp_path).latest_windows()
    assert updated.used_percent == 30
    assert reads == [path]


def test_opencode_filter_changes_do_not_reuse_a_different_query(tmp_path):
    from datetime import date

    db = _oc_db(tmp_path)
    _oc_session(db, session="first")
    _oc_session(db, session="second")
    assert len(list(OpenCodeUsageReader(db_path=db).records())) == 2
    assert [
        record.session_id
        for record in OpenCodeUsageReader(db_path=db).records(session_prefix="first")
    ] == ["first"]
    assert list(OpenCodeUsageReader(db_path=db).records(since=date(2030, 1, 1))) == []
    assert len(list(OpenCodeUsageReader(db_path=db).records())) == 2


def test_source_roots_have_independent_checkpoint_ownership(tmp_path):
    source = tmp_path / "source"
    archive = tmp_path / "archive"
    _write(source, "project", "live", [_assistant("m1", "r1")])
    _write(archive, "project", "saved", [_assistant("m2", "r2")])
    assert [record.session_id for record in ClaudeUsageReader(projects_dir=source).records()] == [
        "live"
    ]
    assert [record.session_id for record in ClaudeUsageReader(projects_dir=archive).records()] == [
        "saved"
    ]
    assert [record.session_id for record in ClaudeUsageReader(projects_dir=source).records()] == [
        "live"
    ]
    with closing(sqlite3.connect(_cache_path())) as connection:
        assert connection.execute("SELECT count(*) FROM entries").fetchone()[0] == 2


@pytest.mark.parametrize("failure", ["malformed", "unreadable"])
def test_droid_failed_sidecar_preserves_first_copy_ownership(tmp_path, monkeypatch, failure):
    from pathlib import Path

    first = _session(tmp_path, "session", directory="a", usage={"inputTokens": 10})
    _session(tmp_path, "session", directory="b", usage={"inputTokens": 99})
    sidecar = _sidecar(first)
    cache = _cache_path()
    with monkeypatch.context() as fault:
        if failure == "malformed":
            sidecar.write_text("{")
        else:
            original = Path.read_text

            def read(path, *args, **kwargs):
                if path == sidecar:
                    raise OSError("temporarily unreadable")
                return original(path, *args, **kwargs)

            fault.setattr(Path, "read_text", read)
        assert list(DroidUsageReader(sessions_dir=tmp_path).records()) == []
    assert _cache_path() == cache
    if failure == "malformed":
        sidecar.write_text(json.dumps({"tokenUsage": {"inputTokens": 10}}))
    assert _single(DroidUsageReader(sessions_dir=tmp_path)).input == 10


def test_codex_reused_reader_refreshes_project_before_checkpointing(tmp_path):
    event = _token_count(10, 0, 0, 2, 0, ts="2026-08-17T10:00:00Z")
    meta = {"type": "session_meta", "payload": {"cwd": "/old"}}
    path = _codex_file(tmp_path, [event], meta=meta)
    reader = CodexUsageReader(sessions_dir=tmp_path)
    assert reader.latest_day() == "2026-08-17"
    path.write_text(path.read_text().replace("/old", "/new"))
    assert _single(reader).project == "/new"
    assert _single(CodexUsageReader(sessions_dir=tmp_path)).project == "/new"


@pytest.mark.parametrize("source", ["codex", "pi"])
def test_relative_source_checkpoint_preserves_absolute_record_identity(
    tmp_path, monkeypatch, source
):
    from pathlib import Path

    root = tmp_path / "sessions"
    if source == "codex":
        path = _codex_file(root, [_token_count(10, 0, 0, 2, 0, ts="2026-08-17T10:00:00Z")])
        cls = CodexUsageReader
    else:
        path = _pi_file(root, "session", "2026-08-17")
        cls = PiUsageReader
    absolute = _single(cls(sessions_dir=root))
    monkeypatch.chdir(tmp_path)
    path.write_text(path.read_text() + "\n")
    relative = _single(cls(sessions_dir=Path("sessions")))
    assert relative.record_id == absolute.record_id.removeprefix(str(tmp_path) + "/")
    assert _single(cls(sessions_dir=root)) == absolute


def test_droid_checkpoint_contains_only_accounting_fields(tmp_path):
    _session(tmp_path, "session")
    list(DroidUsageReader(sessions_dir=tmp_path).records())
    with closing(sqlite3.connect(_cache_path())) as connection:
        (payload,) = connection.execute("SELECT payload FROM entries").fetchone()
    (row,) = json.loads(payload)
    assert set(row) == {"session_id", "record"}
    assert row["session_id"] == "session"
    assert row["record"] == asdict(_single(DroidUsageReader(sessions_dir=tmp_path)))


@pytest.mark.parametrize("via_alias", [False, True])
def test_opencode_wal_refreshes_tokens_and_latest_day(tmp_path, via_alias):
    from ringleader.sources.base import stat_fingerprint

    db = _oc_db(tmp_path)
    _oc_session(db)
    configured = tmp_path / "alias.db" if via_alias else db
    if via_alias:
        configured.symlink_to(db)
    with closing(sqlite3.connect(db)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        reader = OpenCodeUsageReader(db_path=configured)
        before_day = reader.latest_day()
        before = _single(reader)
        stamp = stat_fingerprint(db.stat())
        raw = json.loads(writer.execute("SELECT data FROM message").fetchone()[0])
        raw["tokens"]["input"] += 23
        writer.execute(
            "UPDATE message SET data=?, time_created=time_created+86400000", (json.dumps(raw),)
        )
        writer.commit()
        assert stat_fingerprint(db.stat()) == stamp
        fresh = OpenCodeUsageReader(db_path=configured)
        assert _single(fresh).input == before.input + 23
        assert fresh.latest_day() > before_day


@pytest.mark.parametrize("part", ["head", "project", "windows"])
def test_codex_transient_open_failure_does_not_survive_recovery(tmp_path, monkeypatch, part):
    from pathlib import Path

    from ringleader.sources.base import stat_fingerprint

    from test_usage_codex import _forked_rollout

    if part == "head":
        path = _forked_rollout(tmp_path)
    else:
        limits = {"primary": {"used_percent": 10, "window_minutes": 10080, "resets_at": 1787213354}}
        event = _token_count(10, 0, 0, 2, 0, ts="2026-08-17T10:00:00Z", rate_limits=limits)
        path = _codex_file(
            tmp_path, [event], meta={"type": "session_meta", "payload": {"cwd": "/repo"}}
        )
    original = Path.open
    remaining = 3 if part == "project" else 1
    signature = stat_fingerprint(path.stat())

    def fail_once(candidate, *args, **kwargs):
        nonlocal remaining
        if candidate == path:
            remaining -= 1
            if remaining == 0:
                raise OSError("temporary read failure")
        return original(candidate, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "open", fail_once)
        reader = CodexUsageReader(sessions_dir=tmp_path)
        result = reader.latest_windows() if part == "windows" else list(reader.records())
        if part == "project":
            assert [(record.input, record.project) for record in result] == [(10, None)]
        else:
            assert result == []
    assert stat_fingerprint(path.stat()) == signature
    fresh = CodexUsageReader(sessions_dir=tmp_path)
    if part == "windows":
        assert [window.used_percent for window in fresh.latest_windows()] == [10]
    else:
        record = _single(fresh)
        assert record.input == 10
        if part == "project":
            assert record.project == "/repo"


@pytest.mark.parametrize("method", ["records", "latest_day"])
def test_replay_version_change_invalidates_codex_usage(tmp_path, monkeypatch, method):
    from ringleader.usage import codex

    _codex_file(tmp_path, [_token_count(10, 0, 0, 2, 0, ts="2026-08-17T10:00:00Z")])
    expected = _single(CodexUsageReader(sessions_dir=tmp_path))
    if method == "latest_day":
        assert CodexUsageReader(sessions_dir=tmp_path).latest_day() == expected.timestamp[:10]
    original = CodexUsageReader._records
    reads = []

    def track(self, path):
        reads.append(path)
        yield from original(self, path)

    monkeypatch.setattr(CodexUsageReader, "_records", track)
    monkeypatch.setattr(codex, "REPLAY_VERSION", codex.REPLAY_VERSION + 1)
    reader = CodexUsageReader(sessions_dir=tmp_path)
    if method == "latest_day":
        assert reader.latest_day() == expected.timestamp[:10]
    else:
        assert _single(reader) == expected
    assert len(reads) == 1


@pytest.mark.parametrize("source", ["claude", "codex", "droid", "windows"])
def test_invalid_typed_checkpoint_is_replaced(tmp_path, source):
    import hashlib

    if source == "claude":
        _write(tmp_path, "project", "a", [_assistant("m", "r")])

        def read():
            return list(ClaudeUsageReader(projects_dir=tmp_path).records())
    elif source == "droid":
        _session(tmp_path, "a")

        def read():
            return list(DroidUsageReader(sessions_dir=tmp_path).records())
    else:
        limits = {"primary": {"used_percent": 10, "window_minutes": 10080, "resets_at": 1787213354}}
        _codex_file(
            tmp_path, [_token_count(10, 0, 0, 2, 0, ts="2026-08-17T10:00:00Z", rate_limits=limits)]
        )
        read = (
            (lambda: CodexUsageReader(sessions_dir=tmp_path).latest_windows())
            if source == "windows"
            else (lambda: list(CodexUsageReader(sessions_dir=tmp_path).records()))
        )
    expected = read()
    payload = "[{}]"
    with closing(sqlite3.connect(_cache_path())) as connection:
        connection.execute(
            "UPDATE entries SET payload=?, checksum=?",
            (payload, hashlib.sha256(payload.encode()).hexdigest()),
        )
        connection.commit()
    assert read() == expected
    with closing(sqlite3.connect(_cache_path())) as connection:
        (saved,) = connection.execute("SELECT payload FROM entries").fetchone()
    assert json.loads(saved)[0]
    assert read() == expected


def test_cache_streams_large_results_and_keeps_completed_files(tmp_path, monkeypatch):
    import ringleader.usage.checkpoint as cache

    monkeypatch.setattr(cache, "MAX_PAYLOAD_BYTES", 64)
    small = tmp_path / "small"
    large = tmp_path / "large"
    small.touch()
    large.touch()
    consumed = []

    def rows():
        for i in range(100):
            consumed.append(i)
            yield {"value": "x" * 50, "index": i}

    with Checkpoint("fixture", tmp_path, [small, large]) as checkpoint:
        assert list(checkpoint.rows(small, lambda: [{"value": "small"}])) == [{"value": "small"}]
        stream = checkpoint.rows(large, rows)
        assert next(stream)["index"] == 0
        assert consumed == [0]
        assert len(list(stream)) == 99
    with closing(sqlite3.connect(_cache_path())) as connection:
        assert [json.loads(row[0]) for row in connection.execute("SELECT key FROM entries")] == [
            str(small)
        ]


def test_pending_batches_are_visible_before_scan_finishes(tmp_path, monkeypatch):
    import ringleader.usage.checkpoint as cache

    monkeypatch.setattr(cache, "MAX_PENDING_FILES", 2)
    paths = [tmp_path / str(i) for i in range(3)]
    for path in paths:
        path.touch()
    with Checkpoint("fixture", tmp_path, paths) as checkpoint:
        for path in paths[:2]:
            list(checkpoint.rows(path, lambda: [{"value": "ready"}]))
        with closing(sqlite3.connect(_cache_path())) as connection:
            assert connection.execute("SELECT count(*) FROM entries").fetchone()[0] == 2


def test_source_error_keeps_partial_output_but_not_checkpoint(tmp_path):
    from dataclasses import replace

    path = _write(tmp_path, "project", "a", [_assistant("m", "r")])
    record = _single(ClaudeUsageReader(projects_dir=tmp_path))

    def partial():
        yield replace(record, input=123)
        raise sqlite3.OperationalError("source interrupted")

    with Checkpoint("partial", tmp_path, [path]) as checkpoint:
        assert [r.input for r in checkpoint.records(path, partial)] == [123]
    with closing(sqlite3.connect(_cache_path())) as connection:
        assert connection.execute("SELECT count(*) FROM entries").fetchone()[0] == 1


def test_opencode_reuses_bounded_filter_variants(tmp_path, monkeypatch):
    from datetime import date

    db = _oc_db(tmp_path)
    _oc_session(db)
    filters = [(None, None), (date(2026, 1, 1), None), (None, "ses")]
    for since, prefix in filters:
        list(OpenCodeUsageReader(db_path=db).records(since, prefix))
    original = OpenCodeUsageReader._records
    queries = []

    def track(self, *args):
        queries.append(args)
        yield from original(self, *args)

    monkeypatch.setattr(OpenCodeUsageReader, "_records", track)
    for since, prefix in filters:
        assert len(list(OpenCodeUsageReader(db_path=db).records(since, prefix))) == 1
    assert queries == []
    for index in range(5):
        list(OpenCodeUsageReader(db_path=db).records(session_prefix=f"absent-{index}"))
    with closing(sqlite3.connect(_cache_path())) as connection:
        counts = {
            json.loads(namespace)[0]: count
            for namespace, count in connection.execute(
                "SELECT namespace, count(*) FROM entries GROUP BY namespace"
            )
        }
        assert counts == {"opencode": 2, "opencode-session": 4}
    queries.clear()
    for since, prefix in filters[:2]:
        assert len(list(OpenCodeUsageReader(db_path=db).records(since, prefix))) == 1
    assert queries == []


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "foreign"])
def test_checkpoint_never_changes_foreign_database(tmp_path, kind):
    db = _oc_db(tmp_path)
    _oc_session(db)
    path = _cache_path()
    path.parent.mkdir(parents=True)
    if kind == "symlink":
        path.symlink_to(db)
    elif kind == "hardlink":
        path.hardlink_to(db)
    else:
        path.write_bytes(db.read_bytes())
        db = path
    db.chmod(0o640)
    before = (db.read_bytes(), db.stat().st_mode)
    source = _write(tmp_path / "source", "project", "a", [_assistant("m", "r")])
    assert _single(ClaudeUsageReader(projects_dir=source.parent.parent)).session_id == "a"
    assert (db.read_bytes(), db.stat().st_mode) == before


def test_checkpoint_is_private_before_sqlite_connect(tmp_path, monkeypatch):
    import stat

    path = _cache_path()
    path.parent.mkdir(parents=True, mode=0o755)
    original = sqlite3.connect
    modes = []

    def connect(candidate, *args, **kwargs):
        if candidate == path:
            modes.append(
                (stat.S_IMODE(path.parent.stat().st_mode), stat.S_IMODE(path.stat().st_mode))
            )
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    _write(tmp_path, "project", "a", [_assistant("m", "r")])
    assert _single(ClaudeUsageReader(projects_dir=tmp_path)).session_id == "a"
    assert modes == [(0o700, 0o600)]


def test_overlapping_scans_keep_newer_entries(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("old")
    with Checkpoint("overlap", tmp_path, [first]) as older:
        list(older.rows(first, lambda: [{"value": first.read_text()}]))
        first.write_text("new")
        second.write_text("newer")
        with Checkpoint("overlap", tmp_path, [first, second]) as newer:
            list(newer.rows(first, lambda: [{"value": first.read_text()}]))
            list(newer.rows(second, lambda: [{"value": second.read_text()}]))
    with closing(sqlite3.connect(_cache_path())) as connection:
        assert {
            json.loads(key): json.loads(payload)[0]["value"]
            for key, payload in connection.execute("SELECT key,payload FROM entries")
        } == {str(first): "new", str(second): "newer"}


def test_deleted_payloads_release_disk_pages(tmp_path):
    path = tmp_path / "source"
    path.touch()
    with Checkpoint("vacuum", tmp_path, [path]) as checkpoint:
        list(checkpoint.rows(path, lambda: [{"value": "x" * 500000}]))
    before = _cache_path().stat().st_size
    path.unlink()
    with Checkpoint("vacuum", tmp_path, []):
        pass
    assert _cache_path().stat().st_size < before


def test_surrogate_path_keys_round_trip(tmp_path):
    path = tmp_path / "session-\udcff.jsonl"
    with Checkpoint("surrogate", tmp_path, [path]) as checkpoint:
        assert list(checkpoint.rows(path, lambda: [{"value": "saved"}])) == [{"value": "saved"}]
    with Checkpoint("surrogate", tmp_path, [path]) as checkpoint:
        assert list(checkpoint.rows(path, lambda: pytest.fail("cache missed"))) == [
            {"value": "saved"}
        ]


def test_claude_checkpoint_compacts_duplicate_requests(tmp_path):
    _write(tmp_path, "project", "session", [_assistant("m", "r")] * 1000)
    assert len(list(ClaudeUsageReader(projects_dir=tmp_path).records())) == 1
    with closing(sqlite3.connect(_cache_path())) as connection:
        payload = connection.execute("SELECT payload FROM entries").fetchone()[0]
    assert len(json.loads(payload)) == 1


def test_completed_files_survive_interruption(tmp_path):
    complete = tmp_path / "complete"
    incomplete = tmp_path / "incomplete"
    complete.touch()
    incomplete.touch()

    def interrupted():
        yield {"value": "partial"}
        raise KeyboardInterrupt

    with (
        pytest.raises(KeyboardInterrupt),
        Checkpoint("interrupted", tmp_path, [complete, incomplete]) as checkpoint,
    ):
        list(checkpoint.rows(complete, lambda: [{"value": "complete"}]))
        list(checkpoint.rows(incomplete, interrupted))
    with closing(sqlite3.connect(_cache_path())) as connection:
        assert [
            json.loads(row[0]) for row in connection.execute("SELECT payload FROM entries")
        ] == [[{"value": "complete"}]]


def test_source_type_error_is_not_retried(tmp_path):
    path = tmp_path / "source"
    path.touch()
    calls = []

    def broken():
        calls.append(True)
        raise TypeError("source parser failed")

    with (
        Checkpoint("broken", tmp_path, [path]) as checkpoint,
        pytest.raises(TypeError, match="source parser failed"),
    ):
        list(checkpoint.records(path, broken))
    assert calls == [True]


def test_checkpoint_schema_version_pin():
    import hashlib
    import inspect
    from dataclasses import fields

    from ringleader.usage.base import UsageRecord, WindowSnapshot
    from ringleader.usage.checkpoint import DDL, decode_record
    from ringleader.usage.claude import _Credit, _decode_credit
    from ringleader.usage.codex import _decode_window
    from ringleader.usage.droid import _decode_threads, _Thread

    shape = [DDL]
    for cls in (UsageRecord, WindowSnapshot, _Credit, _Thread):
        shape.append(str([(field.name, str(field.type)) for field in fields(cls)]))
    for decoder in (decode_record, _decode_credit, _decode_window, _decode_threads):
        shape.append(inspect.getsource(decoder))
    digest = hashlib.sha256("\n".join(shape).encode()).hexdigest()[:16]
    assert (VERSION, digest) == (2, "612863ee862f4c48"), (
        "Bump checkpoint VERSION when its persisted shape changes"
    )


def test_droid_returned_records_are_independent(tmp_path):
    _session(tmp_path, "session")
    reader = DroidUsageReader(sessions_dir=tmp_path)
    expected = asdict(_single(reader))
    changed = _single(reader)
    changed.timestamp = "2000-01-01T00:00:00Z"
    changed.input = 99999
    assert asdict(_single(reader)) == expected
    assert reader.latest_day() == expected["timestamp"][:10]


@pytest.mark.parametrize("invalid", [float("nan"), float("inf")])
def test_droid_invalid_excluded_usage_does_not_break_report(tmp_path, invalid):
    _session(tmp_path, "broken", usage={"inputTokens": invalid})
    _session(tmp_path, "good")
    reader = DroidUsageReader(sessions_dir=tmp_path)
    assert [(r.session_id, r.input) for r in reader.records(session_prefix="good")] == [
        ("good", 1200)
    ]


@pytest.mark.parametrize(
    "statement",
    ["PRAGMA auto_vacuum=INCREMENTAL", "PRAGMA application_id=1380734279", "CREATE TABLE"],
)
def test_interrupted_bootstrap_never_publishes_partial_database(tmp_path, monkeypatch, statement):
    original = sqlite3.connect

    class Interrupted(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if sql.startswith(statement):
                raise KeyboardInterrupt
            return result

    def connect(*args, **kwargs):
        return original(*args, factory=Interrupted, **kwargs)

    _write(tmp_path, "project", "a", [_assistant("m", "r")])
    with monkeypatch.context() as fault:
        fault.setattr(sqlite3, "connect", connect)
        with pytest.raises(KeyboardInterrupt):
            _single(ClaudeUsageReader(projects_dir=tmp_path))
    assert not _cache_path().exists()
    assert _single(ClaudeUsageReader(projects_dir=tmp_path)).session_id == "a"
    with closing(original(_cache_path())) as connection:
        assert connection.execute("PRAGMA auto_vacuum").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM entries").fetchone() == (1,)


def test_warm_checkpoint_remains_available_during_concurrent_read(tmp_path, monkeypatch):
    _write(tmp_path, "project", "a", [_assistant("m", "r")])
    expected = _single(ClaudeUsageReader(projects_dir=tmp_path))
    with closing(sqlite3.connect(_cache_path())) as connection:
        version = connection.execute("PRAGMA data_version").fetchone()
        connection.execute("BEGIN")
        connection.execute("SELECT * FROM entries").fetchall()

        def refuse(*args):
            pytest.fail("warm checkpoint reparsed under a reader lock")

        monkeypatch.setattr(ClaudeUsageReader, "_file_rows", refuse)
        assert _single(ClaudeUsageReader(projects_dir=tmp_path)) == expected
        connection.rollback()
        assert connection.execute("PRAGMA data_version").fetchone() == version


def test_existing_empty_foreign_file_is_preserved(tmp_path):
    path = _cache_path()
    path.parent.mkdir(parents=True)
    path.touch(mode=0o640)
    before = path.stat().st_mode
    _write(tmp_path, "project", "a", [_assistant("m", "r")])
    assert _single(ClaudeUsageReader(projects_dir=tmp_path)).session_id == "a"
    assert (path.read_bytes(), path.stat().st_mode) == (b"", before)


def test_shared_writable_state_ancestor_uses_source_reads(tmp_path, monkeypatch):
    data = tmp_path / "shared"
    data.mkdir(mode=0o775)
    data.chmod(0o775)
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    _write(tmp_path / "source", "project", "a", [_assistant("m", "r")])
    assert _single(ClaudeUsageReader(projects_dir=tmp_path / "source")).session_id == "a"
    assert not _cache_path().exists()


def test_private_symlinked_state_root_reuses_checkpoint(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(data, target_is_directory=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(alias))
    _write(tmp_path / "source", "project", "a", [_assistant("m", "r")])
    expected = _single(ClaudeUsageReader(projects_dir=tmp_path / "source"))

    def refuse(*args):
        pytest.fail("private symlinked state should cache")

    monkeypatch.setattr(ClaudeUsageReader, "_file_rows", refuse)
    assert _single(ClaudeUsageReader(projects_dir=tmp_path / "source")) == expected


def test_opencode_unresolvable_path_is_unavailable(tmp_path):
    path = tmp_path / "loop.db"
    path.symlink_to(path)
    reader = OpenCodeUsageReader(db_path=path)
    assert list(reader.records()) == []
    assert reader.latest_day() is None
