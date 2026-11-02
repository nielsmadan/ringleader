import json
import os
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest
from ringleader import refresh, registry, scanner
from ringleader.gitinfo import GitProject, GitRun, lookup_git_project
from ringleader.sources.claude_code import ClaudeCodeSource
from ringleader.sources.codex import CodexSource


def _projects_dir(tmp, sid="s1"):
    """Minimal ~/.claude/projects layout: <encoded>/<uuid>.jsonl"""
    proj = tmp / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True)
    (proj / f"{sid}.jsonl").write_text(
        f'{{"sessionId":"{sid}","cwd":"/Users/me/proj","type":"user"}}\n', encoding="utf-8"
    )
    return tmp / "projects"


def _codex_sessions_dir(tmp, sid="c1"):
    """Minimal ~/.codex/sessions layout: <anything>/<uuid>.jsonl"""
    sessions = tmp / "codex-sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{sid}.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"session_id": sid, "cwd": "/Users/me/cx"}})
        + "\n"
        + json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"text": "hello from codex"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return sessions


def _registry(tmp, threads):
    p = tmp / "registry.json"
    p.write_text(
        json.dumps({"version": 1, "lastScan": None, "threads": threads, "tombstones": []}),
        encoding="utf-8",
    )
    return p


def test_delta_note_variants():
    assert refresh.delta_note(0, 0) is None
    assert refresh.delta_note(2, 0) == "(scanning… 2 new)"
    assert refresh.delta_note(0, 1) == "(scanning… 1 gone)"
    assert refresh.delta_note(2, 1) == "(scanning… 2 new, 1 gone)"


def test_auto_refresh_adds_new_session_and_saves(tmp_path):
    root = _projects_dir(tmp_path, "s1")
    reg_path = _registry(tmp_path, [])
    reg, note = refresh.auto_refresh(
        reg_path, "2026-07-19T00:00:00+00:00", projects_root=root, git_resolver=lambda cwd: None
    )
    assert note == "(scanning… 1 new)"
    assert [t["sessionId"] for t in reg["threads"]] == ["s1"]
    # persisted, not just returned
    saved = json.loads(reg_path.read_text())
    assert [t["sessionId"] for t in saved["threads"]] == ["s1"]
    assert saved["lastScan"] == "2026-07-19T00:00:00+00:00"


def test_auto_refresh_no_change_returns_none(tmp_path):
    root = _projects_dir(tmp_path, "s1")
    reg_path = _registry(tmp_path, [])
    refresh.auto_refresh(
        reg_path, "2026-07-19T00:00:00+00:00", projects_root=root, git_resolver=lambda cwd: None
    )
    _, note = refresh.auto_refresh(
        reg_path, "2026-07-19T00:01:00+00:00", projects_root=root, git_resolver=lambda cwd: None
    )
    assert note is None


def test_auto_refresh_reuses_unchanged_session_without_parsing(tmp_path, monkeypatch):
    root = _projects_dir(tmp_path, "s1")
    reg_path = _registry(tmp_path, [])
    source = ClaudeCodeSource(projects_dir=root)
    real_parse = source.parse
    parsed = []

    def record_parse(ref):
        parsed.append(ref.file)
        return real_parse(ref)

    monkeypatch.setattr(source, "parse", record_parse)
    refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:00:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: None,
    )
    reg, _ = refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:01:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: None,
    )

    assert parsed == [reg["threads"][0]["file"]]


def test_auto_refresh_invalidates_cache_when_parser_version_changes(tmp_path, monkeypatch):
    root = _projects_dir(tmp_path, "s1")
    reg_path = _registry(tmp_path, [])
    source = ClaudeCodeSource(projects_dir=root)
    real_parse = source.parse
    parsed = []

    def record_parse(ref):
        parsed.append(ref.file)
        return real_parse(ref)

    monkeypatch.setattr(source, "parse", record_parse)
    monkeypatch.setattr(source, "parser_version", 1, raising=False)
    refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:00:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: None,
    )
    monkeypatch.setattr(source, "parser_version", 2)

    reg, _ = refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:01:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: None,
    )

    assert parsed == [reg["threads"][0]["file"], reg["threads"][0]["file"]]
    assert reg["threads"][0]["cacheVersion"] == 2


