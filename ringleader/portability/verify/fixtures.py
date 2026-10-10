from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from .. import native
from ..model import (
    Attachment,
    AttachmentRef,
    Checkpoint,
    Content,
    Conversation,
    HistoricalTimestamp,
    Identity,
    Instruction,
    Message,
    Provenance,
    Text,
    ToolCall,
    ToolResult,
)
from .common import PNG

SOURCE = Identity("pi", "source-session")
TIMESTAMP = "2026-09-21T12:00:00.000Z"
SESSION_ID = "019f9b5d-a83f-7f7f-aae9-b65fc6f1a65a"


Role = Literal["user", "assistant", "system", "developer", "tool"]


def provenance(
    locator: str = "user-entry", origin: Literal["authored", "native", "harness"] = "authored"
) -> Provenance:
    return Provenance(origin, SOURCE, locator)


def message(key: str, role: Role, *content: Content, parent: str | None = None) -> Message:
    return Message(key, role, content, provenance(key), parent)


def conversation() -> Conversation:
    history = (
        message(
            "instructions",
            "user",
            Instruction("Preserve my formatting.", "user", provenance("instructions")),
        ),
        message(
            "request",
            "user",
            Text("# AGENTS.md instructions: this is my literal input"),
            AttachmentRef("image"),
            parent="instructions",
        ),
        message(
            "calls",
            "assistant",
            Text("Checking both."),
            ToolCall("a", "read", '{"file":"a"}', "json"),
            ToolCall("b", "shell", "cat b", "freeform"),
            parent="request",
        ),
        message(
            "results",
            "tool",
            ToolResult("a", (Text("contents"),)),
            ToolResult("b", (Text("failed"), AttachmentRef("image")), True),
            parent="calls",
        ),
        message("answer", "assistant", Text("One succeeded."), parent="results"),
    )
    return Conversation(
        SOURCE,
        "answer",
        history,
        history,
        attachments=(Attachment("image", "image/png", b"fixture-image", "image.png"),),
        title="Portable example",
    )


def checkpoint_conversation() -> Conversation:
    base = conversation()
    summary = message("summary", "user", Text("Earlier work was summarized."))
    checkpoint = Checkpoint("compact-1", "results", (summary,), ("request", "calls", "results"))
    context = (summary, replace(base.history[1], parent_id="summary"), *base.history[2:])
    return replace(base, context=context, checkpoints=(checkpoint,))


def split_checkpoint() -> Conversation:
    base = conversation()
    summary = replace(base.history[0], id="summary", content=(Text("Summary"),), parent_id=None)
    model_suffix = native.linked(
        [
            replace(
                m, id="model-" + m.id, provenance=replace(m.provenance, locator="model-" + m.id)
            )
            for m in base.history[1:]
        ]
    )
    checkpoint = Checkpoint("cp", base.history[0].id, (summary,), following_context=model_suffix)
    return replace(base, context=native.linked((summary, *model_suffix)), checkpoints=(checkpoint,))


def opencode_conversation() -> Conversation:
    base = conversation()
    history = []
    for item in base.history:
        contents: list[Content] = []
        for original in item.content:
            part = original
            if isinstance(part, ToolCall) and part.argument_format == "freeform":
                part = replace(part, argument_format="json", arguments='{"command":"cat b"}')
            if isinstance(part, ToolResult) and part.is_error:
                part = replace(part, output=(Text("failed"),))
            contents.append(part)
        history.append(replace(item, content=tuple(contents)))
    return replace(base, history=tuple(history), context=tuple(history))


def native_conversation() -> Conversation:
    source = Identity("pi", "source")
    origin = Provenance("authored", source, "original")
    history = native.linked(
        [
            Message(
                "request",
                "user",
                (
                    Text("Keep original history"),
                    Instruction("Use tabs", "user", origin),
                    AttachmentRef("image"),
                ),
                origin,
            ),
            Message(
                "call",
                "assistant",
                (Text("Checking"), ToolCall("read-id", "read", '{"path":"example.txt"}', "json")),
                origin,
            ),
            Message(
                "result",
                "tool",
                (ToolResult("read-id", (Text("read failed"), AttachmentRef("image")), False),),
                origin,
            ),
            Message("answer", "assistant", (Text("Final answer"),), origin),
        ]
    )
    historical = HistoricalTimestamp("2020-01-02T03:04:05.000Z", origin)
    history = tuple(replace(item, timestamp=historical) for item in history)
    return Conversation(
        source,
        "answer",
        history,
        history,
        attachments=(Attachment("image", "image/png", base64.b64decode(PNG), "pixel.png"),),
        title="Native portability smoke",
    )


