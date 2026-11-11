import json
import sqlite3

from ringleader.commands import relocate
from ringleader.gitinfo import GitProject
from ringleader.slug import cursor_chat_key, cursor_slug, normalize, slugify


def test_relocate_preserves_raw_clone_url(tmp_path, monkeypatch):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({"version": 2, "threads": [{"sessionId": "s1"}]}))
    repo = tmp_path / "repo"
    repo.mkdir()
    clone_url = "ssh://git@gitlab.example.com:2222/team/service.git"
    monkeypatch.setattr(
        relocate.gitinfo,
        "resolve_git_project",
        lambda _: GitProject(
            root=str(repo),
            remote="gitlab.example.com/team/service",
            key="gitlab.example.com/team/service",
            label="service",
            clone_url=clone_url,
        ),
    )

    assert relocate.cmd_relocate(["s1", str(repo), "--registry", str(registry_path)]) == 0
    assert json.loads(registry_path.read_text())["threads"][0]["gitCloneUrl"] == clone_url


def _codex_world(tmp_path, monkeypatch):
    """A codex session recorded at <tmp>/ws, with its own state db."""
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(home / "sessions"))
    ws, dest = tmp_path / "ws", tmp_path / "dest"
    ws.mkdir()
    dest.mkdir()
    rollout = home / "sessions" / "rollout-s1.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    con = sqlite3.connect(home / "state_5.sqlite")
    con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, cwd TEXT)")
    con.execute("INSERT INTO threads VALUES (?, ?, ?)", ("s1", str(rollout), str(ws)))
    con.commit()
    con.close()
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [
                    {
                        "sessionId": "s1",
                        "file": str(rollout),
                        "project": str(ws),
                        "source": "codex",
                        "mtime": "2026-09-01T00:00:00+00:00",
                    }
                ],
                "archived": [],
                "deleted": [],
            }
        )
    )
    return home, dest, reg


def _db_cwd(home):
    con = sqlite3.connect(home / "state_5.sqlite")
    try:
        return con.execute("SELECT cwd FROM threads WHERE id = 's1'").fetchone()[0]
    finally:
        con.close()


def test_relocate_rebinds_a_codex_session_in_the_picker(tmp_path, monkeypatch):
    """`codex resume` filters its picker on threads.cwd, so a registry-only
    relocate leaves the session listed under a directory it no longer lives in."""
    home, dest, reg = _codex_world(tmp_path, monkeypatch)

    assert relocate.cmd_relocate(["s1", str(dest), "--registry", str(reg)]) == 0

    assert _db_cwd(home) == str(dest)
    assert json.loads(reg.read_text())["threads"][0]["project"] == str(dest)


def test_relocate_moves_a_claude_transcript(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(projects))
    ws, dest = tmp_path / "ws", tmp_path / "dest"
    ws.mkdir()
    dest.mkdir()
    slug_dir = projects / slugify(normalize(str(ws)))
    slug_dir.mkdir(parents=True)
    transcript = slug_dir / "s1.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"hi"}}\n')
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [
                    {
                        "sessionId": "s1",
                        "file": str(transcript),
                        "project": str(ws),
                        "source": "claude-code",
                        "mtime": "2026-09-01T00:00:00+00:00",
                    }
                ],
                "archived": [],
                "deleted": [],
            }
        )
    )

    assert relocate.cmd_relocate(["s1", str(dest), "--registry", str(reg)]) == 0

    moved = projects / slugify(normalize(str(dest))) / "s1.jsonl"
    assert moved.exists() and not transcript.exists()
    assert json.loads(reg.read_text())["threads"][0]["file"] == str(moved)


def test_relocate_normalizes_a_relative_destination(tmp_path, monkeypatch):
    """The path is pinned into the registry and written into codex's `threads.cwd`,
    which `codex resume` matches literally — a relative one matches nothing."""
    home, dest, reg = _codex_world(tmp_path, monkeypatch)
    monkeypatch.chdir(dest.parent)

    assert relocate.cmd_relocate(["s1", dest.name, "--registry", str(reg)]) == 0

    assert _db_cwd(home) == str(dest)
    assert json.loads(reg.read_text())["threads"][0]["project"] == str(dest)