def test_auto_refresh_reparses_same_size_rewrite_with_restored_mtime(tmp_path):
    root = _projects_dir(tmp_path, "s1")
    transcript = next(root.glob("*/*.jsonl"))
    before = (
        '{"sessionId":"s1","cwd":"/Users/me/proj","type":"user",'
        '"message":{"role":"user","content":"before"}}\n'
    )
    after = before.replace("before", "update")
    transcript.write_text(before, encoding="utf-8")
    reg_path = _registry(tmp_path, [])
    source = ClaudeCodeSource(projects_dir=root)
    refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:00:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: None,
    )
    original_stat = transcript.stat()
    transcript.write_text(after, encoding="utf-8")
    os.utime(transcript, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    reg, _ = refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:01:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: None,
    )

    assert reg["threads"][0]["lastPrompt"] == "update"


def test_auto_refresh_updates_git_identity_on_cache_hit(tmp_path):
    root = _projects_dir(tmp_path, "s1")
    reg_path = _registry(tmp_path, [])
    source = ClaudeCodeSource(projects_dir=root)
    resolved = [
        GitProject(root="/old", remote="example/old", key="example/old", label="old"),
        GitProject(root="/new", remote="example/new", key="example/new", label="new"),
    ]

    refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:00:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: resolved.pop(0),
    )
    reg, _ = refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:01:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: resolved.pop(0),
    )

    thread = reg["threads"][0]
    assert thread["gitProjectKey"] == "example/new"
    assert thread["gitProjectLabel"] == "new"
    assert thread["gitRoot"] == "/new"
    assert thread["gitRemote"] == "example/new"


def test_auto_refresh_cache_roundtrip_preserves_metadata(tmp_path):
    root = _projects_dir(tmp_path, "s1")
    reg_path = _registry(tmp_path, [])
    source = ClaudeCodeSource(projects_dir=root)
    git_project = GitProject(
        root="/Users/me/proj",
        remote="example/proj",
        key="example/proj",
        label="proj",
    )
    refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:00:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: git_project,
    )
    saved = json.loads(reg_path.read_text(encoding="utf-8"))
    thread = saved["threads"][0]
    assert thread.get("cacheVersion") == source.parser_version
    assert thread.get("fileFingerprint")
    expected = {
        "title": "custom title",
        "aiTitle": "generated title",
        "summary": "summary",
        "lastPrompt": "last prompt",
        "messageCount": 7,
        "gitBranch": "feature/cache",
        "gitProjectKey": "example/proj",
        "gitProjectLabel": "proj",
        "gitRoot": "/Users/me/proj",
        "gitRemote": "example/proj",
    }
    thread.update(expected)
    reg_path.write_text(json.dumps(saved), encoding="utf-8")

    reg, _ = refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:01:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: git_project,
    )

    actual = reg["threads"][0]
    assert {key: actual[key] for key in expected} == expected
    assert actual["cacheVersion"] == source.parser_version
    assert actual["fileFingerprint"] == thread["fileFingerprint"]


def test_auto_refresh_reparses_session_when_size_changes(tmp_path):
    root = _projects_dir(tmp_path, "s1")
    reg_path = _registry(tmp_path, [])
    source = ClaudeCodeSource(projects_dir=root)
    refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:00:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: None,
    )
    transcript = next(root.glob("*/*.jsonl"))
    original_stat = transcript.stat()
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "user",
                    "cwd": "/Users/me/proj",
                    "message": {"role": "user", "content": "updated prompt"},
                }
            )
            + "\n"
        )
    os.utime(transcript, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    reg, _ = refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:01:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: None,
    )

    assert reg["threads"][0]["lastPrompt"] == "updated prompt"


def test_auto_refresh_reparses_session_when_mtime_changes(tmp_path, monkeypatch):
    root = _projects_dir(tmp_path, "s1")
    reg_path = _registry(tmp_path, [])
    source = ClaudeCodeSource(projects_dir=root)
    real_parse = source.parse
    parsed = []

    def record_parse(ref):
        parsed.append(ref.file)
        return real_parse(ref)

    monkeypatch.setattr(source, "parse", record_parse)
    refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:00:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: None,
    )
    transcript = next(root.glob("*/*.jsonl"))
    original_stat = transcript.stat()
    os.utime(
        transcript,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000),
    )

    refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:01:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: None,
    )

    assert parsed == [str(transcript), str(transcript)]