def codex_records(cwd: Path, response_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "session_meta",
            "timestamp": TIMESTAMP,
            "payload": {
                "id": SESSION_ID,
                "session_id": SESSION_ID,
                "cwd": str(cwd),
                "timestamp": TIMESTAMP,
                "originator": "codex_cli_rs",
                "cli_version": "0.155.1",
                "source": "cli",
                "model_provider": "openai",
                "base_instructions": None,
            },
        },
        *[
            {"type": "response_item", "timestamp": TIMESTAMP, "payload": item}
            for item in response_items
        ],
    ]


def _codex_tool_events(
    cwd: Path, kind: str, success: bool, call: dict[str, Any], output: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    if kind == "exec":
        common = {
            "call_id": "native-call",
            "turn_id": "native-turn",
            "command": ["/bin/sh", "-c", "printf example"],
            "cwd": str(cwd),
            "parsed_cmd": [],
            "source": "agent",
        }
        return [
            ("event_msg", {**common, "type": "exec_command_begin"}),
            (
                "event_msg",
                {
                    **common,
                    "type": "exec_command_end",
                    "stdout": "full stdout\n",
                    "stderr": "full stderr\n",
                    "aggregated_output": "full stdout\nfull stderr\n",
                    "exit_code": 0 if success else 1,
                    "duration": {"secs": 0, "nanos": 1},
                    "formatted_output": output["output"],
                    "status": "completed" if success else "failed",
                },
            ),
        ]
    if kind == "patch":
        changes = {
            "added.txt": {"type": "add", "content": "new content\n"},
            "deleted.txt": {"type": "delete", "content": "deleted full content\n"},
            "updated.txt": {
                "type": "update",
                "unified_diff": "@@ -1 +1 @@\n-old\n+new\n",
                "move_path": None,
            },
            "moved.txt": {
                "type": "update",
                "unified_diff": "@@ -1 +1 @@\n-a\n+b\n",
                "move_path": "destination.txt",
            },
        }
        return [
            (
                "event_msg",
                {
                    "type": "patch_apply_end",
                    "call_id": "native-call",
                    "turn_id": "native-turn",
                    "stdout": "",
                    "stderr": "" if success else "patch failed\n",
                    "success": success,
                    "status": "completed" if success else "failed",
                    "changes": changes,
                },
            )
        ]
    return [
        (
            "event_msg",
            {
                "type": "item_completed",
                "thread_id": SESSION_ID,
                "turn_id": "native-turn",
                "item": {
                    "type": "FunctionCallOutput",
                    "id": output["id"],
                    "name": call["name"],
                    "output": "complete native visible output",
                },
            },
        )
    ]


def codex_tool_records(cwd: Path, kind: str = "exec", success: bool = True) -> list[dict[str, Any]]:
    call = {
        "type": "custom_tool_call" if kind == "patch" else "function_call",
        "call_id": "native-call",
        "name": "apply_patch" if kind == "patch" else "exec_command",
        "input" if kind == "patch" else "arguments": "*** Begin Patch\n*** End Patch"
        if kind == "patch"
        else '{"cmd":"printf example"}',
    }
    output = {
        "type": "custom_tool_call_output" if kind == "patch" else "function_call_output",
        "id": "fco_native_result",
        "call_id": "native-call",
        "name": call["name"],
        "output": "model output truncated independently",
    }
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "event_msg",
            {"type": "task_started", "turn_id": "native-turn", "model_context_window": None},
        ),
        (
            "event_msg",
            {
                "type": "user_message",
                "message": "Use the native tool",
                "images": [],
                "local_images": [],
                "text_elements": [],
            },
        ),
        (
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Use the native tool"}],
            },
        ),
        ("response_item", call),
        *_codex_tool_events(cwd, kind, success, call, output),
        ("response_item", output),
        ("event_msg", {"type": "agent_message", "message": "Done"}),
        (
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done"}],
            },
        ),
        (
            "event_msg",
            {"type": "task_complete", "turn_id": "native-turn", "last_agent_message": "Done"},
        ),
    ]
    return [
        *codex_records(cwd, []),
        *(
            {"timestamp": TIMESTAMP, "type": row_type, "payload": payload}
            for row_type, payload in events
        ),
    ]


