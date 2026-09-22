import json
from pathlib import Path

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