def test_auto_refresh_updates_codex_title_without_reparsing_transcript(tmp_path, monkeypatch):
    sessions = _codex_sessions_dir(tmp_path, "c1")
    index = tmp_path / "session_index.jsonl"
    index.write_text(
        json.dumps({"id": "c1", "thread_name": "first", "updated_at": "earlier"}) + "\n",
        encoding="utf-8",
    )
    reg_path = _registry(tmp_path, [])
    source = CodexSource(sessions_dir=sessions)
    real_parse = source.parse
    parsed = []

    def record_parse(ref):
        parsed.append(ref.file)
        return real_parse(ref)

    monkeypatch.setattr(source, "parse", record_parse)
    refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:00:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: None,
    )
    with index.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "c1", "thread_name": "renamed", "updated_at": "later"}) + "\n")

    reg, _ = refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:01:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: None,
    )

    assert reg["threads"][0]["title"] == "renamed"
    assert parsed == [reg["threads"][0]["file"]]


def test_auto_refresh_discovers_additions_and_removals_with_warm_cache(tmp_path):
    root = _projects_dir(tmp_path, "s1")
    reg_path = _registry(tmp_path, [])
    source = ClaudeCodeSource(projects_dir=root)
    refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:00:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: None,
    )
    project = next(root.iterdir())
    added = project / "s2.jsonl"
    added.write_text(
        '{"sessionId":"s2","cwd":"/Users/me/proj","type":"user"}\n',
        encoding="utf-8",
    )

    reg, _ = refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:01:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: None,
    )
    assert {thread["sessionId"] for thread in reg["threads"]} == {"s1", "s2"}

    (project / "s1.jsonl").unlink()
    reg, _ = refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:02:00+00:00",
        sources=[source],
        git_resolver=lambda cwd: None,
    )
    assert [thread["sessionId"] for thread in reg["threads"]] == ["s2"]


def test_auto_refresh_with_explicit_sources_scans_all_of_them(tmp_path):
    claude_root = _projects_dir(tmp_path, "s1")
    codex_root = _codex_sessions_dir(tmp_path, "c1")
    reg_path = _registry(tmp_path, [])
    sources = [
        ClaudeCodeSource(projects_dir=claude_root),
        CodexSource(sessions_dir=codex_root),
    ]
    reg, note = refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:00:00+00:00",
        sources=sources,
        git_resolver=lambda cwd: None,
    )
    assert note == "(scanning… 2 new)"
    by_id = {t["sessionId"]: t for t in reg["threads"]}
    assert set(by_id) == {"s1", "c1"}
    assert by_id["s1"]["source"] == "claude-code"
    assert by_id["c1"]["source"] == "codex"


def _titled_projects_dir(tmp, sid, title):
    proj = tmp / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{sid}.jsonl").write_text(
        json.dumps({"sessionId": sid, "cwd": "/Users/me/proj", "type": "user"})
        + "\n"
        + json.dumps({"type": "custom-title", "customTitle": title})
        + "\n",
        encoding="utf-8",
    )
    return tmp / "projects"


def test_auto_refresh_stamps_matching_sessions_with_the_filter(tmp_path):
    root = _titled_projects_dir(tmp_path, "s1", "[cadet-city] reviewer")
    _titled_projects_dir(tmp_path, "s2", "real work")
    reg_path = _registry(tmp_path, [])

    reg, _ = refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:00:00+00:00",
        projects_root=root,
        git_resolver=lambda cwd: None,
        name_filters=["[cadet-city]*"],
    )

    by_id = {t["sessionId"]: t for t in reg["threads"]}
    assert by_id["s1"]["filteredBy"] == "[cadet-city]*"
    assert by_id["s2"]["filteredBy"] is None
    saved = json.loads(reg_path.read_text(encoding="utf-8"))
    assert {t["sessionId"]: t["filteredBy"] for t in saved["threads"]} == {
        "s1": "[cadet-city]*",
        "s2": None,
    }


def test_auto_refresh_keeps_filtered_sessions_in_the_registry(tmp_path):
    root = _titled_projects_dir(tmp_path, "s1", "[cadet-city] reviewer")
    reg_path = _registry(tmp_path, [])

    reg, note = refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:00:00+00:00",
        projects_root=root,
        git_resolver=lambda cwd: None,
        name_filters=["[cadet-city]*"],
    )

    assert note == "(scanning… 1 new)"
    assert [t["sessionId"] for t in reg["threads"]] == ["s1"]
    assert reg["deleted"] == []


