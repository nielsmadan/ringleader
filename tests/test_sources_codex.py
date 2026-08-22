import json
from itertools import zip_longest
from pathlib import Path

import pytest
from ringleader.sources.base import NameApplyingSource, PickerHidingSource, RawRef, Source
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
    assert src.name == "codex"
    assert src.capabilities == frozenset({"applies_names", "hides_from_picker", "shows_in_picker"})


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
