import json

import pytest
from ringleader.commands import move_workspace
from ringleader.slug import is_beneath, remap, slugify


def _world(tmp_path, monkeypatch, *, drifted=False, extra_session=False):
    """A workspace at <tmp>/ws with one session, plus optional wrinkles:
    `drifted` puts the transcript in a different slug dir than the cwd implies,
    `extra_session` adds a session in the same slug dir that we will NOT be moving.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    projects = tmp_path / "projects"
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(tmp_path / "no-codex"))
    monkeypatch.setattr(move_workspace, "_git_for", lambda p: None)

    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    launch = ws / "elsewhere" if drifted else ws
    if drifted:
        launch.mkdir()
    slug_dir = projects / slugify(str(launch))
    slug_dir.mkdir(parents=True)
    transcript = slug_dir / "s1.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"hi"}}\n')

    threads = [
        {
            "sessionId": "s1",
            "file": str(transcript),
            "project": str(ws),
            "source": "claude-code",
            "mtime": "2026-08-29T00:00:00+00:00",
        }
    ]
    if extra_session:
        other = slug_dir / "other.jsonl"
        other.write_text('{"type":"user"}\n')
        threads.append(
            {
                "sessionId": "other",
                "file": str(other),
                "project": "/somewhere/else",
                "source": "claude-code",
                "mtime": "2026-08-29T00:00:00+00:00",
            }
        )

    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 2, "threads": threads, "archived": [], "deleted": []}))
    return ws, tmp_path / "moved", reg, projects, transcript


def test_is_beneath_respects_directory_boundaries():
    assert is_beneath("/a/b/c", "/a/b")
    assert is_beneath("/a/b", "/a/b")
    assert not is_beneath("/a/b-two", "/a/b")
    assert not is_beneath(None, "/a/b")


def test_remap_preserves_the_subpath():
    assert remap("/a/b/c/d", "/a/b", "/x/y") == "/x/y/c/d"
    assert remap("/a/b", "/a/b", "/x/y") == "/x/y"


def test_move_relocates_directory_storage_and_registry(tmp_path, monkeypatch):
    ws, new, reg, projects, transcript = _world(tmp_path, monkeypatch)

    assert move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--yes"]) == 0

    assert new.is_dir() and not ws.exists()
    moved = projects / slugify(str(new)) / "s1.jsonl"
    assert moved.exists() and not transcript.exists()
    thread = json.loads(reg.read_text())["threads"][0]
    assert thread["project"] == str(new)
    assert thread["file"] == str(moved)
    assert thread["locationPinned"] is True


def test_a_pinned_thread_survives_the_next_scan(tmp_path, monkeypatch):
    """Without the pin, reconcile would revert `project` from the transcript's
    stale baked-in cwd on the very next scan."""
    from ringleader import registry as registry_mod

    ws, new, reg, projects, _ = _world(tmp_path, monkeypatch)
    move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--yes"])

    loaded = json.loads(reg.read_text())
    scanned = [
        {
            "source": "claude-code",
            "session_id": "s1",
            "file": loaded["threads"][0]["file"],
            "project": str(ws),
            "mtime": "2026-08-29T01:00:00+00:00",
        }
    ]
    out = registry_mod.reconcile(loaded, scanned, "2026-08-29T01:00:00+00:00")
    assert out["threads"][0]["project"] == str(new)


def test_a_drifted_session_moves_on_its_own(tmp_path, monkeypatch):
    ws, new, reg, projects, transcript = _world(tmp_path, monkeypatch, drifted=True)

    assert move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--yes"]) == 0

    # it lands under the slug of its remapped cwd, not its old launch dir
    assert (projects / slugify(str(new)) / "s1.jsonl").exists()
    assert not transcript.exists()
    assert json.loads(reg.read_text())["threads"][0]["file"].endswith(
        f"{slugify(str(new))}/s1.jsonl"
    )


def test_a_shared_directory_is_not_renamed_wholesale(tmp_path, monkeypatch):
    """The unmatched session in the same slug dir must stay exactly where it is."""
    ws, new, reg, projects, _ = _world(tmp_path, monkeypatch, extra_session=True)
    stranger = projects / slugify(str(ws)) / "other.jsonl"

    assert move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--yes"]) == 0

    assert stranger.exists(), "a session we do not own was dragged along"
    assert (projects / slugify(str(new)) / "s1.jsonl").exists()


def test_move_refuses_when_storage_already_exists(tmp_path, monkeypatch):
    ws, new, reg, projects, transcript = _world(tmp_path, monkeypatch)
    (projects / slugify(str(new))).mkdir(parents=True)
    before = reg.read_text()

    assert move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--yes"]) == 1

    assert ws.is_dir() and not new.exists(), "refusal must not move anything"
    assert transcript.exists()
    assert reg.read_text() == before


@pytest.mark.parametrize(
    ("bad", "expected"), [("inside", "is inside"), ("exists", "already exists")]
)
def test_move_refuses_bad_destinations(tmp_path, monkeypatch, capsys, bad, expected):
    """Asserts the specific preflight message, not just a nonzero exit: `shutil.move`
    refuses to move a directory into itself on its own, so a bare exit-code assertion
    passes even with the `is_beneath` guard deleted."""
    ws, new, reg, _projects, _t = _world(tmp_path, monkeypatch)
    target = ws / "inner" if bad == "inside" else new
    if bad == "exists":
        target.mkdir()

    assert move_workspace.cmd_move([str(ws), str(target), "--registry", str(reg), "--yes"]) == 1
    assert expected in capsys.readouterr().err
    assert ws.is_dir()


def test_dry_run_changes_nothing(tmp_path, monkeypatch):
    ws, new, reg, _projects, transcript = _world(tmp_path, monkeypatch)
    before = reg.read_text()

    assert move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--dry-run"]) == 0

    assert ws.is_dir() and not new.exists()
    assert transcript.exists()
    assert reg.read_text() == before


def test_retarget_repairs_an_already_moved_directory(tmp_path, monkeypatch):
    """The recovery path: the directory moved by other means, storage and registry
    still point at the old location."""
    ws, new, reg, projects, _ = _world(tmp_path, monkeypatch)
    ws.rename(new)

    assert move_workspace.cmd_retarget([str(ws), str(new), "--registry", str(reg), "--yes"]) == 0

    assert (projects / slugify(str(new)) / "s1.jsonl").exists()
    assert json.loads(reg.read_text())["threads"][0]["project"] == str(new)


def test_retarget_is_idempotent(tmp_path, monkeypatch, capsys):
    """A second run is a no-op and must report success, or a retry script reading the
    exit status treats a completed retarget as a failure. Asserts the message too, so it
    cannot pass via the storage-collision guard instead of the nothing-to-do path."""
    ws, new, reg, _projects, _t = _world(tmp_path, monkeypatch)
    ws.rename(new)
    move_workspace.cmd_retarget([str(ws), str(new), "--registry", str(reg), "--yes"])
    after_first = reg.read_text()
    capsys.readouterr()

    assert move_workspace.cmd_retarget([str(ws), str(new), "--registry", str(reg), "--yes"]) == 0
    assert "already recorded beneath" in capsys.readouterr().out
    assert reg.read_text() == after_first


def test_retarget_reports_a_path_that_matches_nothing_anywhere(tmp_path, monkeypatch, capsys):
    ws, new, reg, _projects, _t = _world(tmp_path, monkeypatch)
    ws.rename(new)
    bogus = tmp_path / "never-existed"

    assert move_workspace.cmd_retarget([str(bogus), str(new), "--registry", str(reg), "--yes"]) == 1
    assert "no sessions recorded beneath" in capsys.readouterr().err


def test_move_refuses_when_nothing_matches(tmp_path, monkeypatch, capsys):
    """Matching is lexical and case-sensitive while the filesystem is neither, so a
    zero-match move is almost always a mistyped OLD — moving anyway would orphan every
    session under it and still exit 0."""
    ws, new, reg, _projects, _t = _world(tmp_path, monkeypatch)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()

    rc = move_workspace.cmd_move(
        [str(unrelated), str(tmp_path / "dest"), "--registry", str(reg), "--yes"]
    )

    assert rc == 1
    assert "no recorded sessions" in capsys.readouterr().err
    assert unrelated.is_dir(), "refusal must not move the directory"


def test_a_declined_confirmation_changes_nothing(tmp_path, monkeypatch):
    ws, new, reg, _projects, transcript = _world(tmp_path, monkeypatch)
    before = reg.read_text()
    monkeypatch.setattr("builtins.input", lambda *_: "n")

    assert move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg)]) == 0

    assert ws.is_dir() and not new.exists()
    assert transcript.exists()
    assert reg.read_text() == before


def test_a_failed_relocation_leaves_the_session_recoverable(tmp_path, monkeypatch, capsys):
    """The central claim of the module: a partial failure must stay repairable by
    `retarget`. Pinning a stranded session would rewrite its `project` and put it beyond
    the reach of the one command that could fix it."""
    ws, new, reg, projects, transcript = _world(tmp_path, monkeypatch)

    def boom(self, old_cwd, new_cwd):
        raise OSError("disk full")

    monkeypatch.setattr("ringleader.sources.claude_code.ClaudeCodeSource.relocate_storage", boom)

    rc = move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--yes"])

    assert rc == 1
    thread = json.loads(reg.read_text())["threads"][0]
    assert thread["project"] == str(ws), "a stranded session must not be pinned to the new path"
    assert transcript.exists(), "its transcript never moved"
    assert "retarget" in capsys.readouterr().err


def test_archived_records_are_retargeted(tmp_path, monkeypatch):
    ws, new, reg, _projects, _t = _world(tmp_path, monkeypatch)
    loaded = json.loads(reg.read_text())
    loaded["archived"] = [
        {
            "sessionId": "a1",
            "archivedAt": "2026-08-01T00:00:00+00:00",
            "project": "some-git-label",
            "thread": {"sessionId": "a1", "project": str(ws / "sub"), "source": "claude-code"},
        }
    ]
    reg.write_text(json.dumps(loaded))

    assert move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--yes"]) == 0

    record = json.loads(reg.read_text())["archived"][0]
    assert record["thread"]["project"] == str(new / "sub")
    assert record["project"] == "some-git-label", "a git label must not be rewritten as a path"