def test_auto_refresh_restamps_when_the_filter_changes(tmp_path):
    root = _titled_projects_dir(tmp_path, "s1", "[cadet-city] reviewer")
    reg_path = _registry(tmp_path, [])
    refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:00:00+00:00",
        projects_root=root,
        git_resolver=lambda cwd: None,
        name_filters=["[cadet-city]*"],
    )

    reg, _ = refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:01:00+00:00",
        projects_root=root,
        git_resolver=lambda cwd: None,
        name_filters=[],
    )

    assert reg["threads"][0]["filteredBy"] is None


def test_refresh_registry_reports_unavailable_sources(tmp_path, monkeypatch):
    reg_path = _registry(tmp_path, [])

    def active_sources(*, unavailable_sources, recorded_threads):
        unavailable_sources.add("opencode")
        return []

    monkeypatch.setattr(refresh.scanner, "active_sources", active_sources)

    refreshed = refresh.refresh_registry(reg_path, "2026-10-15T00:00:00+00:00", name_filters=[])

    assert refreshed.unavailable_sources == frozenset({"opencode"})
    assert refreshed.note is None
    assert refreshed.reg["threads"] == []


def _claude_session(root, cwd, sid, *, filed_under=None):
    store = ClaudeCodeSource(projects_dir=root).storage_for(filed_under or cwd)
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{sid}.jsonl"
    path.write_text(json.dumps({"sessionId": sid, "cwd": cwd, "type": "user"}) + "\n")
    return path


def _scoped(reg_path, root, cwd, *, recursive=False, persist=True, sources=None):
    return refresh.scoped_refresh(
        reg_path,
        "2026-07-20T00:00:00+00:00",
        cwd=cwd,
        recursive=recursive,
        sources=sources or [ClaudeCodeSource(projects_dir=root)],
        git_resolver=lambda cwd: None,
        name_filters=[],
        persist=persist,
    )


def _fully_scanned(tmp_path, root, *, sources=None):
    reg_path = _registry(tmp_path, [])
    refresh.auto_refresh(
        reg_path,
        "2026-07-19T00:00:00+00:00",
        sources=sources or [ClaudeCodeSource(projects_dir=root)],
        git_resolver=lambda cwd: None,
        name_filters=[],
    )
    return reg_path


def _rollout(sessions, sid, cwd):
    sessions.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "payload": {"session_id": sid, "cwd": cwd}}
    (sessions / f"{sid}.jsonl").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    return sessions


def _identities(reg):
    return sorted(registry.identity(t) for t in reg["threads"])


def test_scoped_refresh_registers_a_new_session_and_keeps_what_it_did_not_look_at(tmp_path):
    root = tmp_path / "projects"
    _claude_session(root, "/Users/me/proj", "s1")
    gone_elsewhere = _claude_session(root, "/Users/me/other", "s2")
    reg_path = _fully_scanned(tmp_path, root)
    gone_elsewhere.unlink()
    _claude_session(root, "/Users/me/proj", "s3")

    reg, _ = _scoped(reg_path, root, "/Users/me/proj")

    assert sorted(t["sessionId"] for t in reg["threads"]) == ["s1", "s2", "s3"]
    saved = json.loads(reg_path.read_text())
    assert sorted(t["sessionId"] for t in saved["threads"]) == ["s1", "s2", "s3"]
    assert saved["deleted"] == []
    assert saved["lastScan"] == "2026-07-19T00:00:00+00:00"
    assert saved["sourcesScannedAt"] == {"claude-code": "2026-07-19T00:00:00+00:00"}


def test_scoped_refresh_measures_change_from_the_last_session_scan_not_a_memory_scan(tmp_path):
    root = tmp_path / "projects"
    _claude_session(root, "/Users/me/proj", "s1")
    reg_path = _fully_scanned(tmp_path, root)
    _claude_session(root, "/Users/me/proj", "s2")
    reg = registry.load_registry(reg_path)
    registry.commit_registry(
        reg_path, registry.reconcile_memory(reg, [], "2099-01-01T00:00:00+00:00")
    )

    reg, _ = _scoped(reg_path, root, "/Users/me/proj")

    assert sorted(t["sessionId"] for t in reg["threads"]) == ["s1", "s2"]


def test_scoped_refresh_without_persist_leaves_the_registry_file_as_it_was(tmp_path):
    root = tmp_path / "projects"
    _claude_session(root, "/Users/me/proj", "s1")
    reg_path = _fully_scanned(tmp_path, root)
    _claude_session(root, "/Users/me/proj", "s2")

    reg, _ = _scoped(reg_path, root, "/Users/me/proj", persist=False)

    assert sorted(t["sessionId"] for t in reg["threads"]) == ["s1", "s2"]
    saved = json.loads(reg_path.read_text())
    assert [t["sessionId"] for t in saved["threads"]] == ["s1"]


