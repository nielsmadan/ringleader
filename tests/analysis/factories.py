from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ringleader.analysis import taxonomy
from ringleader.analysis.facts import (
    MAIN_THREAD,
    CompactionFact,
    MessageOrigin,
    RequestFact,
    RewindFact,
    ToolCallFact,
    TranscriptFacts,
    TranscriptRef,
    UserMessageFact,
)

SID = "11111111-2222-3333-4444-555555555555"
START = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


def stamp(seconds: int) -> str:
    return (START + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def bash(command: str, status: str = "ok", **extra: Any) -> dict[str, Any]:
    return {"tool": "Bash", "command": command, "status": status, **extra}


def read(path: str, **extra: Any) -> dict[str, Any]:
    return {"tool": "Read", "file_path": path, **extra}


def grep(pattern: str, **extra: Any) -> dict[str, Any]:
    return {"tool": "Grep", "input_hash": f"grep:{pattern}", **extra}


def edit(path: str, status: str = "ok", **extra: Any) -> dict[str, Any]:
    return {"tool": "Edit", "file_path": path, "status": status, **extra}


def fail(command: str = "make", category: str = "exit_code", **extra: Any) -> dict[str, Any]:
    return bash(
        command, "error", **{"error_category": category, "error_text": "Exit code 1", **extra}
    )


def call(seq: int, spec: dict[str, Any]) -> ToolCallFact:
    tool = spec.get("tool", "Bash")
    command = spec.get("command")
    return ToolCallFact(
        seq=seq,
        line=spec.get("line", seq * 2 + 2),
        call_id=spec.get("call_id", f"t{seq}"),
        dedupe_key=spec.get("call_id", f"t{seq}"),
        timestamp=spec.get("timestamp", stamp(seq * 10)),
        tool=tool,
        kind=spec.get("kind", taxonomy.tool_kind(tool, command)),
        mcp_server=None,
        mcp_tool=None,
        skill=None,
        command=command,
        command_class=spec.get("command_class", taxonomy.command_class(tool, command)),
        file_path=spec.get("file_path"),
        input_hash=spec.get("input_hash", f"h{seq}"),
        input_preview=spec.get("input_preview", command or spec.get("file_path") or tool),
        edit_old_hash=spec.get("old"),
        edit_new_hash=spec.get("new"),
        is_error=spec.get("status") == "error",
        status=spec.get("status", "ok"),
        error_category=spec.get("error_category"),
        error_text=spec.get("error_text"),
        signature=spec.get("signature"),
        on_main_branch=spec.get("on_main_branch", True),
        read_offset=spec.get("read_offset"),
        read_limit=spec.get("read_limit"),
    )


def calls(*specs: dict[str, Any]) -> tuple[ToolCallFact, ...]:
    return tuple(call(seq, spec) for seq, spec in enumerate(specs))


def request(line: int, context: int, *, output: int = 10, **extra: Any) -> RequestFact:
    return RequestFact(
        line=line,
        dedupe_key=f"m{line}|r{line}",
        timestamp=stamp(line * 5),
        model="claude-opus-5",
        input=context,
        output=output,
        cache_read=0,
        cache_write=0,
        cache_write_5m=0,
        cache_write_1h=0,
        reasoning=0,
        context_tokens=context,
        context_window=None,
        gap_seconds=None,
        rebuild=False,
        on_main_branch=extra.get("on_main_branch", True),
        skill=None,
        plugin=None,
    )


def message(
    before_call: int,
    text: str = "please continue",
    *,
    origin: MessageOrigin = "human",
    is_interrupt: bool = False,
) -> UserMessageFact:
    return UserMessageFact(
        before_call=before_call,
        line=before_call * 2 + 1,
        message_id=f"u{before_call}",
        dedupe_key=f"u{before_call}",
        timestamp=stamp(before_call * 10 - 5),
        origin=origin,
        text=text,
        is_interrupt=is_interrupt,
        slash_command=None,
        rule_kind=None,
        content_hash="c",
        on_main_branch=True,
    )


def compaction(before_call: int) -> CompactionFact:
    return CompactionFact(
        before_call=before_call,
        line=before_call * 2 + 1,
        timestamp=stamp(before_call * 10 - 5),
        trigger="auto",
        pre_tokens=150_000,
        post_tokens=20_000,
    )


def rewind(before_call: int, abandoned: int) -> RewindFact:
    return RewindFact(
        line=before_call * 2 + 1,
        timestamp=stamp(before_call * 10 - 5),
        branch_uuid="b",
        before_call=before_call,
        abandoned_calls=abandoned,
    )


def facts(tool_calls: tuple[ToolCallFact, ...] = (), **fields: Any) -> TranscriptFacts:
    base = TranscriptFacts(
        ref=TranscriptRef(
            path=Path(f"/p/{SID}.jsonl"),
            agent="claude-code",
            root="claude",
            archived=False,
            fingerprint="f",
        ),
        session_id=SID,
        thread_id=MAIN_THREAD,
        thread_key=SID,
        parent_thread_id=None,
        parent_tool_use_id=None,
        subagent_type=None,
        subagent_description=None,
        cwd="/w",
        git_branch="main",
        first_ts=stamp(0),
        last_ts=stamp(len(tool_calls) * 10),
        final_text=None,
        last_event="turn_complete",
        api_errors=0,
        requests=(),
        tool_calls=tool_calls,
        user_messages=(),
        compactions=(),
        rewinds=(),
    )
    return replace(base, **fields)


def claude_records(
    session_id: str, commands: tuple[str, ...] = ("ls",), *, failing: bool = False
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(record: dict[str, Any]) -> None:
        n = len(out)
        out.append(
            {
                "uuid": f"{session_id}-u{n}",
                "parentUuid": out[-1]["uuid"] if out else None,
                "sessionId": session_id,
                "timestamp": stamp(n),
                "cwd": "/w",
                **record,
            }
        )

    add({"type": "user", "message": {"role": "user", "content": "go"}})
    for i, command in enumerate(commands):
        call_id = f"{session_id}-t{i}"
        add(
            {
                "type": "assistant",
                "requestId": f"{session_id}-r{i}",
                "message": {
                    "id": f"{session_id}-m{i}",
                    "model": "claude-opus-5",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": "Bash",
                            "input": {"command": command},
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": call_id,
            "content": "Exit code 1\nmake: *** failed" if failing else "ok",
        }
        if failing:
            block["is_error"] = True
        add({"type": "user", "message": {"role": "user", "content": [block]}})
    return out


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def write_claude(
    root: Path,
    session_id: str,
    commands: tuple[str, ...] = ("ls",),
    *,
    project: str = "-w",
    failing: bool = False,
) -> Path:
    return write_jsonl(
        root / project / f"{session_id}.jsonl",
        claude_records(session_id, commands, failing=failing),
    )