def codex_mcp_records(cwd: Path) -> list[dict[str, Any]]:
    rows = codex_tool_records(cwd, "completion")
    for row in rows:
        payload = row["payload"]
        if payload.get("type") == "function_call":
            payload["name"] = "mcp__files__read"
            payload["arguments"] = '{"path":"a"}'
        elif payload.get("type") == "item_completed":
            row["payload"] = {
                "type": "mcp_tool_call_end",
                "call_id": "native-call",
                "invocation": {"server": "files", "tool": "read", "arguments": {"path": "a"}},
                "duration": {"secs": 0, "nanos": 1},
                "result": {
                    "Ok": {
                        "content": [{"type": "text", "text": "complete native visible output"}],
                        "isError": False,
                    }
                },
            }
        elif payload.get("type") == "function_call_output":
            payload["name"] = "mcp__files__read"
    return rows


CODEX_BASE_INSTRUCTIONS = "CARRIED_BASE_INSTRUCTIONS: answer tersely."


def codex_paginated_records(cwd: Path) -> list[dict[str, Any]]:
    turn = "019f9b5d-a840-7f7f-aae9-b65fc6f1a65b"

    def row(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"type": kind, "timestamp": TIMESTAMP, "ordinal": 0, "payload": payload}

    def model(role: str, kinds: list[str], *texts: str, **extra: Any) -> dict[str, Any]:
        block = "output_text" if role == "assistant" else "input_text"
        return row(
            "response_item",
            {
                "type": "message",
                "role": role,
                "content": [{"type": block, "text": text} for text in texts],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": turn,
                    "content_item_kinds": kinds,
                },
                **extra,
            },
        )

    def visible(item: dict[str, Any]) -> dict[str, Any]:
        return row(
            "event_msg",
            {"type": "item_completed", "thread_id": SESSION_ID, "turn_id": turn, "item": item},
        )

    meta = codex_records(cwd, [])[0]
    meta["payload"].update(
        history_mode="paginated",
        cli_version="0.161.0",
        base_instructions={"text": CODEX_BASE_INSTRUCTIONS},
    )
    return [
        meta,
        row("event_msg", {"type": "task_started", "turn_id": turn, "started_at": 1}),
        model(
            "developer",
            ["permissions.instructions"],
            "<permissions instructions>\nsandboxed\n</permissions instructions>",
        ),
        model("user", ["user.text"], "Paginated question?"),
        visible(
            {
                "type": "UserMessage",
                "id": "item-1",
                "content": [{"type": "text", "text": "Paginated question?", "text_elements": []}],
            }
        ),
        row(
            "response_item",
            {
                "type": "function_call",
                "call_id": "spawn",
                "namespace": "collaboration",
                "name": "spawn_agent",
                "arguments": '{"task":"help"}',
            },
        ),
        row(
            "response_item",
            {"type": "function_call_output", "call_id": "spawn", "output": "spawned"},
        ),
        row(
            "response_item",
            {"type": "custom_tool_call", "call_id": "code", "name": "exec", "input": "ls"},
        ),
        row(
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": "code",
                "output": [{"type": "input_text", "text": "ran"}],
            },
        ),
        model("assistant", ["unknown"], "Paginated answer.", phase="final_answer"),
        visible(
            {
                "type": "AgentMessage",
                "id": "item-2",
                "content": [{"type": "Text", "text": "Paginated answer."}],
                "phase": "final_answer",
            }
        ),
        row("event_msg", {"type": "task_complete", "turn_id": turn, "completed_at": 2}),
    ]


