import json
import sqlite3
from pathlib import Path

import pytest
from ringleader import registry as registry_mod
from ringleader.commands import move_workspace
from ringleader.slug import cursor_chat_key, cursor_slug, is_beneath, remap, slugify
from ringleader.sources.droid import DroidSource


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
    transcript.write_text(
        json.dumps({"type": "user", "cwd": str(ws), "message": {"content": "hi"}}) + "\n"
    )

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
        other.write_text(
            json.dumps({"type": "user", "cwd": "/somewhere/else", "message": {"content": "x"}})
            + "\n"
        )
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
    ws, new, reg, _projects, _ = _world(tmp_path, monkeypatch)
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
    _ws, _new, reg, _projects, _t = _world(tmp_path, monkeypatch)
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
    ws, new, reg, _projects, transcript = _world(tmp_path, monkeypatch)

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


def _editable(tmp_path, target: str, name="_editable_impl_demo.pth"):
    site = tmp_path / "home/.local/share/uv/tools/demo/lib/python3.13/site-packages"
    site.mkdir(parents=True, exist_ok=True)
    pth = site / name
    pth.write_text(target + "\n")
    return pth


def test_editable_pointing_at_the_moved_tree_is_found(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    pth = _editable(tmp_path, "/ws/proj")
    assert move_workspace.editable_installs("/ws/proj") == [pth]


def test_an_editable_beneath_the_moved_tree_is_found(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    pth = _editable(tmp_path, "/ws/proj/src")
    assert move_workspace.editable_installs("/ws/proj") == [pth]


def test_an_unrelated_editable_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _editable(tmp_path, "/somewhere/else")
    assert move_workspace.editable_installs("/ws/proj") == []


def test_repointing_rewrites_only_the_matching_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    pth = _editable(tmp_path, "/ws/proj/src")
    assert move_workspace.repoint_editables([pth], "/ws/proj", "/ws/new/dev1") == []
    assert pth.read_text().strip() == "/ws/new/dev1/src"


def test_an_unwritable_pth_is_reported_rather_than_silently_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    pth = _editable(tmp_path, "/ws/proj")
    pth.chmod(0o444)
    pth.parent.chmod(0o555)
    try:
        assert move_workspace.repoint_editables([pth], "/ws/proj", "/ws/new") == [pth]
    finally:
        pth.parent.chmod(0o755)
        pth.chmod(0o644)


def test_a_cached_git_root_moves_with_the_tree():
    # the destination had no git identity, so pin_thread_location kept the cached
    # root; it must still name the new location rather than the vanished one.
    t = {"gitRoot": "/ws/proj/.claude/worktrees/gone"}
    registry_mod.remap_cached_git_root(t, "/ws/proj", "/ws/new/dev1")
    assert t["gitRoot"] == "/ws/new/dev1/.claude/worktrees/gone"


def test_a_git_root_equal_to_the_moved_dir_is_remapped():
    t = {"gitRoot": "/ws/proj"}
    registry_mod.remap_cached_git_root(t, "/ws/proj", "/ws/new/dev1")
    assert t["gitRoot"] == "/ws/new/dev1"


def test_a_git_root_outside_the_moved_tree_is_untouched():
    t = {"gitRoot": "/ws/other"}
    registry_mod.remap_cached_git_root(t, "/ws/proj", "/ws/new")
    assert t["gitRoot"] == "/ws/other"


def test_a_sibling_with_a_shared_prefix_is_not_remapped():
    t = {"gitRoot": "/ws/proj-two"}
    registry_mod.remap_cached_git_root(t, "/ws/proj", "/ws/new")
    assert t["gitRoot"] == "/ws/proj-two"


def test_a_thread_without_a_git_root_is_left_alone():
    t = {}
    registry_mod.remap_cached_git_root(t, "/ws/proj", "/ws/new")
    assert "gitRoot" not in t


def _tree(tmp_path, *paths: str):
    """Build a tree from `dir/` and `file` entries, returning its root."""
    root = tmp_path / "tree"
    for spec in paths:
        target = root / spec.rstrip("/")
        if spec.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("")
    return root


def _found(root):
    return {str(p.relative_to(root)): cmd for p, cmd in move_workspace.derived_state(str(root))}


def test_a_uv_project_venv_reports_the_sync_command(tmp_path):
    root = _tree(tmp_path, ".venv/", "uv.lock")
    assert _found(root) == {".venv": "uv sync --reinstall"}


def test_a_venv_without_a_recognised_toolchain_falls_back(tmp_path):
    root = _tree(tmp_path, ".venv/")
    assert _found(root) == {".venv": "recreate the virtualenv"}


def test_the_lockfile_selects_the_node_package_manager(tmp_path):
    root = _tree(tmp_path, "node_modules/", "pnpm-lock.yaml")
    assert _found(root) == {"node_modules": "pnpm install --force"}


def test_node_modules_without_a_lockfile_falls_back_to_npm(tmp_path):
    root = _tree(tmp_path, "node_modules/")
    assert _found(root) == {"node_modules": "npm ci"}


def test_a_target_directory_needs_a_cargo_manifest_to_count(tmp_path):
    root = _tree(tmp_path, "target/")
    assert _found(root) == {}


def test_a_cargo_target_directory_is_reported(tmp_path):
    root = _tree(tmp_path, "target/", "Cargo.toml")
    assert _found(root) == {"target": "cargo clean"}


def test_every_artifact_in_one_directory_is_reported(tmp_path):
    root = _tree(tmp_path, ".venv/", "uv.lock", "node_modules/", "package-lock.json")
    assert _found(root) == {".venv": "uv sync --reinstall", "node_modules": "npm ci"}


def test_a_monorepo_package_two_levels_down_is_found(tmp_path):
    root = _tree(tmp_path, "apps/web/node_modules/", "apps/web/yarn.lock")
    assert _found(root) == {"apps/web/node_modules": "yarn install --force"}


def test_the_search_stops_below_the_depth_limit(tmp_path):
    root = _tree(tmp_path, "a/b/c/node_modules/")
    assert _found(root) == {}


def test_artifact_directories_are_not_searched_from_within(tmp_path):
    root = _tree(tmp_path, "node_modules/nested/.venv/", "node_modules/nested/uv.lock")
    assert _found(root) == {"node_modules": "npm ci"}


def _codex_world(tmp_path, monkeypatch):
    """A workspace whose only session belongs to a source that rebinds in place:
    Codex files rollouts by date, so nothing moves — only the recorded cwd."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(tmp_path / "no-claude"))
    monkeypatch.setattr(move_workspace, "_git_for", lambda p: None)

    home = tmp_path / "codex"
    rollouts = home / "sessions" / "2026" / "08" / "29"
    rollouts.mkdir(parents=True)
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(home / "sessions"))

    ws = tmp_path / "ws"
    ws.mkdir()
    rollout = rollouts / "rollout-c1.jsonl"
    rollout.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"id": "c1", "cwd": str(ws), "source": "cli"}}
        )
        + "\n"
    )

    con = sqlite3.connect(home / "state_5.sqlite")
    con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, cwd TEXT)")
    con.execute(
        "INSERT INTO threads (id, rollout_path, cwd) VALUES (?, ?, ?)",
        ("c1", str(rollout), str(ws)),
    )
    con.commit()
    con.close()

    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [
                    {
                        "sessionId": "c1",
                        "file": str(rollout),
                        "project": str(ws),
                        "source": "codex",
                        "mtime": "2026-08-29T00:00:00+00:00",
                    }
                ],
                "archived": [],
                "deleted": [],
            }
        )
    )
    return ws, tmp_path / "moved", reg, home, rollout


def _codex_cwd(home, sid="c1"):
    con = sqlite3.connect(home / "state_5.sqlite")
    row = con.execute("SELECT cwd FROM threads WHERE id = ?", (sid,)).fetchone()
    con.close()
    return row[0]


def test_move_rebinds_a_session_whose_source_stores_transcripts_flat(tmp_path, monkeypatch):
    ws, new, reg, home, rollout = _codex_world(tmp_path, monkeypatch)

    assert move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--yes"]) == 0

    assert _codex_cwd(home) == str(new)
    assert rollout.exists()
    assert json.loads(reg.read_text())["threads"][0]["project"] == str(new)


def test_a_rebinding_session_is_not_reported_as_unsupported(tmp_path, monkeypatch, capsys):
    """It used to fall through to `registry only`, which updated the registry and left
    the session unreachable from the new path — the bug this seam exists to fix."""
    ws, new, reg, _home, _rollout = _codex_world(tmp_path, monkeypatch)

    move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--dry-run"])

    out = capsys.readouterr().out
    assert "rebound in place (codex)" in out
    assert "registry only" not in out


def test_a_failed_rebind_leaves_the_session_recoverable(tmp_path, monkeypatch, capsys):
    ws, new, reg, _home, _rollout = _codex_world(tmp_path, monkeypatch)

    def boom(self, session_id, file_path, new_cwd):
        raise RuntimeError("state db is locked")

    monkeypatch.setattr("ringleader.sources.codex.CodexSource.rebind_workspace", boom)

    assert move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--yes"]) == 1
    assert "state db is locked" in capsys.readouterr().err
    # not pinned to the new path: `retarget` finds threads by the old one
    assert json.loads(reg.read_text())["threads"][0]["project"] == str(ws)


def test_a_rebind_with_no_index_entry_warns_by_id_and_still_pins(tmp_path, monkeypatch, capsys):
    """No row is not something a re-run can fix, so the move completes and pins. The
    warning has to name the session, or the user has nothing to act on."""
    ws, new, reg, home, _rollout = _codex_world(tmp_path, monkeypatch)
    con = sqlite3.connect(home / "state_5.sqlite")
    con.execute("DELETE FROM threads WHERE id = 'c1'")
    con.commit()
    con.close()

    assert move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--yes"]) == 0

    err = capsys.readouterr().err
    assert "c1" in err
    assert "stays resumable by id" in err
    assert json.loads(reg.read_text())["threads"][0]["project"] == str(new)


def test_a_rebind_without_an_index_strands_instead_of_pinning(tmp_path, monkeypatch, capsys):
    """A missing state db is usually temporary, and pinning would put the session beyond
    the reach of `retarget` — the one command that could still repair it."""
    ws, new, reg, home, _rollout = _codex_world(tmp_path, monkeypatch)
    (home / "state_5.sqlite").unlink()

    assert move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--yes"]) == 1

    assert "no session index" in capsys.readouterr().err
    assert json.loads(reg.read_text())["threads"][0]["project"] == str(ws)


def test_an_archived_session_is_rebound_at_its_original_path(tmp_path, monkeypatch):
    """Its index entry still exists and still names the pre-archive path, so it needs the
    same rebind — otherwise unarchiving after a move restores a session the picker keeps
    filtering to the old workspace."""
    ws, new, reg, home, _rollout = _codex_world(tmp_path, monkeypatch)
    archived_rollout = home / "sessions" / "2026" / "08" / "29" / "rollout-c2.jsonl"
    con = sqlite3.connect(home / "state_5.sqlite")
    con.execute(
        "INSERT INTO threads (id, rollout_path, cwd) VALUES (?, ?, ?)",
        ("c2", str(archived_rollout), str(ws)),
    )
    con.commit()
    con.close()
    data = json.loads(reg.read_text())
    data["archived"] = [
        {
            "sessionId": "c2",
            "archivePath": str(tmp_path / "archive" / "rollout-c2.jsonl"),
            "originalPath": str(archived_rollout),
            "thread": {"sessionId": "c2", "project": str(ws), "source": "codex"},
        }
    ]
    reg.write_text(json.dumps(data))

    assert move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--yes"]) == 0

    assert _codex_cwd(home, "c2") == str(new)


def test_move_includes_a_session_the_registry_has_not_scanned_yet(tmp_path, monkeypatch):
    """A session started (or forked) since the last scan is on disk but not in the
    registry. The plan is built from the registry, so without a scan first it is
    silently left behind at the old path."""
    ws, new, reg, projects, _transcript = _world(tmp_path, monkeypatch)
    unscanned = projects / slugify(str(ws)) / "fork.jsonl"
    unscanned.write_text(
        json.dumps({"type": "user", "cwd": str(ws), "message": {"content": "forked"}}) + "\n"
    )

    assert move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--yes"]) == 0

    threads = {t["sessionId"]: t for t in json.loads(reg.read_text())["threads"]}
    assert "fork" in threads, "the unscanned session never entered the plan"
    assert threads["fork"]["project"] == str(new)
    assert (projects / slugify(str(new)) / "fork.jsonl").exists()


def test_collisions_reports_a_droid_sidecar_sitting_at_the_destination(tmp_path):
    """`relocate_session` refuses when a sidecar is already there, and the contract is
    that the caller learns that up front — otherwise a multi-session move raises
    half-way, after earlier threads have already moved."""
    sessions = tmp_path / "sessions"
    launched = sessions / slugify("/w/api")
    launched.mkdir(parents=True)
    (launched / "s1.jsonl").write_text("{}\n")
    (launched / "s1.settings.json.bak").write_text("{}")
    destination = sessions / slugify("/w/renamed")
    destination.mkdir(parents=True)
    (destination / "s1.settings.json.bak").write_text("{}")
    source = DroidSource(sessions_dir=sessions)
    plan = move_workspace._Plan(old="/w/api", new="/w/renamed")
    plan.per.append(({"file": str(launched / "s1.jsonl")}, "droid", "/w/renamed"))

    found = move_workspace.collisions(plan, move_workspace._Sources({"droid": source}, {}))

    assert found == [str(destination / "s1.settings.json.bak")]


def test_a_droid_session_under_its_own_underscore_dot_space_slug_has_not_drifted():
    thread = {
        "source": "droid",
        "project": "/w/my_api.v2 x",
        "file": "/f/sessions/-w-my_api.v2 x/s1.jsonl",
    }
    srcs = move_workspace._Sources({"droid": DroidSource(sessions_dir=Path("/f/sessions"))}, {})

    assert move_workspace._drifted(thread, srcs) is False


def _cursor_world(tmp_path, monkeypatch, sessions):
    """Cursor chats, each `(chat_id, launch_dir)`, in the store conftest points Cursor at:
    a chat dir under `chats/<md5(cwd)>/` and a transcript under
    `projects/<slug>/agent-transcripts/<id>/`."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(move_workspace, "_git_for", lambda p: None)
    store = tmp_path / "_cursor"
    threads = []
    for chat_id, launch in sessions:
        launch.mkdir(parents=True, exist_ok=True)
        chat = store / "chats" / cursor_chat_key(str(launch)) / chat_id
        chat.mkdir(parents=True)
        (chat / "meta.json").write_text(json.dumps({"hasConversation": True, "cwd": str(launch)}))
        folder = store / "projects" / cursor_slug(str(launch)) / "agent-transcripts" / chat_id
        folder.mkdir(parents=True)
        (folder / f"{chat_id}.jsonl").write_text("{}\n")
        threads.append(
            {
                "sessionId": chat_id,
                "file": str(folder / f"{chat_id}.jsonl"),
                "project": str(launch),
                "source": "cursor",
                "mtime": "2026-11-11T00:00:00+00:00",
            }
        )
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 2, "threads": threads, "archived": [], "deleted": []}))
    return store, reg


