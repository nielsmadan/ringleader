import json
import sqlite3

import pytest
from ringleader import export, refresh, registry, scanner
from ringleader.scanner import scan
from ringleader.sources.opencode import OpenCodeSource

NOW = "2026-08-30T20:00:00+00:00"


def _create_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE project (
            id TEXT PRIMARY KEY,
            worktree TEXT NOT NULL
        );
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            parent_id TEXT,
            slug TEXT NOT NULL,
            directory TEXT NOT NULL,
            title TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            time_archived INTEGER
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    connection.execute("INSERT INTO project VALUES (?, ?)", ("opaque-id", "/repo"))
    connection.commit()
    connection.close()
    return path


def _session(
    db,
    session_id="ses_live",
    *,
    parent_id=None,
    archived=None,
    title="Visible title",
    slug="shiny-star",
):
    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            "opaque-id",
            parent_id,
            slug,
            "/repo/worktree",
            title,
            1770998000000,
            1770998100000,
            archived,
        ),
    )
    connection.commit()
    connection.close()


def _message(db, session_id, message_id, role, timestamp, text):
    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        (message_id, session_id, timestamp, timestamp, json.dumps({"role": role})),
    )
    part_data = "{broken" if text is None else json.dumps({"type": "text", "text": text})
    connection.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        (f"part_{message_id}", message_id, session_id, timestamp + 1, timestamp + 1, part_data),
    )
    connection.commit()
    connection.close()


def test_discover_returns_live_top_level_rows_with_source_owned_identity(tmp_path):
    db = _create_db(tmp_path / "opencode.db")
    _session(db)
    _session(db, "ses_child", parent_id="ses_live")
    _session(db, "ses_archived", archived=1770999000000)

    refs = list(OpenCodeSource(db_path=db).discover())

    assert [ref.source_ref for ref in refs] == ["ses_live"]
    assert refs[0].file is None
    assert refs[0].fingerprint.startswith("opencode:")


def test_parse_populates_db_metadata_and_bounded_conversation_tail(tmp_path):
    db = _create_db(tmp_path / "opencode.db")
    _session(db, title="")
    _message(db, "ses_live", "msg_2", "assistant", 1770998300000, "answer")
    _message(db, "ses_live", "msg_1", "user", 1770998200000, "first prompt")
    _message(db, "ses_live", "msg_3", "user", 1770998400000, "x" * 7000 + "LATEST")
    _message(
        db,
        "ses_live",
        "msg_4",
        "assistant",
        1770998500000,
        None,
    )

    source = OpenCodeSource(db_path=db, tail_turns=2)
    ref = next(source.discover())
    parsed = source.parse(ref)

    assert parsed.started_at == "2026-02-13T15:53:20+00:00"
    assert parsed.session_id == "ses_live"
    assert parsed.source_ref == "ses_live"
    assert parsed.file is None
    assert parsed.cwd == "/repo/worktree"
    assert parsed.custom_title == "shiny-star"
    assert parsed.message_count == 4
    assert parsed.last_prompt == "x" * 500
    assert len(parsed.last_prompt) == 500
    assert len(parsed.tail_text) <= 6000
    assert parsed.tail_text.endswith("LATEST")
    assert parsed.size_bytes > 7000
    assert parsed.mtime.startswith("2026-")


