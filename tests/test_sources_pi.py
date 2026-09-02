import json
from pathlib import Path

import pytest
from ringleader.sources.base import NameApplyingSource, RawRef, Source
from ringleader.sources.pi import PiSource

SESSION_ID = "019f9b5d-a83f-7f7f-aae9-b65fc6f1a65a"
FILENAME_ID = "019f9b5d-a83f-7f7f-aae9-b65fc6f1a65b"
CWD = "/Users/me/wrksp/proj"


def _session(
    sessions_dir: Path,
    records: list[dict | str],
    *,
    file_session_id: str = FILENAME_ID,
    trailing_newline: bool = True,
) -> Path:
    project = sessions_dir / "--Users-me-wrksp-proj--"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"2026-08-31T10-20-30-000Z_{file_session_id}.jsonl"
    content = "\n".join(
        json.dumps(record) if isinstance(record, dict) else record for record in records
    )
    path.write_text(content + ("\n" if trailing_newline else ""), encoding="utf-8")
    return path


def test_source_satisfies_protocol(tmp_path):
    source = PiSource(sessions_dir=tmp_path)

    assert isinstance(source, Source)
    assert isinstance(source, NameApplyingSource)
    assert source.name == "pi"
    assert source.capabilities == frozenset({"applies_names", "filesystem_lifecycle"})


def test_discover_finds_sessions_and_excludes_nested_subagents(tmp_path):
    transcript = _session(tmp_path, [{"type": "session", "id": SESSION_ID, "cwd": CWD}])
    nested = transcript.with_suffix("") / "a1b2c3d4" / "run-0"
    nested.mkdir(parents=True)
    (nested / "session.jsonl").write_text("{}\n", encoding="utf-8")

    refs = list(PiSource(sessions_dir=tmp_path).discover())

    assert [ref.file for ref in refs] == [str(transcript)]
    assert isinstance(refs[0], RawRef)
    assert refs[0].source == "pi"
    assert refs[0].source_ref == FILENAME_ID
    assert refs[0].fingerprint


def test_discover_missing_dir_yields_nothing(tmp_path):
    assert list(PiSource(sessions_dir=tmp_path / "missing").discover()) == []


def test_parse_extracts_header_messages_latest_name_and_tail(tmp_path):
    _session(
        tmp_path,
        [
            {"type": "session", "id": SESSION_ID, "cwd": CWD},
            {"type": "session_info", "id": "name-1", "name": "old name"},
            {
                "type": "message",
                "id": "user-1",
                "message": {"role": "user", "content": "first prompt"},
            },
            "{not-json",
            {
                "type": "message",
                "id": "assistant-1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "internal"},
                        {"type": "text", "text": "the answer"},
                    ],
                },
            },
            {"type": "session_info", "id": "name-2", "name": "  latest\nname  "},
            {
                "type": "message",
                "id": "user-2",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "last"},
                        {"type": "toolResult", "content": "ignored"},
                        {"type": "text", "text": "prompt"},
                    ],
                },
            },
        ],
    )
    source = PiSource(sessions_dir=tmp_path, tail_turns=2)

    parsed = source.parse(next(iter(source.discover())))

    assert parsed.source == "pi"
    assert parsed.session_id == SESSION_ID
    assert parsed.cwd == CWD
    assert parsed.custom_title == "latest name"
    assert parsed.last_prompt == "last prompt"
    assert parsed.message_count == 3
    assert parsed.tail_text == "ASSISTANT: the answer\nUSER: last prompt"


def test_parse_uses_filename_id_without_a_session_header(tmp_path):
    _session(
        tmp_path,
        [
            {
                "type": "message",
                "id": "user-1",
                "message": {"role": "user", "content": "hello"},
            }
        ],
    )
    source = PiSource(sessions_dir=tmp_path)

    parsed = source.parse(next(iter(source.discover())))

    assert parsed.session_id == FILENAME_ID
    assert parsed.last_prompt == "hello"


def test_apply_name_appends_native_session_info(tmp_path):
    records = [
        {"type": "session", "id": SESSION_ID, "cwd": CWD},
        {
            "type": "message",
            "id": "turn-1",
            "parentId": SESSION_ID,
            "message": {"role": "user", "content": "hello"},
        },
    ]
    transcript = _session(tmp_path, records, trailing_newline=False)
    source = PiSource(sessions_dir=tmp_path)

    source.apply_name(SESSION_ID, str(transcript), "  fresh\nname  ")

    entries = [json.loads(line) for line in transcript.read_text().splitlines()]
    appended = entries[-1]
    assert entries[:-1] == records
    assert appended["type"] == "session_info"
    assert appended["parentId"] == "turn-1"
    assert appended["name"] == "fresh name"
    assert len(appended["id"]) == 8
    assert appended["timestamp"].endswith("Z")
    assert source.parse(next(iter(source.discover()))).custom_title == "fresh name"


def test_apply_name_rejects_a_different_session_id_without_writing(tmp_path):
    transcript = _session(tmp_path, [{"type": "session", "id": SESSION_ID, "cwd": CWD}])
    before = transcript.read_bytes()
    source = PiSource(sessions_dir=tmp_path)

    with pytest.raises(ValueError, match="session id mismatch"):
        source.apply_name(FILENAME_ID, str(transcript), "wrong session")

    assert transcript.read_bytes() == before


def test_apply_name_rejects_a_missing_transcript(tmp_path):
    missing = tmp_path / f"2026-08-31T10-20-30-000Z_{SESSION_ID}.jsonl"

    with pytest.raises(FileNotFoundError):
        PiSource(sessions_dir=tmp_path).apply_name(SESSION_ID, str(missing), "ghost")