def test_scoped_refresh_reaches_below_its_directory_only_when_recursive(tmp_path):
    root = tmp_path / "projects"
    _claude_session(root, "/Users/me/proj", "s1")
    reg_path = _fully_scanned(tmp_path, root)
    _claude_session(root, "/Users/me/proj/sub", "s2")

    flat, _ = _scoped(reg_path, root, "/Users/me/proj")
    assert [t["sessionId"] for t in flat["threads"]] == ["s1"]

    deep, _ = _scoped(reg_path, root, "/Users/me/proj", recursive=True)
    assert sorted(t["sessionId"] for t in deep["threads"]) == ["s1", "s2"]


@pytest.mark.parametrize("last_scan", [None, "2099-01-01T00:00:00+00:00"])
def test_scoped_refresh_scans_everything_when_no_source_has_a_baseline(tmp_path, last_scan):
    """A registry from before per-source baselines has a `lastScan` a memory scan may have
    moved, which says nothing about what the sessions' sources last saw."""
    root = tmp_path / "projects"
    _claude_session(root, "/Users/me/proj", "s1")
    _claude_session(root, "/Users/me/other", "s2")
    reg_path = _registry(tmp_path, [])
    reg_path.write_text(json.dumps(json.loads(reg_path.read_text()) | {"lastScan": last_scan}))

    reg, full = _scoped(reg_path, root, "/Users/me/proj")

    assert full
    assert sorted(t["sessionId"] for t in reg["threads"]) == ["s1", "s2"]
    assert reg["sourcesScannedAt"] == {"claude-code": "2026-07-20T00:00:00+00:00"}


def test_scoped_refresh_follows_a_registered_session_filed_under_another_workspace(tmp_path):
    root = tmp_path / "projects"
    drifted = _claude_session(root, "/Users/me/proj/wt", "s1", filed_under="/Users/me/proj")
    reg_path = _fully_scanned(tmp_path, root)
    with drifted.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"sessionId": "s1", "cwd": "/Users/me/proj/wt", "type": "user"}))
    written = datetime(2026, 8, 1, tzinfo=UTC)
    os.utime(drifted, (written.timestamp(), written.timestamp()))

    reg, full = _scoped(reg_path, root, "/Users/me/proj/wt")

    assert not full
    assert [t["mtime"] for t in reg["threads"]] == [written.isoformat()]


def test_scoped_refresh_hands_each_source_only_its_own_known_files(tmp_path):
    root = tmp_path / "projects"
    _claude_session(root, "/Users/me/proj", "s1")
    codex = _rollout(tmp_path / "codex", "c1", "/Users/me/proj")

    def sources():
        return [ClaudeCodeSource(projects_dir=root), CodexSource(sessions_dir=codex)]

    reg_path = _fully_scanned(tmp_path, root, sources=sources())

    reg, _ = _scoped(reg_path, root, "/Users/me/proj", sources=sources())

    assert _identities(reg) == [("claude-code", "s1"), ("codex", "c1")]


def test_scoped_refresh_discovers_a_source_with_no_baseline_whole(tmp_path):
    root = tmp_path / "projects"
    _claude_session(root, "/Users/me/proj", "s1")
    reg_path = _fully_scanned(tmp_path, root)
    codex = _rollout(tmp_path / "codex", "c1", "/Users/me/elsewhere")
    sources = [ClaudeCodeSource(projects_dir=root), CodexSource(sessions_dir=codex)]

    reg, _ = _scoped(reg_path, root, "/Users/me/proj", sources=sources)

    assert _identities(reg) == [("claude-code", "s1"), ("codex", "c1")]


def _repo_and_clock(cwd):
    (cwd / ".git").mkdir(parents=True)
    after = datetime.fromtimestamp((cwd / ".git").stat().st_ctime, UTC) + timedelta(minutes=1)
    return lambda hours: (after + timedelta(hours=hours)).isoformat()


