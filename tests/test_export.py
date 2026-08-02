import json
from pathlib import Path

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
