import json
import os
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path

import pytest
from ringleader.sources.base import (
    DiscoveryCacheSource,
    NameApplyingSource,
    PickerHidingSource,
    RawRef,
    ScopedDiscoverySource,
    SessionLaunchingSource,
    Source,
    WorkspaceRebindingSource,
)
from ringleader.sources.codex import CodexSessionIndexError, CodexSource


def _rollout(
    dir_: Path,
    sid: str,
    cwd: str,
    users: list[str],
    *,
    preamble: bool = True,
    assistants: list[str] | None = None,
) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "type": "session_meta",
            "payload": {
                "session_id": sid,
                "cwd": cwd,
                "timestamp": "2026-07-28T17:19:45.210Z",
                "model_provider": "openai",
            },
        },
    ]
    preamble_msgs = (
        [f"# AGENTS.md instructions for {cwd}\n<INSTRUCTIONS>...</INSTRUCTIONS>"]
        if preamble
        else []
    )
    for text in preamble_msgs:
        lines.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
    assistant_msgs = list(assistants or [])
    for user_text, assistant_text in zip_longest(users, assistant_msgs, fillvalue=None):
        if user_text is not None:
            lines.append(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": user_text}],
                    },
                }
            )
        if assistant_text is not None:
            lines.append(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": assistant_text}],
                    },
                }
            )
    p = dir_ / f"rollout-2026-07-28T19-19-45-{sid}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


def test_source_satisfies_protocol(tmp_path):
    src = CodexSource(sessions_dir=tmp_path)
    assert isinstance(src, Source)
    assert isinstance(src, NameApplyingSource)
    assert isinstance(src, PickerHidingSource)
    assert isinstance(src, WorkspaceRebindingSource)
    assert isinstance(src, SessionLaunchingSource)
    assert isinstance(src, ScopedDiscoverySource)
    assert isinstance(src, DiscoveryCacheSource)
    assert src.name == "codex"
    assert src.capabilities == frozenset(
        {
            "applies_names",
            "discovers_scoped",
            "seeds_discovery",
            "filesystem_lifecycle",
            "hides_from_picker",
            "shows_in_picker",
            "rebinds_workspace",
            "launches_sessions",
            "forks_sessions",
        }
    )


LAST_SCAN = datetime(2026, 7, 1, tzinfo=UTC)
BEFORE_LAST_SCAN = LAST_SCAN.timestamp() - 1


def _scoped(src: CodexSource, cwd: str | None, known: frozenset[str] = frozenset()) -> list:
    refs = src.discover_scoped(cwd, recursive=False, since=LAST_SCAN, known=known)
    return [r.source_ref for r in refs]


def test_scoped_discovery_skips_rollouts_unchanged_since_the_last_scan(tmp_path):
    day = tmp_path / "2026" / "07" / "28"
    stale = _rollout(day, "old", "/Users/me/proj", ["before"])
    _rollout(day, "new", "/Users/me/proj", ["after"])
    os.utime(stale, (BEFORE_LAST_SCAN, BEFORE_LAST_SCAN))

    assert _scoped(CodexSource(sessions_dir=tmp_path), None) == ["new"]


def test_scoped_discovery_skips_rollouts_recorded_in_another_workspace(tmp_path):
    day = tmp_path / "2026" / "07" / "28"
    _rollout(day, "here", "/Users/me/proj", ["mine"])
    _rollout(day, "there", "/Users/me/other", ["theirs"])

    assert _scoped(CodexSource(sessions_dir=tmp_path), "/Users/me/proj") == ["here"]


def test_scoped_discovery_keeps_a_known_rollout_whose_recorded_cwd_moved(tmp_path):
    """`rl workspace move` rewrites Codex's state db, never the cwd baked into the rollout."""
    moved = _rollout(tmp_path / "2026" / "07" / "28", "moved", "/Users/me/old", ["hi"])
    source = CodexSource(sessions_dir=tmp_path)

    assert _scoped(source, "/Users/me/proj", known=frozenset({str(moved)})) == ["moved"]