def test_a_rescan_reuses_the_git_identity_the_last_refresh_recorded(tmp_path):
    cwd = tmp_path / "proj"
    at = _repo_and_clock(cwd)
    proj = tmp_path / "projects" / "-proj"
    proj.mkdir(parents=True)
    (proj / "s1.jsonl").write_text(
        json.dumps({"sessionId": "s1", "cwd": str(cwd), "type": "user"}) + "\n", encoding="utf-8"
    )
    reg_path = _registry(tmp_path, [])
    calls = []

    def resolver(path):
        calls.append(path)
        return GitProject(root=path, remote=None, key=path, label="proj", clone_url=None)

    for now_iso in (at(0), at(1)):
        refresh.refresh_registry(
            reg_path, now_iso, projects_root=tmp_path / "projects", git_resolver=resolver
        )

    saved = json.loads(reg_path.read_text())["threads"][0]
    assert calls == [str(cwd)]
    assert (saved["gitProjectLabel"], saved["gitCheckedAt"]) == ("proj", at(0))


def _one_session_in(tmp_path, cwd, recorded):
    proj = tmp_path / "projects" / "-proj"
    proj.mkdir(parents=True)
    (proj / "s1.jsonl").write_text(
        json.dumps({"sessionId": "s1", "cwd": str(cwd), "type": "user"}) + "\n", encoding="utf-8"
    )
    thread = {
        "sessionId": "s1",
        "source": "claude-code",
        "project": str(cwd),
        "gitProjectKey": "github.com/me/proj",
        "gitProjectLabel": "proj",
        "gitRoot": str(cwd),
        "gitRemote": "github.com/me/proj",
        "gitCloneUrl": "git@github.com:me/proj.git",
        "gitCheckedAt": recorded,
    }
    return _registry(tmp_path, [thread])


def _git(answers):
    def run(cmd):
        return answers[tuple(cmd[3:])]

    return partial(lookup_git_project, runner=run)


