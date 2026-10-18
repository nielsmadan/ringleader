from pathlib import Path

from ringleader import memory


def test_parse_frontmatter_splits_meta_and_body():
    text = (
        "---\nname: foo\ndescription: a thing\nmetadata:\n  type: feedback\n---\n\nThe body line.\n"
    )
    meta, body = memory.parse_frontmatter(text)
    assert meta["name"] == "foo"
    assert meta["description"] == "a thing"
    assert meta["metadata"]["type"] == "feedback"
    assert body.strip() == "The body line."


def test_parse_frontmatter_no_frontmatter():
    meta, body = memory.parse_frontmatter("just a bullet\n")
    assert meta == {}
    assert body.strip() == "just a bullet"


def test_detect_obsolete_flags_fixed_and_status_snapshot():
    assert "fixed" in memory.detect_obsolete(
        "Bug X — FIXED 2026-06-10", repo_gone=False, repo_doc_text=None
    )
    assert "status-snapshot" in memory.detect_obsolete(
        "Status (as of 2026-06-23): 23 tests passing", repo_gone=False, repo_doc_text=None
    )


def test_detect_obsolete_repo_gone():
    assert "repo-gone" in memory.detect_obsolete("anything", repo_gone=True, repo_doc_text=None)


def test_detect_obsolete_already_in_doc():
    body = "Always run yarn check-all before done."
    doc = "# CLAUDE.md\n\nAlways run yarn check-all before done.\n"
    assert "already-in-doc" in memory.detect_obsolete(body, repo_gone=False, repo_doc_text=doc)


def test_detect_obsolete_clean_body_has_no_signals():
    assert (
        memory.detect_obsolete(
            "A durable convention about ports.", repo_gone=False, repo_doc_text="# unrelated\n"
        )
        == []
    )


def test_parse_index_links_excludes_memory_md():
    text = "# Memory\n\n- [a](a.md) — x\n- [b](sub/b.md) — y\n- [self](MEMORY.md)\n"
    assert memory.parse_index_links(text) == ["a.md", "sub/b.md"]


def test_rewrite_index_without_drops_matching_lines():
    text = "# Memory\n\n- [a](a.md) — x\n- [b](sub/b.md) — y\n- [self](MEMORY.md)\n"
    result = memory.rewrite_index_without(text, {"a.md"})
    assert "[a](a.md)" not in result
    assert "[b](sub/b.md)" in result
    assert "# Memory" in result
    assert "a.md" not in result
    assert "b.md" in result
    assert result.endswith("\n") and not result.endswith("\n\n")


def _memory_file(root: Path, encoded: str, slug: str, body: str, frontmatter=True):
    d = root / encoded / "memory"
    d.mkdir(parents=True, exist_ok=True)
    fm = (
        f"---\nname: {slug}\ndescription: d\nmetadata:\n  type: feedback\n---\n\n"
        if frontmatter
        else ""
    )
    (d / f"{slug}.md").write_text(fm + body + "\n", encoding="utf-8")


def _transcript(root: Path, encoded: str, cwd: str):
    import json

    (root / encoded).mkdir(parents=True, exist_ok=True)
    (root / encoded / "s.jsonl").write_text(
        json.dumps({"type": "user", "cwd": cwd, "message": {"role": "user", "content": "hi"}})
        + "\n",
        encoding="utf-8",
    )


def test_scan_memory_builds_entries_with_git_and_signals(tmp_path):
    from ringleader.gitinfo import GitProject

    enc = "-Users-me-proj"
    cwd = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "CLAUDE.md").write_text("# CLAUDE.md\n\nRun yarn check-all before done.\n")
    _transcript(tmp_path, enc, cwd)
    _memory_file(tmp_path, enc, "keep-me", "A durable convention about ports.")
    _memory_file(tmp_path, enc, "already", "Run yarn check-all before done.")

    def fake(c):
        return GitProject(
            root=cwd, remote="github.com/me/proj", key="github.com/me/proj", label="proj"
        )

    entries = memory.scan_memory(tmp_path, git_resolver=fake)
    by_name = {e.name: e for e in entries}
    assert by_name["keep-me"].git_project_label == "proj"
    assert by_name["keep-me"].project == cwd
    assert by_name["keep-me"].obsolete_signals == []
    assert "already-in-doc" in by_name["already"].obsolete_signals


def test_scan_memory_marks_repo_gone(tmp_path):
    enc = "-Users-me-ghost"
    _transcript(tmp_path, enc, str(tmp_path / "ghost-does-not-exist"))
    _memory_file(tmp_path, enc, "orphan", "some fact")
    entries = memory.scan_memory(tmp_path, git_resolver=lambda c: None)
    assert entries[0].repo_gone is True
    assert "repo-gone" in entries[0].obsolete_signals


def test_scan_memory_ignores_index_file(tmp_path):
    enc = "-Users-me-proj"
    _transcript(tmp_path, enc, str(tmp_path))
    d = tmp_path / enc / "memory"
    d.mkdir(parents=True)
    (d / "MEMORY.md").write_text("# Memory\n- [x](x.md)\n")
    (d / "x.md").write_text("real entry\n")
    entries = memory.scan_memory(tmp_path, git_resolver=lambda c: None)
    assert [e.path.split("/")[-1] for e in entries] == ["x.md"]


def test_scan_memory_resolves_each_project_cwd_concurrently(tmp_path):
    import threading

    from ringleader.gitinfo import GitProject

    barrier = threading.Barrier(2, timeout=5)
    for name in ("one", "two"):
        cwd = tmp_path / name
        cwd.mkdir()
        _transcript(tmp_path / "projects", f"-{name}", str(cwd))
        _memory_file(tmp_path / "projects", f"-{name}", f"{name}-fact", "some fact")

    def resolver(c):
        barrier.wait()
        return GitProject(root=c, remote=None, key=c, label=Path(c).name)

    entries = memory.scan_memory(tmp_path / "projects", git_resolver=resolver)

    assert sorted(e.git_project_label for e in entries) == ["one", "two"]


def test_scan_memory_reuses_a_git_identity_a_session_thread_checked_recently(tmp_path):
    from datetime import UTC, datetime, timedelta

    cwd = tmp_path / "proj"
    (cwd / ".git").mkdir(parents=True)
    now = datetime.fromtimestamp((cwd / ".git").stat().st_ctime, UTC) + timedelta(hours=3)
    _transcript(tmp_path / "projects", "-proj", str(cwd))
    _memory_file(tmp_path / "projects", "-proj", "fact", "some fact")
    thread = {
        "sessionId": "s",
        "project": str(cwd),
        "gitProjectKey": "github.com/me/recorded",
        "gitProjectLabel": "recorded",
        "gitRoot": str(cwd),
        "gitCheckedAt": (now - timedelta(hours=1)).isoformat(),
    }
    calls = []

    entries = memory.scan_memory(
        tmp_path / "projects", git_resolver=calls.append, recorded_threads=[thread], now=now
    )

    assert calls == []
    assert entries[0].git_project_label == "recorded"
