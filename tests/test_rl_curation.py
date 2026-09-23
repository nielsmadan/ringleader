import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from ringleader.commands import archive as archive_mod
from ringleader.commands import harvest as harvest_mod

REPO = Path(__file__).resolve().parent.parent


class ControlledPublishLock:
    def __init__(self, writer_name):
        self.writer_name = writer_name
        self.writer_attempted = threading.Event()
        self.lock = threading.Lock()
        self.owner = None

    @contextmanager
    def __call__(self):
        if threading.current_thread().name == self.writer_name:
            self.writer_attempted.set()
        self.lock.acquire()
        self.owner = threading.get_ident()
        try:
            yield
        finally:
            self.owner = None
            self.lock.release()

    def owned_by_current_thread(self):
        return self.owner == threading.get_ident()


class FakeModel:
    def complete_json(self, prompt, schema):
        return {"summary": "writer summary"}


def _session(tmp, sid="a"):
    # mimic ~/.claude/projects/<encoded>/<uuid>.jsonl + sidecar
    proj = tmp / "-Users-me-proj"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{sid}.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    side = proj / sid
    side.mkdir()
    (side / "n.txt").write_text("x", encoding="utf-8")
    return f


def _droid_session(tmp, sid="d"):
    # mimic ~/.factory/sessions/<encoded>/<uuid>.jsonl + its two settings sidecars
    proj = tmp / "_factory" / ".factory" / "sessions" / "-Users-me-proj"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{sid}.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    (proj / f"{sid}.settings.json").write_text('{"model":"opus"}', encoding="utf-8")
    (proj / f"{sid}.settings.json.bak").write_text("{}", encoding="utf-8")
    return f


def _registry(tmp, sid, f, *, harvest=None, source=None):
    reg = tmp / "registry.json"
    thread = {"sessionId": sid, "file": str(f), "harvestPath": harvest}
    if source:
        thread["source"] = source
    reg.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": None,
                "archived": [],
                "deleted": [],
                "threads": [thread],
            }
        ),
        encoding="utf-8",
    )
    return reg