CODEX_COMPACTION_CONTENT = "SYNTHETIC_OPAQUE_COMPACTION_PAYLOAD"


def codex_compacted_records(cwd: Path) -> list[dict[str, Any]]:
    first, second = "019f9b5d-a840-7f7f-aae9-b65fc6f1a65c", "019f9b5d-a840-7f7f-aae9-b65fc6f1a65d"
    rows = codex_paginated_records(cwd)
    meta = rows[0]

    def row(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"type": kind, "timestamp": TIMESTAMP, "ordinal": 0, "payload": payload}

    def text(role: str, turn: str, kinds: list[str], value: str, **extra: Any) -> dict[str, Any]:
        block = "output_text" if role == "assistant" else "input_text"
        return {
            "type": "message",
            "role": role,
            "content": [{"type": block, "text": value}],
            "internal_chat_message_metadata_passthrough": {
                "turn_id": turn,
                "content_item_kinds": kinds,
            },
            **extra,
        }

    def visible(turn: str, item: dict[str, Any]) -> dict[str, Any]:
        return row(
            "event_msg",
            {"type": "item_completed", "thread_id": SESSION_ID, "turn_id": turn, "item": item},
        )

    bootstrap = text(
        "developer",
        first,
        ["permissions.instructions"],
        "<permissions instructions>\nsandboxed\n</permissions instructions>",
    )
    question = text("user", first, ["user.text"], "Before compaction?", id="msg_retained")
    compaction = {
        "type": "compaction",
        "id": "cmp_synthetic",
        "encrypted_content": CODEX_COMPACTION_CONTENT,
        "internal_chat_message_metadata_passthrough": {"turn_id": second},
    }
    return [
        meta,
        row("event_msg", {"type": "task_started", "turn_id": first, "started_at": 1}),
        row("response_item", bootstrap),
        row("response_item", question),
        visible(
            first,
            {
                "type": "UserMessage",
                "id": "item-1",
                "content": [{"type": "text", "text": "Before compaction?", "text_elements": []}],
            },
        ),
        row(
            "response_item",
            {"type": "function_call", "call_id": "look", "name": "shell", "arguments": "{}"},
        ),
        row("response_item", {"type": "function_call_output", "call_id": "look", "output": "ok"}),
        row(
            "response_item",
            text("assistant", first, ["unknown"], "Earlier answer.", phase="final_answer"),
        ),
        visible(
            first,
            {
                "type": "AgentMessage",
                "id": "item-2",
                "content": [{"type": "Text", "text": "Earlier answer."}],
                "phase": "final_answer",
            },
        ),
        row("event_msg", {"type": "task_complete", "turn_id": first, "completed_at": 2}),
        row("event_msg", {"type": "task_started", "turn_id": second, "started_at": 3}),
        row(
            "compacted",
            {
                "message": "",
                "replacement_history": [bootstrap, question, compaction],
                "replacement_history_metadata": [
                    {"client_authored": False},
                    {"client_authored": False, "user_input_order": 1},
                    {"client_authored": False, "compaction_model_hash": "synthetic"},
                ],
                "window_number": 1,
                "first_window_id": None,
                "previous_window_id": None,
                "window_id": None,
                "compaction_response_id": None,
                "latest_token_usage_record": None,
                "resume_metadata": {
                    "multi_agent_version": None,
                    "last_started_turn_id": second,
                    "previous_turn_settings": None,
                },
            },
        ),
        visible(second, {"type": "ContextCompaction", "id": "item-3"}),
        row("response_item", text("user", second, ["user.text"], "After compaction?")),
        visible(
            second,
            {
                "type": "UserMessage",
                "id": "item-4",
                "content": [{"type": "text", "text": "After compaction?", "text_elements": []}],
            },
        ),
        row(
            "response_item",
            text("assistant", second, ["unknown"], "Later answer.", phase="final_answer"),
        ),
        visible(
            second,
            {
                "type": "AgentMessage",
                "id": "item-5",
                "content": [{"type": "Text", "text": "Later answer."}],
                "phase": "final_answer",
            },
        ),
        row("event_msg", {"type": "task_complete", "turn_id": second, "completed_at": 4}),
    ]