def test_a_cursor_workspace_moves_its_storage_once_for_all_its_chats(tmp_path, monkeypatch):
    ws, new = tmp_path / "ws", tmp_path / "moved"
    store, reg = _cursor_world(tmp_path, monkeypatch, [("c1", ws), ("c2", ws)])

    plan = move_workspace.build_plan(
        json.loads(reg.read_text()), str(ws), str(new), move_workspace._resolve_sources()
    )
    assert (len(plan.whole), plan.per) == (1, [])

    assert move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--yes"]) == 0

    moved = store / "projects" / cursor_slug(str(new)) / "agent-transcripts"
    assert sorted(p.name for p in moved.glob("*/*.jsonl")) == ["c1.jsonl", "c2.jsonl"]
    chats = store / "chats" / cursor_chat_key(str(new))
    assert sorted(p.name for p in chats.iterdir()) == ["c1", "c2"]
    assert [t["file"] for t in json.loads(reg.read_text())["threads"]] == [
        str(moved / "c1" / "c1.jsonl"),
        str(moved / "c2" / "c2.jsonl"),
    ]


def test_a_cursor_slug_shared_with_another_directory_moves_chats_one_by_one(tmp_path, monkeypatch):
    """`my-app` and `my_app` share Cursor's slug, so the slug dir is not ours to rename."""
    ws, stranger, new = tmp_path / "my-app", tmp_path / "my_app", tmp_path / "moved"
    store, reg = _cursor_world(tmp_path, monkeypatch, [("c1", ws), ("c2", stranger)])

    assert move_workspace.cmd_move([str(ws), str(new), "--registry", str(reg), "--yes"]) == 0

    shared = store / "projects" / cursor_slug(str(ws)) / "agent-transcripts"
    assert sorted(p.name for p in shared.glob("*/*.jsonl")) == ["c2.jsonl"]
    moved = store / "projects" / cursor_slug(str(new)) / "agent-transcripts" / "c1" / "c1.jsonl"
    assert moved.is_file()
    assert (store / "chats" / cursor_chat_key(str(new)) / "c1" / "meta.json").is_file()
