import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ringleader import registry
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
    assert ns.mtime.endswith("+00:00")
    assert "do thing" in ns.tail_text


def test_scan_missing_root_returns_empty(tmp_path):
    scanned = scan(sources=[ClaudeCodeSource(projects_dir=tmp_path / "does-not-exist")])
    assert scanned == []


def test_scan_reuses_legacy_file_backed_cache_without_source_ref(tmp_path, monkeypatch):
    proj = tmp_path / "-Users-me-proj"
    _session(proj, "legacy", "/Users/me/proj")
    source = ClaudeCodeSource(projects_dir=tmp_path)
    first = scan(sources=[source], git_resolver=lambda cwd: None)
    reg = registry.reconcile(registry.empty_registry(), first, "2026-08-30T00:00:00+00:00")
    reg["threads"][0].pop("sourceRef")

    def fail_parse(ref):
        raise AssertionError(f"cache miss for {ref}")

    monkeypatch.setattr(source, "parse", fail_parse)

    cached = scan(sources=[source], cached_threads=reg["threads"], git_resolver=lambda cwd: None)

    assert cached[0].source_ref == "legacy"


def test_scan_skips_invalid_utf8_file(tmp_path):
    proj = tmp_path / "-Users-me-proj"
    proj.mkdir(parents=True, exist_ok=True)
    _session(proj, "dddddddd-0000-0000-0000-000000000001", "/Users/me/proj", "valid content")
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
            clone_url="git@github.com:me/proj.git",
        )

    scanned = scan(sources=[ClaudeCodeSource(projects_dir=tmp_path)], git_resolver=fake)
    assert scanned[0].git_project_key == "github.com/me/proj"
    assert scanned[0].git_project_label == "proj"
    assert scanned[0].git_clone_url == "git@github.com:me/proj.git"


def test_active_sources_autodetects_by_root(tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    pi = tmp_path / "pi"
    claude.mkdir()
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(claude))
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(codex))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("PI_SESSIONS_DIR", str(pi))
    from ringleader import scanner

    names = {s.name for s in scanner.active_sources()}
    assert names == {"claude-code"}
    codex.mkdir()
    assert {s.name for s in scanner.active_sources()} == {"claude-code", "codex"}
    pi.mkdir()
    assert {s.name for s in scanner.active_sources()} == {"claude-code", "codex", "pi"}


def test_unreadable_source_root_is_unavailable_not_absent(tmp_path, monkeypatch, capsys):
    from ringleader import scanner

    root = tmp_path / "blocked"
    source = SimpleNamespace(name="blocked", root=root)
    real_stat = Path.stat

    def denied_stat(path, *args, **kwargs):
        if path == root:
            raise PermissionError("permission denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    monkeypatch.setattr(scanner, "_new_sources", lambda: [source])
    unavailable_sources = set()

    assert scanner.active_sources(unavailable_sources=unavailable_sources) == []
    assert unavailable_sources == {"blocked"}
    assert str(root) in capsys.readouterr().err

    cached = {"source": "blocked", "sessionId": "s1"}
    reg = registry.empty_registry()
    reg["threads"] = [cached]
    reconciled = registry.reconcile(
        reg,
        [],
        "2026-08-31T00:00:00+00:00",
        unavailable_sources=unavailable_sources,
    )
    assert reconciled["threads"] == [cached]
    assert reconciled["deleted"] == []


def test_capabilities_declared():
    from ringleader.sources.claude_code import ClaudeCodeSource
    from ringleader.sources.codex import CodexSource

    assert "applies_names" in ClaudeCodeSource().capabilities
    assert "applies_names" in CodexSource().capabilities


def test_source_by_name():
    from ringleader import scanner

    assert scanner.source_by_name("codex").name == "codex"
    assert scanner.source_by_name("nope") is None
