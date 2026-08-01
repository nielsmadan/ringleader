import json
from pathlib import Path
from ringleader.transcript import parse_transcript, TranscriptInfo


def _write(path: Path, records):
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_parse_extracts_metadata_and_tail(tmp_path):
    sid = "11111111-1111-1111-1111-111111111111"
    f = tmp_path / f"{sid}.jsonl"
    _write(f, [
        {"type": "user", "cwd": "/Users/me/proj", "gitBranch": "main",
         "message": {"role": "user", "content": "first prompt"}},
        {"type": "assistant", "cwd": "/Users/me/proj",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "doing it"},
                                 {"type": "tool_use", "name": "Bash"}]}},
        {"type": "user",
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "content": "noise"}]}},
        {"type": "user", "message": {"role": "user", "content": "last prompt"}},
        {"type": "custom-title", "customTitle": "my-name", "sessionId": sid},
        {"type": "ai-title", "aiTitle": "auto-name", "sessionId": sid},
        {"type": "last-prompt", "lastPrompt": "last prompt", "sessionId": sid},
        {"type": "summary", "summary": "a summary"},
    ])

    info = parse_transcript(f)

    assert isinstance(info, TranscriptInfo)
    assert info.session_id == sid
    assert info.cwd == "/Users/me/proj"
    assert info.git_branch == "main"
    assert info.custom_title == "my-name"
    assert info.ai_title == "auto-name"
    assert info.summary == "a summary"
    assert info.last_prompt == "last prompt"
    # 4 user/assistant records counted (incl. tool_result-only user msg)
    assert info.message_count == 4
    # tail has the textual turns only, tool_result/tool_use stripped
    assert "USER: first prompt" in info.tail_text
    assert "ASSISTANT: doing it" in info.tail_text
    assert "USER: last prompt" in info.tail_text
    assert "noise" not in info.tail_text


def test_tail_is_limited_to_last_n_turns(tmp_path):
    f = tmp_path / "22222222-2222-2222-2222-222222222222.jsonl"
    recs = [{"type": "user", "message": {"role": "user", "content": f"msg{i}"}}
            for i in range(10)]
    _write(f, recs)
    info = parse_transcript(f, tail_turns=3)
    assert "msg9" in info.tail_text
    assert "msg6" not in info.tail_text


def test_skips_malformed_lines_and_missing_fields(tmp_path):
    f = tmp_path / "33333333-3333-3333-3333-333333333333.jsonl"
    f.write_text(
        'not json\n'
        '{"type":"last-prompt","leafUuid":"x"}\n'      # no lastPrompt key
        '{"type":"user","message":{"role":"user","content":"hi"}}\n',
        encoding="utf-8")
    info = parse_transcript(f)
    assert info.message_count == 1
    # falls back to last user turn when no lastPrompt value present
    assert info.last_prompt == "hi"
    assert info.cwd is None