def _run(script, reg, *args):
    sub = script.removeprefix("rl-").split()
    return subprocess.run(
        [sys.executable, "-m", "ringleader", *sub, "--registry", str(reg), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
    )


def test_archive_moves_files_and_tombstones(tmp_path):
    f = _session(tmp_path, "a")
    reg = _registry(tmp_path, "a", f, harvest=["docs/x.md"])
    arc = tmp_path / "archive"
    r = _run("rl-session archive", reg, "a", "--archive-root", str(arc))
    assert r.returncode == 0 and "archived a" in r.stdout
    assert not f.exists() and not (f.parent / "a").exists()  # moved out
    # no gitProjectLabel/project/source on this bare thread, so the slug falls
    # back to "unknown" rather than the transcript's parent dir name.
    archived = arc / "unknown" / "a.jsonl"
    assert archived.exists()
    assert (arc / "unknown" / "a").exists()  # sidecar moved too
    data = json.loads(reg.read_text())
    assert data["threads"] == []
    ts = data["archived"][0]
    assert ts["harvestPath"] == ["docs/x.md"]
    assert ts["archivePath"] == str(archived)


def test_archive_cli_retries_after_move_when_registry_save_failed(tmp_path, monkeypatch, capsys):
    f = _session(tmp_path, "crash")
    reg = _registry(tmp_path, "crash", f)
    arc = tmp_path / "archive"
    summary = tmp_path / "summary.md"
    summary.write_text("# Crash recovery\n")
    real_save = archive_mod.registry.save_registry
    monkeypatch.setattr(
        archive_mod.registry,
        "save_registry",
        lambda *args: (_ for _ in ()).throw(OSError("registry save failed")),
    )

    with pytest.raises(OSError, match="registry save failed"):
        archive_mod.cmd_archive(
            [
                "crash",
                "--registry",
                str(reg),
                "--archive-root",
                str(arc),
                "--summary-file",
                str(summary),
            ]
        )

    assert not f.exists()
    assert (arc / "unknown" / "crash.jsonl").exists()
    assert (arc / "unknown" / "crash.md").read_text() == "# Crash recovery\n"
    monkeypatch.setattr(archive_mod.registry, "save_registry", real_save)

    assert (
        archive_mod.cmd_archive(
            [
                "crash",
                "--registry",
                str(reg),
                "--archive-root",
                str(arc),
                "--summary-file",
                str(summary),
            ]
        )
        == 0
    )
    assert "archived crash" in capsys.readouterr().out
    saved = json.loads(reg.read_text())
    assert saved["threads"] == []
    assert [item["sessionId"] for item in saved["archived"]] == ["crash"]


def test_archive_rejects_missing_source_and_destination_without_mutating_registry(tmp_path):
    transcript = _session(tmp_path, "missing")
    reg = _registry(tmp_path, "missing", transcript)
    before = reg.read_bytes()
    transcript.unlink()
    sidecar = transcript.with_suffix("")
    arc = tmp_path / "archive"

    result = _run("rl-session archive", reg, "missing", "--archive-root", str(arc))

    assert result.returncode == 1
    assert "missing from both" in result.stderr
    assert reg.read_bytes() == before
    assert sidecar.exists()


def test_archive_and_harvest_writer_serialize_registry_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    first = _session(tmp_path, "archive-me")
    second = _session(tmp_path, "harvest-me")
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [
                    {
                        "sessionId": "archive-me",
                        "file": str(first),
                        "project": "/repo",
                        "gitProjectLabel": "repo",
                    },
                    {
                        "sessionId": "harvest-me",
                        "file": str(second),
                        "project": "/repo",
                        "gitProjectLabel": "repo",
                        "title": "writer",
                    },
                ],
                "archived": [],
                "deleted": [],
            }
        )
    )
    monkeypatch.setattr(harvest_mod, "active_model", lambda cfg: FakeModel())
    monkeypatch.setattr(harvest_mod.export, "render_conversation", lambda path: "hi")
    controlled_lock = ControlledPublishLock("harvest-writer")
    monkeypatch.setattr(archive_mod, "publish_lock", controlled_lock, raising=False)
    monkeypatch.setattr(harvest_mod, "publish_lock", controlled_lock)
    archive_loaded = threading.Event()
    writer_done = threading.Event()
    real_load = archive_mod.registry.load_registry

    def controlled_load(path):
        loaded = real_load(path)
        if threading.current_thread().name == "archive" and not archive_loaded.is_set():
            archive_loaded.set()
            assert controlled_lock.writer_attempted.wait(3)
            if not controlled_lock.owned_by_current_thread():
                assert writer_done.wait(3)
        return loaded

    monkeypatch.setattr(archive_mod.registry, "load_registry", controlled_load)
    results = {}

    def run_archive():
        results["archive"] = archive_mod.cmd_archive(
            [
                "archive-me",
                "--registry",
                str(reg),
                "--archive-root",
                str(tmp_path / "archive"),
            ]
        )

    def run_writer():
        try:
            assert archive_loaded.wait(3)
            results["writer"] = harvest_mod.cmd_harvest(["harvest-me", "--registry", str(reg)])
        finally:
            writer_done.set()

    archive_thread = threading.Thread(target=run_archive, name="archive")
    writer_thread = threading.Thread(target=run_writer, name="harvest-writer")
    archive_thread.start()
    writer_thread.start()
    archive_thread.join(5)
    writer_thread.join(5)

    assert not archive_thread.is_alive()
    assert not writer_thread.is_alive()
    assert results == {"archive": 0, "writer": 0}
    saved = real_load(reg)
    assert [item["sessionId"] for item in saved["archived"]] == ["archive-me"]
    writer = next(item for item in saved["threads"] if item["sessionId"] == "harvest-me")
    assert len(writer["harvestPath"]) == 1


def test_delete_removes_files_and_tombstones(tmp_path):
    f = _session(tmp_path, "b")
    reg = _registry(tmp_path, "b", f)
    r = _run("rl-session delete", reg, "b")
    assert r.returncode == 0 and "deleted b" in r.stdout
    assert not f.exists() and not (f.parent / "b").exists()
    data = json.loads(reg.read_text())
    assert data["threads"] == []
    assert data["deleted"][0]["reason"] == "deleted"


def test_archive_unknown_id_exits_1(tmp_path):
    reg = _registry(tmp_path, "a", tmp_path / "x.jsonl")
    assert _run("rl-session archive", reg, "nope").returncode == 1


def test_mark_harvested_sets_decision(tmp_path):
    reg = _registry(tmp_path, "a", tmp_path / "a.jsonl")
    r = _run("rl-session mark harvested", reg, "a", "--paths", "docs/x.md,docs/y.md")
    assert r.returncode == 0
    t = json.loads(reg.read_text())["threads"][0]
    assert t["decision"] == "harvested" and t["harvestPath"] == ["docs/x.md", "docs/y.md"]


def test_mark_harvested_empty_paths_is_none(tmp_path):
    reg = _registry(tmp_path, "a", tmp_path / "a.jsonl")
    _run("rl-session mark harvested", reg, "a")
    assert json.loads(reg.read_text())["threads"][0]["harvestPath"] is None


