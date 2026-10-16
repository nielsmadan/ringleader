import json
import os

from ringleader import refresh
from ringleader.gitinfo import GitProject
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
    assert thread.get("cacheVersion") == 1
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
    assert actual["cacheVersion"] == 1
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