def test_a_timed_out_remote_lookup_keeps_the_recorded_remote_identity(tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    reg_path = _one_session_in(tmp_path, cwd, "2026-07-17T00:00:00+00:00")
    flaky = _git(
        {
            ("rev-parse", "--show-toplevel"): GitRun(0, f"{cwd}\n"),
            ("config", "--get", "remote.origin.url"): GitRun(None),
        }
    )

    refresh.refresh_registry(
        reg_path,
        "2026-07-19T00:00:00+00:00",
        projects_root=tmp_path / "projects",
        git_resolver=flaky,
    )

    saved = json.loads(reg_path.read_text())["threads"][0]
    assert (saved["gitProjectKey"], saved["gitCheckedAt"]) == (
        "github.com/me/proj",
        "2026-07-17T00:00:00+00:00",
    )


def test_a_timed_out_toplevel_lookup_is_not_recorded_as_a_check(tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    reg_path = _one_session_in(tmp_path, cwd, None)
    calls = []

    def run(cmd):
        calls.append(cmd)
        return GitRun(None)

    for now_iso in ("2026-07-19T00:00:00+00:00", "2026-07-19T01:00:00+00:00"):
        refresh.refresh_registry(
            reg_path,
            now_iso,
            projects_root=tmp_path / "projects",
            git_resolver=partial(lookup_git_project, runner=run),
        )

    saved = json.loads(reg_path.read_text())["threads"][0]
    assert (saved["gitProjectKey"], saved["gitCheckedAt"]) == ("github.com/me/proj", None)
    assert len(calls) == 2


def test_a_directory_that_is_no_longer_a_repo_clears_the_recorded_identity(tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    reg_path = _one_session_in(tmp_path, cwd, "2026-07-18T23:00:00+00:00")
    not_a_repo = _git(
        {("rev-parse", "--show-toplevel"): GitRun(128, stderr="fatal: not a git repository")}
    )

    refresh.refresh_registry(
        reg_path,
        "2026-07-19T00:00:00+00:00",
        projects_root=tmp_path / "projects",
        git_resolver=not_a_repo,
    )

    saved = json.loads(reg_path.read_text())["threads"][0]
    assert [saved[k] for k in ("gitProjectKey", "gitRoot", "gitRemote", "gitCheckedAt")] == [
        None,
        None,
        None,
        "2026-07-19T00:00:00+00:00",
    ]


def test_refreshes_sharing_a_pool_reread_only_the_codex_headers_that_changed(tmp_path, monkeypatch):
    from ringleader.sources import codex

    sessions = _codex_sessions_dir(tmp_path, "c1")
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(sessions))
    reg_path = _registry(tmp_path, [])
    reads = []
    real_header = codex._header

    def counting_header(path):
        reads.append(path.name)
        return real_header(path)

    monkeypatch.setattr(codex, "_header", counting_header)
    pool = scanner.SourcePool()

    for minute in (0, 1):
        refresh.refresh_registry(
            reg_path,
            f"2026-07-19T00:0{minute}:00+00:00",
            git_resolver=lambda cwd: None,
            name_filters=[],
            pool=pool,
        )

    assert reads == ["c1.jsonl"]


@pytest.mark.parametrize("warm", [False, True])
def test_workspace_refresh_finds_an_old_fork_and_preserves_the_full_scan_clock(tmp_path, warm):
    root = tmp_path / "projects"
    _claude_session(root, "/Users/me/proj", "s1")
    elsewhere = _claude_session(root, "/Users/me/other", "elsewhere")
    path = _fully_scanned(tmp_path, root) if warm else _registry(tmp_path, [])
    before = registry.load_registry(path)
    fork = _claude_session(root, "/Users/me/proj", "fork")
    os.utime(fork, (1, 1))
    elsewhere.unlink()
    result = refresh.workspace_refresh(
        path,
        "2026-07-20T00:00:00+00:00",
        "/Users/me/proj",
        False,
        sources=[ClaudeCodeSource(projects_dir=root)],
        git_resolver=lambda cwd: None,
        name_filters=[],
    )
    assert sorted(t["sessionId"] for t in result.reg["threads"]) == (
        ["elsewhere", "fork", "s1"] if warm else ["fork", "s1"]
    )
    assert result.reg["lastScan"] == before["lastScan"]
    assert result.reg["sourcesScannedAt"] == before.get("sourcesScannedAt", {})
    assert result.reg["deleted"] == before["deleted"]


@pytest.mark.parametrize("recursive", [False, True])
def test_workspace_refresh_scopes_discovery_and_reuses_unchanged_parses(tmp_path, recursive):
    root = tmp_path / "projects"
    _claude_session(root, "/Users/me/proj", "s1")
    _claude_session(root, "/Users/me/proj/sub", "child")
    _claude_session(root, "/Users/me/elsewhere", "elsewhere")
    calls = []

    class CountingClaude(ClaudeCodeSource):
        def parse(self, ref):
            calls.append(ref.source_ref)
            return super().parse(ref)

    path = _registry(tmp_path, [])
    for _ in range(2):
        result = refresh.workspace_refresh(
            path,
            "2026-07-20T00:00:00+00:00",
            "/Users/me/proj",
            recursive,
            sources=[CountingClaude(projects_dir=root)],
            git_resolver=lambda cwd: None,
            name_filters=[],
        )
    expected = ["child", "s1"] if recursive else ["s1"]
    assert sorted(calls) == expected
    assert sorted(t["sessionId"] for t in result.reg["threads"]) == expected


def test_workspace_refresh_defers_unscoped_sources_and_can_leave_disk_unchanged(tmp_path):
    class Unscoped:
        name = "pi"
        capabilities = frozenset()

        def discover(self):
            raise AssertionError("the preview must not discover an unscoped source")

    root = tmp_path / "projects"
    _claude_session(root, "/Users/me/proj", "s1")
    path = _registry(tmp_path, [])
    before = path.read_bytes()
    result = refresh.workspace_refresh(
        path,
        "2026-07-20T00:00:00+00:00",
        "/Users/me/proj",
        False,
        sources=[ClaudeCodeSource(projects_dir=root), Unscoped()],
        git_resolver=lambda cwd: None,
        name_filters=[],
        persist=False,
    )
    assert [t["sessionId"] for t in result.reg["threads"]] == ["s1"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("mode", ["full", "scoped", "workspace"])
def test_refresh_retires_cached_droid_exec_runs_without_deleting_files(tmp_path, mode):
    from ringleader.sources.droid import DroidSource

    source = DroidSource(sessions_dir=tmp_path / "droid")
    workspace = source.storage_for("/work/repo")
    workspace.mkdir(parents=True)
    for sid in ("interactive", "exec"):
        (workspace / f"{sid}.jsonl").write_text(
            json.dumps({"type": "session_start", "id": sid, "cwd": "/work/repo"}) + "\n"
        )
    path = tmp_path / "registry.json"
    kwargs = {"sources": [source], "git_resolver": lambda cwd: None, "name_filters": []}
    refresh.auto_refresh(path, "2026-11-01T00:00:00+00:00", **kwargs)
    sidecar = workspace / "exec.settings.json"
    sidecar.write_text(
        json.dumps(
            {"tags": [{"name": "exec"}], "tokenUsage": {"inputTokens": 1200, "outputTokens": 300}}
        )
    )
    for file in workspace.iterdir():
        os.utime(file, (1, 1))
    before = {file.name: file.read_bytes() for file in workspace.iterdir()}
    now = "2026-11-02T00:00:00+00:00"
    if mode == "full":
        result, _ = refresh.auto_refresh(path, now, **kwargs)
    elif mode == "scoped":
        result, _ = refresh.scoped_refresh(path, now, cwd="/work/repo", recursive=False, **kwargs)
    else:
        result = refresh.workspace_refresh(path, now, "/work/repo", False, **kwargs).reg
    assert [thread["sessionId"] for thread in result["threads"]] == ["interactive"]
    assert [(row["sessionId"], row["reason"]) for row in result["deleted"]] == [
        ("exec", "excluded")
    ]
    assert {file.name: file.read_bytes() for file in workspace.iterdir()} == before
    assert registry.load_registry(path)["threads"] == result["threads"]
    from ringleader.usage.droid import DroidUsageReader

    usage = list(DroidUsageReader(sessions_dir=source.sessions_dir).records())
    assert [(r.session_id, r.input, r.output, r.total) for r in usage] == [
        ("exec", 1200, 300, 1500)
    ]
    sidecar.write_text('{"tags": []}')
    restored, _ = refresh.auto_refresh(path, now, **kwargs)
    assert {thread["sessionId"] for thread in restored["threads"]} == {"interactive", "exec"}


@pytest.mark.parametrize("mode", ["scoped", "workspace"])
def test_partial_refresh_exclusions_stay_in_workspace(tmp_path, monkeypatch, mode):
    from ringleader.sources.droid import DroidSource

    source = DroidSource(sessions_dir=tmp_path / "sessions")
    for sid in ("here", "elsewhere"):
        folder = source.storage_for("/work/" + sid)
        folder.mkdir(parents=True)
        (folder / f"{sid}.jsonl").write_text(
            json.dumps({"type": "session_start", "id": sid, "cwd": "/work/" + sid}) + "\n"
        )
    path = tmp_path / "registry.json"
    kwargs = {"sources": [source], "git_resolver": lambda cwd: None, "name_filters": []}
    now = "2026-11-02T00:00:00+00:00"
    refresh.auto_refresh(path, now, **kwargs)
    for sid in ("here", "elsewhere"):
        (source.storage_for("/work/" + sid) / f"{sid}.settings.json").write_text(
            '{"tags": [{"name": "exec"}]}'
        )
    checked = []
    original = source.excludes_session

    def excluding(file):
        checked.append(file)
        return original(file)

    monkeypatch.setattr(source, "excludes_session", excluding)
    if mode == "scoped":
        result, _ = refresh.scoped_refresh(path, now, cwd="/work/here", recursive=False, **kwargs)
    else:
        result = refresh.workspace_refresh(path, now, "/work/here", False, **kwargs).reg
    assert [t["sessionId"] for t in result["threads"]] == ["elsewhere"]
    assert {Path(file).stem for file in checked} == {"here"}


@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("change", ["remove", "malformed", "untag"])
def test_scoped_refresh_restores_an_excluded_run_when_settings_stop_excluding_it(
    tmp_path, change, registered
):
    from ringleader.sources.droid import DroidSource

    source = DroidSource(sessions_dir=tmp_path / "sessions")
    folder = source.storage_for("/work/repo")
    folder.mkdir(parents=True)
    transcript = folder / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "session_start", "id": "session", "cwd": "/work/repo"}) + "\n"
    )
    os.utime(transcript, (1, 1))
    path = tmp_path / "registry.json"
    now = "2026-11-02T00:00:00+00:00"
    kwargs = {"sources": [source], "git_resolver": lambda cwd: None, "name_filters": []}
    if registered:
        refresh.auto_refresh(path, now, **kwargs)
    sidecar = transcript.with_suffix(".settings.json")
    sidecar.write_text('{"tags": [{"name": "exec"}]}')
    excluded, _ = refresh.auto_refresh(path, now, **kwargs)
    assert excluded["threads"] == []
    if change == "remove":
        sidecar.unlink()
    else:
        sidecar.write_text("broken" if change == "malformed" else '{"tags": []}')
        os.utime(sidecar, (1, 1))
    restored, _ = refresh.scoped_refresh(path, now, cwd="/work/repo", recursive=False, **kwargs)
    assert [t["sessionId"] for t in restored["threads"]] == ["session"]
