import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


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


def _registry(tmp, sid, f, *, harvest=None):
    reg = tmp / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": None,
                "tombstones": [],
                "threads": [{"sessionId": sid, "file": str(f), "harvestPath": harvest}],
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
    ts = data["tombstones"][0]
    assert ts["reason"] == "archived" and ts["harvestPath"] == ["docs/x.md"]
    assert ts["archivePath"] == str(archived)


def test_delete_removes_files_and_tombstones(tmp_path):
    f = _session(tmp_path, "b")
    reg = _registry(tmp_path, "b", f)
    r = _run("rl-session delete", reg, "b")
    assert r.returncode == 0 and "deleted b" in r.stdout
    assert not f.exists() and not (f.parent / "b").exists()
    data = json.loads(reg.read_text())
    assert data["threads"] == []
    assert data["tombstones"][0]["reason"] == "deleted"


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


def test_review_sets_reviewed_at_mtime(tmp_path):
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": None,
                "tombstones": [],
                "threads": [
                    {
                        "sessionId": "a",
                        "file": "x",
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
                "tombstones": [],
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
    ts = json.loads(reg.read_text())["tombstones"][0]
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
    ts = json.loads(reg.read_text())["tombstones"][0]
    assert ts["summary"] == "release tooling / tag-script"


def test_archive_summary_fallback_uses_first_line_without_session_prefix(tmp_path):
    f = _session(tmp_path, "a")
    reg = _registry(tmp_path, "a", f)
    summ = tmp_path / "nuggets.md"
    summ.write_text("## Gotchas\n- watch out\n", encoding="utf-8")
    arc = tmp_path / "archive"
    _run("rl-session archive", reg, "a", "--archive-root", str(arc), "--summary-file", str(summ))
    ts = json.loads(reg.read_text())["tombstones"][0]
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
                "tombstones": [],
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
    ts = data["tombstones"][0]
    assert ts["reason"] == "archived" and ts["archivePath"] == str(archived)


def test_st_archived_lists_archived_with_project_filter(tmp_path):
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": None,
                "threads": [],
                "tombstones": [
                    {
                        "sessionId": "a",
                        "deletedAt": "2026-06-24T00:00:00+00:00",
                        "reason": "archived",
                        "archivePath": "archive/enc/a.jsonl",
                        "harvestPath": ["docs/x.md"],
                        "summary": "did A",
                        "project": "splashdown",
                    },
                    {
                        "sessionId": "b",
                        "deletedAt": "2026-06-23T00:00:00+00:00",
                        "reason": "prune",
                        "harvestPath": None,
                    },
                    {
                        "sessionId": "c",
                        "deletedAt": "2026-06-22T00:00:00+00:00",
                        "reason": "archived",
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
                "tombstones": [],
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
        json.dumps({"version": 1, "lastScan": None, "tombstones": [], "threads": []}),
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
