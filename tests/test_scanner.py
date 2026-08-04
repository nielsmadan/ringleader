import json
from pathlib import Path
from unittest.mock import patch

from ringleader.scanner import scan
from ringleader.sources.base import NormalizedSession
from ringleader.sources.claude_code import ClaudeCodeSource


def _session(dir_: Path, sid: str, cwd: str, content="hello"):
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": cwd, "message": {"role": "user", "content": content}})
        + "\n",
        encoding="utf-8",
    )


def test_scan_builds_normalized_sessions(tmp_path):
    proj = tmp_path / "-Users-me-proj"
    _session(proj, "bbbbbbbb-0000-0000-0000-000000000002", "/Users/me/proj", "do thing")
    scanned = scan(sources=[ClaudeCodeSource(projects_dir=tmp_path)])
    assert len(scanned) == 1
    ns = scanned[0]
    assert isinstance(ns, NormalizedSession)
    assert ns.session_id == "bbbbbbbb-0000-0000-0000-000000000002"
    assert ns.cwd == "/Users/me/proj"
    assert ns.size_bytes > 0
    assert ns.mtime.endswith("+00:00")  # tz-aware iso
    assert "do thing" in ns.tail_text


def test_scan_missing_root_returns_empty(tmp_path):
    scanned = scan(sources=[ClaudeCodeSource(projects_dir=tmp_path / "does-not-exist")])
    assert scanned == []


def test_scan_skips_invalid_utf8_file(tmp_path):
    proj = tmp_path / "-Users-me-proj"
    proj.mkdir(parents=True, exist_ok=True)
    # valid session
    _session(proj, "dddddddd-0000-0000-0000-000000000001", "/Users/me/proj", "valid content")
    # file with invalid UTF-8 bytes — should NOT abort the scan
    bad = proj / "eeeeeeee-0000-0000-0000-000000000002.jsonl"
    bad.write_bytes(b"\xff\xfe not utf8\n")
    scanned = scan(sources=[ClaudeCodeSource(projects_dir=tmp_path)])
    session_ids = [ns.session_id for ns in scanned]
    assert "dddddddd-0000-0000-0000-000000000001" in session_ids
    assert len(scanned) >= 1


def test_scan_skips_oserror_file(tmp_path):
    proj = tmp_path / "-Users-me-proj"
    proj.mkdir(parents=True, exist_ok=True)
    _session(proj, "ffffffff-0000-0000-0000-000000000001", "/Users/me/proj", "good session")
    _session(proj, "00000000-0000-0000-0000-000000000002", "/Users/me/proj", "bad session")

    bad_path = proj / "00000000-0000-0000-0000-000000000002.jsonl"

    original_parse = __import__(
        "ringleader.transcript", fromlist=["parse_transcript"]
    ).parse_transcript

    def patched_parse(path, **kwargs):
        if Path(path) == bad_path:
            raise OSError("simulated permission denied")
        return original_parse(path, **kwargs)

    with patch("ringleader.sources.claude_code.parse_transcript", side_effect=patched_parse):
        scanned = scan(sources=[ClaudeCodeSource(projects_dir=tmp_path)])

    session_ids = [ns.session_id for ns in scanned]
    assert "ffffffff-0000-0000-0000-000000000001" in session_ids
    assert "00000000-0000-0000-0000-000000000002" not in session_ids


def test_scan_attaches_injected_git_project(tmp_path):
    from ringleader.gitinfo import GitProject

    proj = tmp_path / "-Users-me-proj"
    _session(proj, "cccccccc-0000-0000-0000-000000000009", "/Users/me/proj")

    def fake(cwd):
        return GitProject(
            root="/Users/me/proj",
            remote="github.com/me/proj",
            key="github.com/me/proj",
            label="proj",
        )

    scanned = scan(sources=[ClaudeCodeSource(projects_dir=tmp_path)], git_resolver=fake)
    assert scanned[0].git_project_key == "github.com/me/proj"
    assert scanned[0].git_project_label == "proj"


def test_active_sources_autodetects_by_root(tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    claude.mkdir()  # only claude root exists
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(claude))
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(codex))
    from ringleader import scanner

    names = {s.name for s in scanner.active_sources()}
    assert names == {"claude-code"}
    codex.mkdir()
    assert {s.name for s in scanner.active_sources()} == {"claude-code", "codex"}


def test_capabilities_declared():
    from ringleader.sources.claude_code import ClaudeCodeSource
    from ringleader.sources.codex import CodexSource

    assert "applies_names" in ClaudeCodeSource().capabilities
    assert "applies_names" in CodexSource().capabilities


def test_source_by_name():
    from ringleader import scanner

    assert scanner.source_by_name("codex").name == "codex"
    assert scanner.source_by_name("nope") is None