def test_review_sets_reviewed_at_mtime_for_fileless_session(tmp_path):
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": None,
                "archived": [],
                "deleted": [],
                "threads": [
                    {
                        "sessionId": "a",
                        "source": "opencode",
                        "file": None,
                        "mtime": "2026-06-05T00:00:00+00:00",
                        "reviewedAtMtime": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    r = _run("rl-session mark reviewed", reg, "a")
    assert r.returncode == 0
    assert (
        json.loads(reg.read_text())["threads"][0]["reviewedAtMtime"] == "2026-06-05T00:00:00+00:00"
    )


def test_archive_aborts_on_sidecar_dest_collision(tmp_path):
    """rl session archive exits 1 if the sidecar destination already exists; source files are not moved."""
    f = _session(tmp_path, "c")
    reg = _registry(tmp_path, "c", f)
    arc = tmp_path / "archive"
    # Pre-create the sidecar destination directory so it already exists
    sidecar_dest = arc / "unknown" / "c"
    sidecar_dest.mkdir(parents=True, exist_ok=True)
    r = _run("rl-session archive", reg, "c", "--archive-root", str(arc))
    assert r.returncode == 1
    # Original .jsonl must still be in place
    assert f.exists()
    # Original sidecar must still be in place
    assert (f.parent / "c").exists()


def test_archive_writes_summary_sidecar_and_records_summary_project(tmp_path):
    f = _session(tmp_path, "a")
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": None,
                "archived": [],
                "deleted": [],
                "threads": [
                    {
                        "sessionId": "a",
                        "file": str(f),
                        "harvestPath": ["docs/x.md"],
                        "whatItIs": "fixed the thing",
                        "gitProjectLabel": "splashdown",
                        "project": "/p",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    summ = tmp_path / "nuggets.md"
    summ.write_text("## Gotchas\n- watch out\n", encoding="utf-8")
    arc = tmp_path / "archive"
    r = _run(
        "rl-session archive", reg, "a", "--archive-root", str(arc), "--summary-file", str(summ)
    )
    assert r.returncode == 0
    side = arc / "splashdown" / "a.md"  # dest dir now derives from gitProjectLabel
    assert side.exists() and "watch out" in side.read_text()
    ts = json.loads(reg.read_text())["archived"][0]
    assert ts["summary"] == "fixed the thing" and ts["project"] == "splashdown"


def test_archive_summary_falls_back_to_sidecar_heading(tmp_path):
    """When the thread has no whatItIs, the tombstone summary is derived from the
    summary file's first heading, stripping a 'Session <id> — ' prefix."""
    f = _session(tmp_path, "a")
    reg = _registry(tmp_path, "a", f)  # no whatItIs on the thread
    summ = tmp_path / "nuggets.md"
    summ.write_text(
        "# Session a — release tooling / tag-script\n\n## Gotchas\n- x\n", encoding="utf-8"
    )
    arc = tmp_path / "archive"
    r = _run(
        "rl-session archive", reg, "a", "--archive-root", str(arc), "--summary-file", str(summ)
    )
    assert r.returncode == 0
    ts = json.loads(reg.read_text())["archived"][0]
    assert ts["summary"] == "release tooling / tag-script"


def test_archive_summary_fallback_uses_first_line_without_session_prefix(tmp_path):
    f = _session(tmp_path, "a")
    reg = _registry(tmp_path, "a", f)
    summ = tmp_path / "nuggets.md"
    summ.write_text("## Gotchas\n- watch out\n", encoding="utf-8")
    arc = tmp_path / "archive"
    _run("rl-session archive", reg, "a", "--archive-root", str(arc), "--summary-file", str(summ))
    ts = json.loads(reg.read_text())["archived"][0]
    assert ts["summary"] == "Gotchas"


def test_archive_codex_thread_uses_git_project_label_not_parent_dir(tmp_path):
    """Codex transcripts don't live under a per-project encoded directory the way
    Claude Code's do, so the archive destination must come from gitProjectLabel
    (falling back to project/source), not from the transcript's parent dir name."""
    f = tmp_path / "codex-sessions" / "2026" / "08" / "03" / "rollout-abc.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{}\n", encoding="utf-8")
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": None,
                "archived": [],
                "deleted": [],
                "threads": [
                    {
                        "sessionId": "codex-a",
                        "source": "codex",
                        "file": str(f),
                        "gitProjectLabel": "proj",
                        "project": "/Users/me/proj",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    arc = tmp_path / "archive"
    r = _run("rl-session archive", reg, "codex-a", "--archive-root", str(arc))
    assert r.returncode == 0 and "archived codex-a" in r.stdout
    archived = arc / "proj" / "rollout-abc.jsonl"
    assert archived.exists()
    assert not f.exists()
    data = json.loads(reg.read_text())
    assert data["threads"] == []
    ts = data["archived"][0]
    assert ts["archivePath"] == str(archived)


def test_st_archived_lists_archived_with_project_filter(tmp_path):
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": None,
                "threads": [],
                "archived": [
                    {
                        "sessionId": "a",
                        "archivedAt": "2026-06-24T00:00:00+00:00",
                        "archivePath": "archive/enc/a.jsonl",
                        "harvestPath": ["docs/x.md"],
                        "summary": "did A",
                        "project": "splashdown",
                    },
                    {
                        "sessionId": "c",
                        "archivedAt": "2026-06-22T00:00:00+00:00",
                        "archivePath": "archive/enc2/c.jsonl",
                        "summary": "did C",
                        "project": "juggler",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    r = _run("rl-overview", reg, "--archived", "--no-scan")
    assert "did A" in r.stdout and "did C" in r.stdout and "b" not in r.stdout.split("\n")[0]
    assert "2 archived session(s)" in r.stdout
    r2 = _run("rl-overview", reg, "--archived", "--project", "splashdown", "--no-scan")
    assert "did A" in r2.stdout and "did C" not in r2.stdout
    assert "1 archived session(s)" in r2.stdout


def test_st_cleaned_records_timestamp(tmp_path):
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": None,
                "archived": [],
                "deleted": [],
                "projectsCleanedAt": {},
                "threads": [
                    {
                        "sessionId": "a",
                        "file": "x",
                        "project": "/Users/me/proj",
                        "gitProjectKey": "k1",
                        "gitProjectLabel": "proj",
                        "mtime": "2026-06-01T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    r = _run("rl-session mark cleaned", reg, "proj")
    assert r.returncode == 0
    assert "k1" in json.loads(reg.read_text())["projectsCleanedAt"]


def test_st_cleaned_unknown_selector_exits_1(tmp_path):
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps({"version": 1, "lastScan": None, "archived": [], "deleted": [], "threads": []}),
        encoding="utf-8",
    )
    assert _run("rl-session mark cleaned", reg, "nope").returncode == 1


def test_mark_reviewed_sets_reviewed_marker(tmp_path):
    f = _session(tmp_path, "r")
    reg = _registry(tmp_path, "r", f)
    # give the thread an mtime so mark-reviewed has something to stamp
    data = json.loads(reg.read_text())
    data["threads"][0]["mtime"] = "2026-06-01T00:00:00+00:00"
    reg.write_text(json.dumps(data), encoding="utf-8")
    r = _run("rl-session mark reviewed", reg, "r")
    assert r.returncode == 0 and "reviewed" in r.stdout.lower()
    assert json.loads(reg.read_text())["threads"][0]["reviewedAtMtime"] is not None


def test_archive_carries_a_droid_sessions_settings_sidecars_into_the_archive(tmp_path):
    """Droid keeps a session's token accounting in `<id>.settings.json` beside the
    transcript, so archiving without them splits the session from its own state."""
    f = _droid_session(tmp_path, "d")
    reg = _registry(tmp_path, "d", f, source="droid")
    arc = tmp_path / "archive"

    r = _run("rl-session archive", reg, "d", "--archive-root", str(arc))

    assert r.returncode == 0 and "archived d" in r.stdout
    assert sorted(p.name for p in (arc / "droid").iterdir()) == [
        "d.jsonl",
        "d.settings.json",
        "d.settings.json.bak",
    ]
    assert list(f.parent.iterdir()) == []
    assert (arc / "droid" / "d.settings.json").read_text() == '{"model":"opus"}'


def test_unarchive_puts_a_droid_sessions_settings_sidecars_back(tmp_path):
    original = tmp_path / "_factory" / ".factory" / "sessions" / "-Users-me-proj" / "d.jsonl"
    original.parent.mkdir(parents=True)
    arc = tmp_path / "archive" / "droid"
    arc.mkdir(parents=True)
    (arc / "d.jsonl").write_text("{}\n", encoding="utf-8")
    (arc / "d.settings.json").write_text('{"model":"opus"}', encoding="utf-8")
    (arc / "d.settings.json.bak").write_text("{}", encoding="utf-8")
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": None,
                "threads": [],
                "deleted": [],
                "archived": [
                    {
                        "sessionId": "d",
                        "source": "droid",
                        "archivePath": str(arc / "d.jsonl"),
                        "originalPath": str(original),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    r = _run("rl-session unarchive", reg, "d")

    assert r.returncode == 0 and "restored d" in r.stdout
    assert sorted(p.name for p in original.parent.iterdir()) == [
        "d.jsonl",
        "d.settings.json",
        "d.settings.json.bak",
    ]
    assert (original.parent / "d.settings.json").read_text() == '{"model":"opus"}'
    assert list(arc.iterdir()) == []


def test_delete_removes_a_droid_sessions_settings_sidecars_too(tmp_path):
    f = _droid_session(tmp_path, "d")
    reg = _registry(tmp_path, "d", f, source="droid")

    r = _run("rl-session delete", reg, "d")

    assert r.returncode == 0 and "deleted d" in r.stdout
    assert list(f.parent.iterdir()) == []
