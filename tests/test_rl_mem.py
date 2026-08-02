import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run(sub, *args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "ringleader", sub, *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _mem(root, enc, slug, body):
    d = root / enc / "memory"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(body + "\n", encoding="utf-8")


def _txn(root, enc, cwd):
    (root / enc).mkdir(parents=True, exist_ok=True)
    (root / enc / "s.jsonl").write_text(
        json.dumps({"type": "user", "cwd": cwd, "message": {"role": "user", "content": "x"}}) + "\n"
    )


def test_mem_scan_emits_entries(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    _txn(tmp_path, "-enc", str(proj))
    _mem(tmp_path, "-enc", "a", "durable note about ports")
    reg = tmp_path / "registry.json"
    import os

    env = {**os.environ, "SESSION_TRACKER_PROJECTS_DIR": str(tmp_path)}
    r = _run("mem-scan", "--registry", str(reg), env=env)
    assert r.returncode == 0, r.stderr
    assert "a.md" in r.stdout
    assert reg.exists()


def test_mem_prune_previews_then_deletes_obsolete(tmp_path):
    import os

    proj = tmp_path / "proj"
    proj.mkdir()
    _txn(tmp_path, "-enc", str(proj))
    _mem(tmp_path, "-enc", "gone", "Bug — FIXED 2026-01-01")
    reg = tmp_path / "registry.json"
    env = {**os.environ, "SESSION_TRACKER_PROJECTS_DIR": str(tmp_path)}
    _run("mem-scan", "--registry", str(reg), env=env)
    prev = _run("mem-prune", "--registry", str(reg), env=env)
    assert "fixed" in prev.stdout and "preview" in prev.stdout.lower()
    assert (tmp_path / "-enc" / "memory" / "gone.md").exists()  # preview: not deleted
    subprocess.run(
        [sys.executable, "-m", "ringleader", "mem-prune", "--registry", str(reg), "--delete"],
        input="y\n",
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert not (tmp_path / "-enc" / "memory" / "gone.md").exists()  # deleted


def test_mark_mem_reviewed_unknown_path_exits_nonzero(tmp_path):
    reg = tmp_path / "registry.json"
    r = _run("mark-mem-reviewed", str(tmp_path / "nope.md"), "--registry", str(reg))
    assert r.returncode != 0
    assert r.stderr.strip() != ""


def test_mem_cleaned_selector_matching(tmp_path):
    import os

    proj = tmp_path / "proj"
    proj.mkdir()
    _txn(tmp_path, "-enc", str(proj))
    _mem(tmp_path, "-enc", "a", "durable note about ports")
    reg = tmp_path / "registry.json"
    env = {**os.environ, "SESSION_TRACKER_PROJECTS_DIR": str(tmp_path)}
    r = _run("mem-scan", "--registry", str(reg), env=env)
    assert r.returncode == 0, r.stderr

    miss = _run("mem-cleaned", "no-such-project", "--registry", str(reg), env=env)
    assert miss.returncode != 0
    assert "nothing to track" in miss.stderr

    hit = _run("mem-cleaned", str(proj), "--registry", str(reg), env=env)
    assert hit.returncode == 0, hit.stderr


def test_mem_cleaned_roundtrips_through_overview(tmp_path):
    import os

    proj = tmp_path / "proj"
    proj.mkdir()
    _txn(tmp_path, "-enc", str(proj))
    _mem(tmp_path, "-enc", "a", "durable note about ports")
    reg = tmp_path / "registry.json"
    env = {**os.environ, "SESSION_TRACKER_PROJECTS_DIR": str(tmp_path)}
    r = _run("mem-scan", "--registry", str(reg), env=env)
    assert r.returncode == 0, r.stderr

    # No git identity in this tmp project, so overview.group_key falls back to the
    # raw `project` path — passing that same path as the selector means the stamped
    # key equals the group_key, and the round-trip works without a real git repo.
    hit = _run("mem-cleaned", str(proj), "--registry", str(reg), env=env)
    assert hit.returncode == 0, hit.stderr

    ov = _run("overview", "--memory", "--no-scan", "--registry", str(reg), env=env)
    assert ov.returncode == 0, ov.stderr
    assert "[cleaned " in ov.stdout
    assert "[uncleaned]" not in ov.stdout


def test_mem_prune_delete_trims_memory_index(tmp_path):
    import os

    proj = tmp_path / "proj"
    proj.mkdir()
    _txn(tmp_path, "-enc", str(proj))
    _mem(tmp_path, "-enc", "obsolete", "Bug — FIXED 2026-01-01")
    mem_dir = tmp_path / "-enc" / "memory"
    (mem_dir / "MEMORY.md").write_text("- [x](obsolete.md)\n", encoding="utf-8")
    reg = tmp_path / "registry.json"
    env = {**os.environ, "SESSION_TRACKER_PROJECTS_DIR": str(tmp_path)}
    _run("mem-scan", "--registry", str(reg), env=env)

    dele = subprocess.run(
        [
            sys.executable,
            "-m",
            "ringleader",
            "mem-prune",
            "--registry",
            str(reg),
            "--project",
            str(proj),
            "--delete",
        ],
        input="y\n",
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert dele.returncode == 0, dele.stderr
    assert not (mem_dir / "obsolete.md").exists()
    index_text = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "obsolete.md" not in index_text
