import json
import sqlite3
from pathlib import Path

import pytest
from ringleader import prune, scanner
from ringleader.commands import delete
from ringleader.sources.codex import CodexSource, CodexStateError

NOW = "2026-08-16T00:00:00+00:00"

THREADS_DDL = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER
)
"""


def _codex_home(tmp_path: Path, monkeypatch, *, state_versions=(5,), ddl=THREADS_DDL) -> Path:
    home = tmp_path / "codex"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    for version in state_versions:
        con = sqlite3.connect(home / f"state_{version}.sqlite")
        con.execute(ddl)
        con.commit()
        con.close()
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(sessions))
    return home


def _add_thread(home: Path, sid: str, *, version=5, archived=0) -> Path:
    rollout = home / "sessions" / f"rollout-{sid}.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    con = sqlite3.connect(home / f"state_{version}.sqlite")
    con.execute(
        "INSERT INTO threads (id, rollout_path, archived) VALUES (?, ?, ?)",
        (sid, str(rollout), archived),
    )
    con.commit()
    con.close()
    return rollout


def _archived(home: Path, sid: str, *, version=5):
    con = sqlite3.connect(home / f"state_{version}.sqlite")
    row = con.execute("SELECT archived, archived_at FROM threads WHERE id = ?", (sid,)).fetchone()
    con.close()
    return row


def _thread(sid: str, file: str, source="codex") -> dict:
    return {"sessionId": sid, "file": file, "source": source}


def _reg(threads):
    return {"version": 1, "lastScan": NOW, "threads": list(threads), "tombstones": []}


def test_hiding_sets_the_archived_flag_codex_resume_filters_on(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, monkeypatch)
    rollout = _add_thread(home, "sess-1")

    assert CodexSource().hide_from_picker("sess-1", str(rollout)) is True

    archived, archived_at = _archived(home, "sess-1")
    assert archived == 1
    assert archived_at > 0


def test_hiding_reports_false_when_the_thread_is_already_archived(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, monkeypatch)
    rollout = _add_thread(home, "sess-1", archived=1)

    assert CodexSource().hide_from_picker("sess-1", str(rollout)) is False


def test_hiding_reports_false_for_a_session_codex_has_no_row_for(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, monkeypatch)
    _add_thread(home, "sess-1")

    assert CodexSource().hide_from_picker("other", str(home)) is False


def test_hiding_uses_the_newest_state_db_when_codex_has_migrated(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, monkeypatch, state_versions=(5, 12))
    _add_thread(home, "sess-1", version=5)
    rollout = _add_thread(home, "sess-1", version=12)

    CodexSource().hide_from_picker("sess-1", str(rollout))

    assert _archived(home, "sess-1", version=12)[0] == 1
    assert _archived(home, "sess-1", version=5)[0] == 0


def test_hiding_raises_when_codex_schema_no_longer_has_the_flag(tmp_path, monkeypatch):
    home = _codex_home(
        tmp_path,
        monkeypatch,
        ddl="CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)",
    )
    _add_thread_ddl = sqlite3.connect(home / "state_5.sqlite")
    _add_thread_ddl.execute("INSERT INTO threads VALUES ('sess-1', '/x.jsonl')")
    _add_thread_ddl.commit()
    _add_thread_ddl.close()

    with pytest.raises(CodexStateError, match="schema moved"):
        CodexSource().hide_from_picker("sess-1", "/x.jsonl")


def test_hiding_refuses_to_touch_a_thread_the_transcript_does_not_belong_to(tmp_path, monkeypatch):
    # the id/path pair disagreeing is what a mis-derived session id looks like
    home = _codex_home(tmp_path, monkeypatch)
    _add_thread(home, "parent")
    other = _add_thread(home, "child")

    assert CodexSource().hide_from_picker("parent", str(other)) is False
    assert _archived(home, "parent")[0] == 0


def test_sources_without_an_index_are_left_alone(tmp_path, monkeypatch):
    _codex_home(tmp_path, monkeypatch)

    assert scanner.hide_from_picker(_thread("sess-1", "/x.jsonl", source="claude-code")) is False


def test_prune_clears_the_picker_entry_before_deleting_the_transcript(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, monkeypatch)
    rollout = _add_thread(home, "sess-1")
    reg = _reg([_thread("sess-1", str(rollout))])

    assert prune.delete_sessions(reg, list(reg["threads"]), NOW) == 1

    assert not rollout.exists()
    assert _archived(home, "sess-1")[0] == 1


def test_session_delete_clears_the_picker_entry_too(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, monkeypatch)
    rollout = _add_thread(home, "sess-1")
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(json.dumps(_reg([_thread("sess-1", str(rollout))])), encoding="utf-8")

    assert delete.cmd_delete(["sess-1", "--registry", str(reg_path)]) == 0

    assert not rollout.exists()
    assert _archived(home, "sess-1")[0] == 1


def test_prune_keeps_the_transcript_when_the_picker_entry_cannot_be_cleared(tmp_path, monkeypatch):
    home = _codex_home(
        tmp_path,
        monkeypatch,
        ddl="CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)",
    )
    rollout = home / "sessions" / "rollout-sess-1.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    reg = _reg([_thread("sess-1", str(rollout))])

    assert prune.delete_sessions(reg, list(reg["threads"]), NOW) == 0

    assert rollout.exists()
    assert reg.get("deleted", []) == []
    assert [t["sessionId"] for t in reg["threads"]] == ["sess-1"]
