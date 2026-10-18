import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from ringleader import registry
from ringleader.gitinfo import GitLookupFailedError, GitProject
from ringleader.scanner import scan, scoped_refs
from ringleader.sources.base import NormalizedSession, RawRef
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
    factory = tmp_path / "factory"
    claude.mkdir()
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(claude))
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(codex))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("PI_SESSIONS_DIR", str(pi))
    monkeypatch.setenv("FACTORY_HOME_OVERRIDE", str(factory))
    from ringleader import scanner

    names = {s.name for s in scanner.active_sources()}
    assert names == {"claude-code"}
    codex.mkdir()
    assert {s.name for s in scanner.active_sources()} == {"claude-code", "codex"}
    pi.mkdir()
    assert {s.name for s in scanner.active_sources()} == {"claude-code", "codex", "pi"}
    (factory / ".factory" / "sessions").mkdir(parents=True)
    assert {s.name for s in scanner.active_sources()} == {
        "claude-code",
        "codex",
        "droid",
        "pi",
    }


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


def test_scoped_refs_filters_a_source_without_narrow_discovery_on_mtime():
    refs = [
        RawRef(source="fake", file=f"/s/{name}.jsonl", mtime=mtime, size_bytes=1)
        for name, mtime in (
            ("old", "2026-07-01T00:00:00+00:00"),
            ("new", "2026-07-03T00:00:00+00:00"),
        )
    ]
    source = SimpleNamespace(name="fake", capabilities=frozenset(), discover=lambda: iter(refs))
    since = datetime(2026, 7, 2, tzinfo=UTC)

    found = scoped_refs(source, "/Users/me/proj", recursive=False, since=since, known=frozenset())
    assert [r.file for r in found] == ["/s/new.jsonl"]


def _labelled(cwd: str) -> GitProject:
    return GitProject(root=cwd, remote=None, key=cwd, label=Path(cwd).name)


def test_a_scan_that_spares_cached_git_still_resolves_sessions_it_parses(tmp_path):
    _session(tmp_path / "-Users-me-proj", "s1", "/Users/me/proj")
    source = ClaudeCodeSource(projects_dir=tmp_path)
    reg = registry.reconcile(
        registry.empty_registry(), scan(sources=[source], git_resolver=_labelled), "now"
    )
    _session(tmp_path / "-Users-me-other", "s2", "/Users/me/other")
    resolved: list[str] = []

    def resolver(cwd: str) -> GitProject | None:
        resolved.append(cwd)
        return GitProject(root=cwd, remote=None, key=cwd, label="fresh")

    again = scan(
        sources=[source],
        git_resolver=resolver,
        cached_threads=reg["threads"],
        resolve_cached_git=False,
    )
    assert sorted((s.cwd, s.git_project_label) for s in again) == [
        ("/Users/me/other", "fresh"),
        ("/Users/me/proj", "proj"),
    ]
    assert resolved == ["/Users/me/other"]


NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def _project(cwd: str, key: str = "github.com/me/proj") -> GitProject:
    return GitProject(root=cwd, remote=key, key=key, label=key.rsplit("/", 1)[-1])


def _cached_thread(cwd: str, checked_at: str | None, **extra):
    return {
        "source": "claude-code",
        "sessionId": "cached",
        "project": cwd,
        "gitProjectKey": "github.com/me/cached",
        "gitProjectLabel": "cached",
        "gitRoot": cwd,
        "gitRemote": "github.com/me/cached",
        "gitCloneUrl": "git@github.com:me/cached.git",
        "gitCheckedAt": checked_at,
        **extra,
    }


def _repo(cwd: Path) -> datetime:
    """Make `cwd` look like a repository root; returns a scan time 3 h after its
    `.git` last changed, so a check stamped up to 3 h earlier still post-dates it."""
    (cwd / ".git").mkdir(parents=True)
    return datetime.fromtimestamp((cwd / ".git").stat().st_ctime, UTC) + timedelta(hours=3)


def _scan_repo(tmp_path, cached, resolver, now):
    return scan(
        sources=[ClaudeCodeSource(projects_dir=tmp_path / "projects")],
        git_resolver=resolver,
        cached_threads=cached,
        now=now,
    )