def test_scan_cache_reuses_unchanged_row_and_invalidates_on_content_change(tmp_path, monkeypatch):
    db = _create_db(tmp_path / "opencode.db")
    _session(db)
    _session(db, "ses_other")
    _message(db, "ses_live", "msg_1", "user", 1770998200000, "first")
    _message(db, "ses_other", "msg_2", "user", 1770998200000, "other")
    source = OpenCodeSource(db_path=db)
    real_parse = source.parse
    parsed = []

    def record_parse(ref):
        parsed.append(ref.source_ref)
        return real_parse(ref)

    monkeypatch.setattr(source, "parse", record_parse)
    first = scan(sources=[source], git_resolver=lambda cwd: None)
    reg = registry.reconcile(registry.empty_registry(), first, NOW)
    assert sorted(parsed) == ["ses_live", "ses_other"]
    parsed.clear()

    second = scan(sources=[source], cached_threads=reg["threads"], git_resolver=lambda cwd: None)

    assert parsed == []
    assert {session.session_id: session.last_prompt for session in second} == {
        "ses_live": "first",
        "ses_other": "other",
    }
    assert {thread["sourceRef"] for thread in reg["threads"]} == {"ses_live", "ses_other"}
    assert all(thread["file"] is None for thread in reg["threads"])

    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE part SET data = ?, time_updated = ? WHERE id = ?",
        (
            json.dumps({"type": "text", "text": "later"}),
            1770999000000,
            "part_msg_1",
        ),
    )
    connection.execute(
        "UPDATE session SET time_updated = ? WHERE id = ?",
        (1770999000000, "ses_live"),
    )
    connection.commit()
    connection.close()

    third = scan(sources=[source], cached_threads=reg["threads"], git_resolver=lambda cwd: None)

    assert parsed == ["ses_live"]
    assert {session.session_id: session.last_prompt for session in third} == {
        "ses_live": "later",
        "ses_other": "other",
    }


def test_session_disappearing_during_scan_does_not_abort_later_rows(tmp_path, monkeypatch):
    db = _create_db(tmp_path / "opencode.db")
    _session(db, "ses_a")
    _session(db, "ses_b")
    source = OpenCodeSource(db_path=db)
    refs = list(source.discover())
    connection = sqlite3.connect(db)
    connection.execute("DELETE FROM session WHERE id = ?", ("ses_a",))
    connection.commit()
    connection.close()
    monkeypatch.setattr(source, "discover", lambda: iter(refs))
    unavailable_sources = set()

    sessions = scan(
        sources=[source],
        git_resolver=lambda cwd: None,
        unavailable_sources=unavailable_sources,
    )

    assert [session.session_id for session in sessions] == ["ses_b"]
    assert unavailable_sources == set()


def test_metadata_change_invalidates_fingerprint_without_timestamp_change(tmp_path):
    db = _create_db(tmp_path / "opencode.db")
    _session(db, title="Before")
    source = OpenCodeSource(db_path=db)
    before = next(source.discover()).fingerprint
    connection = sqlite3.connect(db)
    connection.execute("UPDATE session SET title = ? WHERE id = ?", ("After!", "ses_live"))
    connection.commit()
    connection.close()

    after = next(source.discover()).fingerprint

    assert after != before


def test_render_conversation_routes_through_opencode_and_preserves_order(tmp_path):
    db = _create_db(tmp_path / "opencode.db")
    _session(db)
    _message(db, "ses_live", "msg_assistant", "assistant", 1770998300000, "second")
    _message(db, "ses_live", "msg_user", "user", 1770998200000, "first")
    source = OpenCodeSource(db_path=db)
    thread = {"source": "opencode", "sourceRef": "ses_live", "file": None}

    rendered = export.render_conversation(thread, source=source)

    assert rendered.index("## USER") < rendered.index("## ASSISTANT")
    assert "first" in rendered
    assert "second" in rendered


def test_render_tail_routes_through_the_native_opencode_renderer(tmp_path):
    db = _create_db(tmp_path / "store" / "opencode.db")
    _session(db)
    _message(db, "ses_live", "msg_user", "user", 1770998200000, "first")
    _message(db, "ses_live", "msg_assistant", "assistant", 1770998300000, "second answer")
    thread = {
        "source": "opencode",
        "sourceRef": "ses_live",
        "sessionId": "ses_live",
        "file": None,
        "nativeRoots": {"store": str(tmp_path / "store")},
    }

    rendered = export.render_conversation(thread)

    assert export.render_tail(thread, 12) == rendered[-12:]
    assert "second answer" in export.render_tail(thread, 100)