def test_discover_finds_rollouts(tmp_path):
    _rollout(
        tmp_path / "2026" / "07" / "28",
        "11111111-0000-0000-0000-000000000001",
        "/Users/me/proj",
        ["hi"],
    )
    src = CodexSource(sessions_dir=tmp_path)
    refs = list(src.discover())
    assert len(refs) == 1
    r = refs[0]
    assert isinstance(r, RawRef) and r.source == "codex" and r.file.endswith(".jsonl")
    assert r.source_ref == "11111111-0000-0000-0000-000000000001"


def test_parse_extracts_fields_and_skips_preamble(tmp_path):
    d = tmp_path / "2026" / "07" / "28"
    _rollout(
        d,
        "22222222-0000-0000-0000-000000000002",
        "/Users/me/proj",
        ["first real prompt", "the last prompt"],
    )
    src = CodexSource(sessions_dir=tmp_path)
    ns = src.parse(next(iter(src.discover())))
    assert ns.started_at == "2026-07-28T17:19:45.210000+00:00"
    assert ns.source == "codex"
    assert ns.session_id == "22222222-0000-0000-0000-000000000002"
    assert ns.cwd == "/Users/me/proj"
    assert ns.last_prompt == "the last prompt"
    assert ns.message_count == 2  # preamble excluded
    assert ns.custom_title is None and ns.summary is None and ns.ai_title is None


def test_discover_missing_dir_yields_nothing(tmp_path):
    assert list(CodexSource(sessions_dir=tmp_path / "nope").discover()) == []


def test_parse_preamble_only_session(tmp_path):
    d = tmp_path / "2026" / "07" / "28"
    _rollout(d, "33333333-0000-0000-0000-000000000003", "/Users/me/proj", [])
    src = CodexSource(sessions_dir=tmp_path)
    ns = src.parse(next(iter(src.discover())))
    assert ns.message_count == 0
    assert ns.last_prompt is None


def test_parse_keeps_later_message_resembling_preamble(tmp_path):
    d = tmp_path / "2026" / "07" / "28"
    _rollout(
        d,
        "44444444-0000-0000-0000-000000000004",
        "/Users/me/proj",
        ["first real prompt", "# AGENTS.md style content the user pasted"],
    )
    src = CodexSource(sessions_dir=tmp_path)
    ns = src.parse(next(iter(src.discover())))
    assert ns.message_count == 2
    assert ns.last_prompt == "# AGENTS.md style content the user pasted"


def test_parse_message_count_includes_assistant_turns(tmp_path):
    d = tmp_path / "2026" / "07" / "28"
    _rollout(
        d,
        "66666666-0000-0000-0000-000000000006",
        "/Users/me/proj",
        ["first real prompt", "second prompt", "the last prompt"],
        assistants=["ack one", "ack two", "ack three"],
    )
    src = CodexSource(sessions_dir=tmp_path)
    ns = src.parse(next(iter(src.discover())))
    assert ns.message_count == 6  # 3 user + 3 assistant, preamble excluded
    assert ns.last_prompt == "the last prompt"  # last USER message, not assistant


def test_parse_leaves_git_fields_at_defaults(tmp_path):
    d = tmp_path / "2026" / "07" / "28"
    _rollout(
        d,
        "55555555-0000-0000-0000-000000000005",
        "/Users/me/proj",
        ["first real prompt", "the last prompt"],
    )
    src = CodexSource(sessions_dir=tmp_path)
    ns = src.parse(next(iter(src.discover())))
    assert ns.git_branch is None
    assert ns.git_project_key is None


