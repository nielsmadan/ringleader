import json
from pathlib import Path

import pytest
from ringleader import export
from ringleader.export import render_conversation


def _write(p: Path, records):
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_render_includes_turns_and_tools_skips_metadata(tmp_path):
    f = tmp_path / "s.jsonl"
    _write(
        f,
        [
            {"type": "custom-title", "customTitle": "x", "sessionId": "s"},  # metadata → skipped
            {"type": "user", "message": {"role": "user", "content": "fix the login bug"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Looking into it."},
                        {"type": "tool_use", "name": "Bash", "input": {}},
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "noise"}],
                },
            },  # no text → no turn
        ],
    )
    out = render_conversation(f)
    assert "## USER" in out and "fix the login bug" in out
    assert "## ASSISTANT" in out and "Looking into it." in out
    assert "Bash" in out  # tool noted
    assert "custom-title" not in out  # metadata not rendered
    assert "noise" not in out  # tool_result not rendered as a turn


def test_render_tolerates_garbage_and_empty(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(
        'not json\n\n{"type":"user","message":{"role":"user","content":"hi"}}\n', encoding="utf-8"
    )
    out = render_conversation(f)
    assert "hi" in out


def test_render_conversation_handles_codex(tmp_path):
    f = tmp_path / "rollout.jsonl"
    f.write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {"type": "session_meta", "payload": {"session_id": "s", "cwd": "/p"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "do the thing"}],
                    },
                },
                {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done the thing"}],
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out = export.render_conversation(f)
    assert "## USER" in out and "do the thing" in out
    assert "## ASSISTANT" in out and "done the thing" in out


def test_render_conversation_codex_null_content_does_not_crash(tmp_path):
    f = tmp_path / "rollout.jsonl"
    f.write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {
                    "type": "response_item",
                    "payload": {"type": "message", "role": "user", "content": None},
                },
                {
                    "type": "response_item",
                    "payload": {"type": "message", "role": "assistant"},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "still works"}],
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out = export.render_conversation(f)
    assert "## USER" in out and "still works" in out


def test_render_conversation_handles_pi(tmp_path):
    f = tmp_path / "2026-09-21T10-00-00_0199aa.jsonl"
    _write(
        f,
        [
            {"type": "session", "id": "0199aa", "cwd": "/w/api", "version": 3},
            {"type": "session_info", "name": "auth-refresh"},  # metadata → skipped
            {
                "type": "message",
                "message": {"role": "user", "content": [{"type": "text", "text": "why the 401"}]},
            },
            {
                "type": "message",
                "message": {"role": "assistant", "content": "the token expired"},
            },
            {"type": "message", "message": {"role": "system", "content": "boot"}},  # not a turn
        ],
    )
    out = export.render_conversation(f)
    assert "## USER" in out and "why the 401" in out
    assert "## ASSISTANT" in out and "the token expired" in out
    assert "boot" not in out


def _transcript(n):
    records = []
    for i in range(n):
        records.append({"type": "user", "message": {"role": "user", "content": f"ask {i} " * 7}})
        records.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": f"answer {i} — ünïcode " * 5},
                        {"type": "tool_use", "name": "Bash", "input": {}},
                    ],
                },
            }
        )
        records.append({"type": "progress", "data": "z" * (i % 50)})
    return records


@pytest.mark.parametrize("max_chars", [0, 1, 37, 500, 4000, 10**7])
def test_render_tail_is_the_end_of_the_full_rendering(tmp_path, monkeypatch, max_chars):
    f = tmp_path / "s.jsonl"
    _write(f, [*_transcript(40), "a json string, not a record"])
    f.write_text(f.read_text(encoding="utf-8") + "\nnot json\n", encoding="utf-8")
    thread = {"source": "claude-code", "sessionId": "s", "file": str(f)}
    monkeypatch.setattr(export, "TAIL_CHUNK_BYTES", 64)

    full = render_conversation(f)
    assert export.render_tail(thread, max_chars) == (full[-max_chars:] if max_chars else "")


def test_render_tail_reads_only_the_end_of_a_large_transcript(tmp_path, monkeypatch):
    f = tmp_path / "big.jsonl"
    filler = json.dumps({"type": "progress", "data": "x" * 2000}) + "\n"
    with f.open("w", encoding="utf-8") as fh:
        fh.write(filler * 25_000)
        fh.write("".join(json.dumps(r) + "\n" for r in _transcript(60)))
    assert f.stat().st_size > 50_000_000
    calls = []
    real_parse = export._parse_line

    def counting(raw):
        calls.append(1)
        return real_parse(raw)

    monkeypatch.setattr(export, "_parse_line", counting)
    tail = export.render_tail({"source": "claude-code", "sessionId": "big", "file": str(f)}, 6000)
    parsed_for_tail = len(calls)
    monkeypatch.setattr(export, "_parse_line", real_parse)

    assert tail == render_conversation(f)[-6000:]
    assert parsed_for_tail < 2000


def test_render_tail_raises_like_render_conversation(tmp_path):
    with pytest.raises(RuntimeError, match="no readable conversation"):
        export.render_tail({"source": "claude-code", "sessionId": "s", "file": None}, 100)
    with pytest.raises(OSError):
        export.render_tail(
            {"source": "claude-code", "sessionId": "s", "file": str(tmp_path / "gone.jsonl")}, 100
        )