def test_scan_reuses_a_recently_checked_git_identity_for_the_same_cwd(tmp_path):
    cwd = tmp_path / "repo"
    now = _repo(cwd)
    _session(tmp_path / "projects" / "-repo", "new-session", str(cwd))
    calls = []

    def resolver(c):
        calls.append(c)
        return _project(c)

    checked = (now - timedelta(hours=2)).isoformat()
    scanned = scan(
        sources=[ClaudeCodeSource(projects_dir=tmp_path / "projects")],
        git_resolver=resolver,
        cached_threads=[_cached_thread(str(cwd), checked)],
        now=now,
    )

    assert calls == []
    assert scanned[0].git_project_key == "github.com/me/cached"
    assert scanned[0].git_clone_url == "git@github.com:me/cached.git"
    assert scanned[0].git_checked_at == checked


def test_scan_re_resolves_a_git_identity_checked_more_than_a_day_ago(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _session(tmp_path / "projects" / "-repo", "s", str(cwd))

    scanned = scan(
        sources=[ClaudeCodeSource(projects_dir=tmp_path / "projects")],
        git_resolver=lambda c: _project(c, "github.com/me/moved"),
        cached_threads=[_cached_thread(str(cwd), (NOW - timedelta(hours=25)).isoformat())],
        now=NOW,
    )

    assert scanned[0].git_project_key == "github.com/me/moved"
    assert scanned[0].git_resolved is True
    assert scanned[0].git_checked_at == NOW.isoformat()


def test_scan_re_resolves_when_the_recorded_cwd_no_longer_exists(tmp_path):
    gone = str(tmp_path / "gone")
    _session(tmp_path / "projects" / "-gone", "s", gone)
    calls = []

    def resolver(c):
        calls.append(c)

    scanned = scan(
        sources=[ClaudeCodeSource(projects_dir=tmp_path / "projects")],
        git_resolver=resolver,
        cached_threads=[_cached_thread(gone, (NOW - timedelta(hours=1)).isoformat())],
        now=NOW,
    )

    assert calls == [gone]
    assert scanned[0].git_checked_at is None


def test_scan_records_a_check_for_an_existing_cwd_that_is_not_a_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    _session(tmp_path / "projects" / "-plain", "s", str(plain))

    scanned = scan(
        sources=[ClaudeCodeSource(projects_dir=tmp_path / "projects")],
        git_resolver=lambda c: None,
        now=NOW,
    )

    assert scanned[0].git_checked_at == NOW.isoformat()
    assert scanned[0].git_resolved is True


def test_scan_ignores_the_git_check_of_a_location_pinned_thread(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _session(tmp_path / "projects" / "-repo", "s", str(cwd))
    pinned = _cached_thread(str(cwd), (NOW - timedelta(hours=1)).isoformat(), locationPinned=True)

    scanned = scan(
        sources=[ClaudeCodeSource(projects_dir=tmp_path / "projects")],
        git_resolver=lambda c: _project(c, "github.com/me/live"),
        cached_threads=[pinned],
        now=NOW,
    )

    assert scanned[0].git_project_key == "github.com/me/live"


def test_scan_resolves_distinct_cwds_concurrently_and_keeps_discovery_order(tmp_path):
    cwds = [str(tmp_path / f"cwd{i}") for i in range(4)]
    for i, cwd in enumerate(cwds):
        _session(tmp_path / "projects" / f"-p{i}", f"s{i}", cwd)
    barrier = threading.Barrier(len(cwds), timeout=5)

    def resolver(c):
        barrier.wait()
        return _project(c, f"github.com/me/{Path(c).name}")

    scanned = scan(
        sources=[ClaudeCodeSource(projects_dir=tmp_path / "projects")],
        git_resolver=resolver,
        now=NOW,
    )

    assert [s.session_id for s in scanned] == ["s0", "s1", "s2", "s3"]
    assert [s.git_project_label for s in scanned] == ["cwd0", "cwd1", "cwd2", "cwd3"]


def test_scan_reuses_a_recent_check_that_found_no_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    _session(tmp_path / "projects" / "-plain", "s", str(plain))
    checked = (NOW - timedelta(hours=3)).isoformat()
    cached = {
        "source": "claude-code",
        "sessionId": "c",
        "project": str(plain),
        "gitCheckedAt": checked,
    }
    calls = []

    scanned = scan(
        sources=[ClaudeCodeSource(projects_dir=tmp_path / "projects")],
        git_resolver=calls.append,
        cached_threads=[cached],
        now=NOW,
    )

    assert calls == []
    assert scanned[0].git_checked_at == checked
    assert scanned[0].git_resolved is False


@pytest.mark.parametrize(
    "stamp",
    [
        lambda now: (now + timedelta(hours=1)).isoformat(),
        lambda now: (now - timedelta(hours=2)).replace(tzinfo=None).isoformat(),
        lambda now: "yesterday-ish",
    ],
    ids=["future", "naive", "unparseable"],
)
def test_scan_re_resolves_a_git_check_it_cannot_trust(tmp_path, stamp):
    cwd = tmp_path / "repo"
    now = _repo(cwd)
    _session(tmp_path / "projects" / "-repo", "s", str(cwd))

    scanned = _scan_repo(
        tmp_path,
        [_cached_thread(str(cwd), stamp(now))],
        lambda c: _project(c, "github.com/me/live"),
        now,
    )

    assert scanned[0].git_project_key == "github.com/me/live"
    assert scanned[0].git_checked_at == now.isoformat()


def test_scan_reuses_the_most_recent_of_several_checks_for_one_cwd(tmp_path):
    cwd = tmp_path / "repo"
    now = _repo(cwd)
    _session(tmp_path / "projects" / "-repo", "s", str(cwd))
    newer = {
        **_cached_thread(str(cwd), (now - timedelta(hours=2)).isoformat()),
        "sessionId": "newer",
        "gitProjectKey": "github.com/me/newer",
    }
    older = {
        **_cached_thread(str(cwd), (now - timedelta(hours=20)).isoformat()),
        "sessionId": "older",
        "gitProjectKey": "github.com/me/older",
    }

    for cached in ([newer, older], [older, newer]):
        scanned = _scan_repo(tmp_path, cached, lambda c: None, now)
        assert scanned[0].git_project_key == "github.com/me/newer"


def test_a_failed_git_lookup_leaves_the_recorded_identity_alone(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _session(tmp_path / "projects" / "-repo", "s", str(cwd))

    def flaky(c):
        raise GitLookupFailedError(c)

    scanned = _scan_repo(
        tmp_path, [_cached_thread(str(cwd), (NOW - timedelta(hours=30)).isoformat())], flaky, NOW
    )

    assert (scanned[0].git_resolved, scanned[0].git_checked_at) == (False, None)


def test_a_repository_recreated_since_the_check_is_resolved_again(tmp_path):
    cwd = tmp_path / "repo"
    now = _repo(cwd)
    _session(tmp_path / "projects" / "-repo", "s", str(cwd))
    before_reclone = (now - timedelta(hours=4)).isoformat()

    scanned = _scan_repo(
        tmp_path,
        [_cached_thread(str(cwd), before_reclone)],
        lambda c: _project(c, "github.com/me/recloned"),
        now,
    )

    assert scanned[0].git_project_key == "github.com/me/recloned"
    assert scanned[0].git_resolved is True


def test_a_repository_whose_git_dir_is_gone_is_resolved_again(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _session(tmp_path / "projects" / "-repo", "s", str(cwd))

    scanned = _scan_repo(
        tmp_path,
        [_cached_thread(str(cwd), (NOW - timedelta(hours=2)).isoformat())],
        lambda c: None,
        NOW,
    )

    assert (scanned[0].git_resolved, scanned[0].git_checked_at) == (True, NOW.isoformat())


def test_a_recorded_no_repo_check_is_dropped_once_a_repository_appears(tmp_path):
    cwd = tmp_path / "plain"
    cwd.mkdir()
    _session(tmp_path / "projects" / "-plain", "s", str(cwd))
    no_repo = {
        "source": "claude-code",
        "sessionId": "c",
        "project": str(cwd),
        "gitCheckedAt": (NOW - timedelta(hours=2)).isoformat(),
    }
    (tmp_path / ".git").mkdir()

    scanned = _scan_repo(tmp_path, [no_repo], lambda c: _project(c, "github.com/me/new"), NOW)

    assert scanned[0].git_project_key == "github.com/me/new"