def test_relocate_is_idempotent(tmp_path, monkeypatch):
    """Re-running is the documented recovery after a partial failure, so the second
    pass must not trip over the transcript the first one moved."""
    projects = tmp_path / "projects"
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(projects))
    ws, dest = tmp_path / "ws", tmp_path / "dest"
    ws.mkdir()
    dest.mkdir()
    slug_dir = projects / slugify(normalize(str(ws)))
    slug_dir.mkdir(parents=True)
    transcript = slug_dir / "s1.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"hi"}}\n')
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [
                    {
                        "sessionId": "s1",
                        "file": str(transcript),
                        "project": str(ws),
                        "source": "claude-code",
                        "mtime": "2026-09-01T00:00:00+00:00",
                    }
                ],
                "archived": [],
                "deleted": [],
            }
        )
    )
    moved = projects / slugify(normalize(str(dest))) / "s1.jsonl"

    assert relocate.cmd_relocate(["s1", str(dest), "--registry", str(reg)]) == 0
    assert relocate.cmd_relocate(["s1", str(dest), "--registry", str(reg)]) == 0

    assert moved.exists()
    assert json.loads(reg.read_text())["threads"][0]["file"] == str(moved)


def test_relocate_refuses_when_the_source_has_no_index(tmp_path, monkeypatch):
    """Pinning a thread the index never learned about puts it beyond the reach of
    `rl workspace retarget`, which finds sessions by their old path."""
    home, dest, reg = _codex_world(tmp_path, monkeypatch)
    (home / "state_5.sqlite").unlink()
    before = reg.read_text()

    assert relocate.cmd_relocate(["s1", str(dest), "--registry", str(reg)]) == 1
    assert reg.read_text() == before


def test_registry_only_leaves_storage_alone(tmp_path, monkeypatch):
    home, dest, reg = _codex_world(tmp_path, monkeypatch)
    ws = tmp_path / "ws"

    assert relocate.cmd_relocate(["s1", str(dest), "--registry-only", "--registry", str(reg)]) == 0

    assert _db_cwd(home) == str(ws)
    assert json.loads(reg.read_text())["threads"][0]["project"] == str(dest)


def test_relocating_a_cursor_session_twice_is_a_no_op_the_second_time(tmp_path):
    """The second run finds the transcript already filed under the destination's slug,
    which sits two levels above the transcript's own folder."""
    store, ws, dest = tmp_path / "_cursor", tmp_path / "ws", tmp_path / "dest"
    ws.mkdir()
    dest.mkdir()
    chat = store / "chats" / cursor_chat_key(str(ws)) / "c1"
    chat.mkdir(parents=True)
    (chat / "meta.json").write_text(json.dumps({"hasConversation": True, "cwd": str(ws)}))
    folder = store / "projects" / cursor_slug(str(ws)) / "agent-transcripts" / "c1"
    folder.mkdir(parents=True)
    (folder / "c1.jsonl").write_text("{}\n")
    thread = {
        "sessionId": "c1",
        "file": str(folder / "c1.jsonl"),
        "project": str(ws),
        "source": "cursor",
        "mtime": "2026-11-11T00:00:00+00:00",
    }
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 2, "threads": [thread], "archived": [], "deleted": []}))
    moved = store / "projects" / cursor_slug(str(dest)) / "agent-transcripts" / "c1" / "c1.jsonl"

    assert relocate.cmd_relocate(["c1", str(dest), "--registry", str(reg)]) == 0
    assert relocate.cmd_relocate(["c1", str(dest), "--registry", str(reg)]) == 0

    assert json.loads(reg.read_text())["threads"][0]["file"] == str(moved)
    assert (store / "chats" / cursor_chat_key(str(dest)) / "c1" / "meta.json").is_file()
