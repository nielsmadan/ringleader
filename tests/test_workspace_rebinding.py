import sqlite3
from pathlib import Path

import pytest
from ringleader.sources.base import IndexUpdate
from ringleader.sources.codex import CodexSource, CodexStateError

THREADS_DDL = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    cwd TEXT NOT NULL
)
"""

NULLABLE_DDL = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    cwd TEXT
)
"""

NO_CWD_DDL = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL
)
"""


def _codex_home(tmp_path: Path, monkeypatch, *, ddl=THREADS_DDL, state=True) -> Path:
    home = tmp_path / "codex"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    if state:
        con = sqlite3.connect(home / "state_5.sqlite")
        con.execute(ddl)
        con.commit()
        con.close()
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(sessions))
    return home


def _add_thread(home: Path, sid: str, cwd: str) -> Path:
    rollout = home / "sessions" / f"rollout-{sid}.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    con = sqlite3.connect(home / "state_5.sqlite")
    con.execute(
        "INSERT INTO threads (id, rollout_path, cwd) VALUES (?, ?, ?)", (sid, str(rollout), cwd)
    )
    con.commit()
    con.close()
    return rollout


def _cwd(home: Path, sid: str) -> str | None:
    con = sqlite3.connect(home / "state_5.sqlite")
    row = con.execute("SELECT cwd FROM threads WHERE id = ?", (sid,)).fetchone()
    con.close()
    return row[0] if row else None


def test_rebind_points_the_thread_at_the_new_workspace(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, monkeypatch)
    rollout = _add_thread(home, "sess-1", "/ws/old")

    assert CodexSource().rebind_workspace("sess-1", str(rollout), "/ws/new") is IndexUpdate.UPDATED
    assert _cwd(home, "sess-1") == "/ws/new"


def test_rebind_is_pinned_to_the_transcript_it_names(tmp_path, monkeypatch):
    """A mis-derived id must touch nothing rather than rebind some other thread."""
    home = _codex_home(tmp_path, monkeypatch)
    _add_thread(home, "sess-1", "/ws/old")

    assert (
        CodexSource().rebind_workspace("sess-1", "/somewhere/else.jsonl", "/ws/new")
        is IndexUpdate.NO_ENTRY
    )
    assert _cwd(home, "sess-1") == "/ws/old"


def test_rebind_reports_already_current_when_the_row_is_where_it_belongs(tmp_path, monkeypatch):
    """A re-run must be distinguishable from a failure: `retarget` is documented as
    re-runnable, so reporting the second run as "not rebound" would be a false alarm."""
    home = _codex_home(tmp_path, monkeypatch)
    rollout = _add_thread(home, "sess-1", "/ws/new")

    assert (
        CodexSource().rebind_workspace("sess-1", str(rollout), "/ws/new")
        is IndexUpdate.ALREADY_CURRENT
    )
    assert _cwd(home, "sess-1") == "/ws/new"


def test_rebind_leaves_other_threads_alone(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, monkeypatch)
    rollout = _add_thread(home, "sess-1", "/ws/old")
    _add_thread(home, "sess-2", "/ws/old")

    CodexSource().rebind_workspace("sess-1", str(rollout), "/ws/new")
    assert _cwd(home, "sess-2") == "/ws/old"


def test_rebind_refuses_a_schema_without_cwd(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, monkeypatch, ddl=NO_CWD_DDL)
    con = sqlite3.connect(home / "state_5.sqlite")
    con.execute("INSERT INTO threads (id, rollout_path) VALUES ('sess-1', '/x.jsonl')")
    con.commit()
    con.close()

    with pytest.raises(CodexStateError, match=r"threads\.cwd"):
        CodexSource().rebind_workspace("sess-1", "/x.jsonl", "/ws/new")


def test_rebind_reports_no_index_without_a_state_db(tmp_path, monkeypatch):
    _codex_home(tmp_path, monkeypatch, state=False)
    assert CodexSource().rebind_workspace("sess-1", "/x.jsonl", "/ws/new") is IndexUpdate.NO_INDEX


def test_rebind_matches_a_row_whose_cwd_is_null(tmp_path, monkeypatch):
    """`!=` would read NULL as unchanged and skip the row; `IS NOT` rebinds it."""
    home = _codex_home(tmp_path, monkeypatch, ddl=NULLABLE_DDL)
    rollout = _add_thread(home, "sess-1", None)

    assert CodexSource().rebind_workspace("sess-1", str(rollout), "/ws/new") is IndexUpdate.UPDATED
    assert _cwd(home, "sess-1") == "/ws/new"