def test_parse_reads_latest_name_from_session_index(tmp_path):
    sessions = tmp_path / "sessions"
    sid = "77777777-0000-0000-0000-000000000007"
    _rollout(sessions / "2026" / "07" / "28", sid, "/Users/me/proj", ["hello"])
    index = tmp_path / "session_index.jsonl"
    index.write_text(
        "\n".join(
            [
                json.dumps({"id": sid, "thread_name": "old-name", "updated_at": "earlier"}),
                "not json",
                json.dumps({"id": sid, "thread_name": "latest-name", "updated_at": "later"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    src = CodexSource(sessions_dir=sessions)
    ns = src.parse(next(iter(src.discover())))

    assert ns.custom_title == "latest-name"


def test_apply_name_appends_native_session_index_entry(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    sid = "88888888-0000-0000-0000-000000000008"
    rollout = _rollout(sessions, sid, "/Users/me/proj", ["hello"])
    index = tmp_path / "session_index.jsonl"
    index.write_text(
        json.dumps({"id": "other", "thread_name": "other-name", "updated_at": "earlier"}),
        encoding="utf-8",
    )
    src = CodexSource(sessions_dir=sessions)

    src.apply_name(sid, str(rollout), "  fresh-name  ")

    entries = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines()]
    assert entries[-1]["id"] == sid
    assert entries[-1]["thread_name"] == "fresh-name"
    assert entries[-1]["updated_at"].endswith("Z")


def test_apply_name_rejects_missing_rollout(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    src = CodexSource(sessions_dir=sessions)

    with pytest.raises(FileNotFoundError):
        src.apply_name("missing", str(sessions / "missing.jsonl"), "ghost-name")

    assert not (tmp_path / "session_index.jsonl").exists()


def test_unreadable_session_index_aborts_scan(tmp_path):
    sessions = tmp_path / "sessions"
    sid = "99999999-0000-0000-0000-000000000009"
    _rollout(sessions, sid, "/Users/me/proj", ["hello"])
    index = tmp_path / "session_index.jsonl"
    index.mkdir()
    src = CodexSource(sessions_dir=sessions)

    with pytest.raises(CodexSessionIndexError):
        src.parse(next(iter(src.discover())))


def test_parse_keeps_a_prompt_containing_a_raw_unicode_line_separator(tmp_path):
    path = _rollout(tmp_path, "sid-sep", "/repo", ["first", "split\N{LINE SEPARATOR}here", "last"])
    raw = path.read_text(encoding="utf-8").replace("\\u2028", "\N{LINE SEPARATOR}")
    path.write_text(raw, encoding="utf-8")
    src = CodexSource(sessions_dir=tmp_path)

    parsed = src.parse(next(src.discover()))

    assert parsed.message_count == 3
    assert parsed.last_prompt == "last"


def test_discover_reads_an_unchanged_rollout_header_once_per_source(tmp_path, monkeypatch):
    from ringleader.sources import codex

    path = _rollout(tmp_path, "sid-memo", "/repo", ["hi"])
    src = CodexSource(sessions_dir=tmp_path)
    reads = []
    real_meta = codex._meta

    def counting_meta(p):
        reads.append(p)
        return real_meta(p)

    monkeypatch.setattr(codex, "_meta", counting_meta)
    first = [ref.source_ref for ref in src.discover()]
    second = [ref.source_ref for ref in src.discover()]
    _rollout(tmp_path, "sid-changed", "/repo", ["hi", "again"])
    path.unlink()
    (tmp_path / "rollout-2026-07-28T19-19-45-sid-changed.jsonl").rename(path)
    third = [ref.source_ref for ref in src.discover()]

    assert first == second == ["sid-memo"]
    assert third == ["sid-changed"]
    assert reads == [path, path]


def test_discover_forgets_a_rollout_header_whose_file_is_gone(tmp_path):
    kept = _rollout(tmp_path, "sid-kept", "/repo", ["hi"])
    gone = _rollout(tmp_path / "sub", "sid-gone", "/repo", ["hi"])
    src = CodexSource(sessions_dir=tmp_path)
    list(src.discover())
    gone.unlink()

    list(src.discover())

    assert list(src._headers) == [str(kept)]


def test_a_new_codex_source_reads_headers_afresh(tmp_path, monkeypatch):
    from ringleader.sources import codex

    _rollout(tmp_path, "sid", "/repo", ["hi"])
    reads = []
    real_header = codex._header
    monkeypatch.setattr(codex, "_header", lambda p: reads.append(p) or real_header(p))

    for _ in range(2):
        list(CodexSource(sessions_dir=tmp_path).discover())

    assert len(reads) == 2