def test_available_handles_missing_schema_and_locked_db(tmp_path):
    missing = OpenCodeSource(db_path=tmp_path / "missing.db")
    wrong = tmp_path / "wrong.db"
    sqlite3.connect(wrong).close()
    db = _create_db(tmp_path / "opencode.db")
    lock = sqlite3.connect(db)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        assert missing.available() is False
        assert OpenCodeSource(db_path=wrong).available() is False
        assert OpenCodeSource(db_path=db).available() is False
    finally:
        lock.rollback()
        lock.close()


def test_scanner_registers_and_autodetects_opencode(tmp_path, monkeypatch):
    xdg = tmp_path / "xdg"
    db = _create_db(xdg / "opencode" / "opencode.db")
    _session(db)
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(tmp_path / "no-claude"))
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(tmp_path / "no-codex"))

    assert OpenCodeSource in scanner.SOURCE_TYPES
    assert scanner.source_by_name("opencode").name == "opencode"
    assert {source.name for source in scanner.active_sources()} == {"opencode"}


def test_bad_opencode_store_does_not_abort_other_source_scans(tmp_path):
    from ringleader.sources.claude_code import ClaudeCodeSource

    wrong = tmp_path / "wrong.db"
    sqlite3.connect(wrong).close()
    project = tmp_path / "claude" / "project"
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text(
        json.dumps({"type": "user", "cwd": "/repo", "message": {"content": "hello"}}) + "\n"
    )

    unavailable_sources = set()
    sessions = scan(
        sources=[OpenCodeSource(db_path=wrong), ClaudeCodeSource(projects_dir=tmp_path / "claude")],
        git_resolver=lambda cwd: None,
        unavailable_sources=unavailable_sources,
    )

    assert [session.source for session in sessions] == ["claude-code"]
    assert unavailable_sources == {"opencode"}


def test_locked_store_preserves_cached_sessions_during_auto_refresh(tmp_path, monkeypatch):
    xdg = tmp_path / "xdg"
    db = _create_db(xdg / "opencode" / "opencode.db")
    _session(db)
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(tmp_path / "no-claude"))
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(tmp_path / "no-codex"))

    refresh.auto_refresh(
        registry_path,
        NOW,
        git_resolver=lambda cwd: None,
    )
    lock = sqlite3.connect(db)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        reg, note = refresh.auto_refresh(
            registry_path,
            "2026-08-30T20:01:00+00:00",
            git_resolver=lambda cwd: None,
        )
    finally:
        lock.rollback()
        lock.close()

    assert [thread["sessionId"] for thread in reg["threads"]] == ["ses_live"]
    assert reg["deleted"] == []
    assert note is None


def test_discover_sizes_sessions_in_payload_bytes_and_sees_a_new_message(tmp_path):
    db = _create_db(tmp_path / "opencode.db")
    _session(db)
    _message(db, "ses_live", "msg_1", "user", 1770998200000, "placeholder")
    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE part SET data = ? WHERE id = 'part_msg_1'",
        (json.dumps({"type": "text", "text": "é" * 100}, ensure_ascii=False),),
    )
    connection.commit()
    source = OpenCodeSource(db_path=db)
    before = next(source.discover())
    payloads = connection.execute(
        "SELECT data FROM message UNION ALL SELECT data FROM part"
    ).fetchall()
    connection.close()
    _message(db, "ses_live", "msg_2", "user", 1770998200000, "again")

    after = next(source.discover())

    assert before.size_bytes == sum(len(data.encode()) for (data,) in payloads)
    assert after.fingerprint != before.fingerprint


@pytest.mark.parametrize("created", [-1, "bad", 0, 1e100])
def test_invalid_creation_time_is_unknown(tmp_path, created):
    db = _create_db(tmp_path / "opencode.db")
    _session(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute("UPDATE session SET time_created = ?", (created,))
        connection.commit()
    finally:
        connection.close()
    source = OpenCodeSource(db_path=db)
    parsed = source.parse(next(source.discover()))
    assert parsed.started_at is None
