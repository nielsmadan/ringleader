from dataclasses import replace

from ringleader.portability.model import (
    Attachment,
    AttachmentRef,
    Checkpoint,
    Conversation,
    Identity,
    Instruction,
    Message,
    Provenance,
    Text,
    ToolCall,
    ToolResult,
)

SOURCE = Identity("pi", "source-session")
TIMESTAMP = "2026-09-21T12:00:00.000Z"
SESSION_ID = "019f9b5d-a83f-7f7f-aae9-b65fc6f1a65a"
NATIVE_PROFILES = {
    "claude-code": "Claude Code 2.1.278; SDK v0.2.156 session parentUuid chains",
    "pi": "Pi 0.85.1 session header v3 and message entries",
    "codex": "openai/codex rust-v0.155.1 protocol SessionMeta and ResponseItem",
    "opencode": "anomalyco/opencode v1.18.30 v1 session schema and CLI import",
}


def provenance(locator="user-entry", origin="authored"):
    return Provenance(origin, SOURCE, locator)


def message(id, role, *content, parent=None):
    return Message(id, role, content, provenance(id), parent)


def conversation():
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


def checkpoint_conversation():
    base = conversation()
    summary = message("summary", "user", Text("Earlier work was summarized."))
    checkpoint = Checkpoint("compact-1", "results", (summary,), ("request", "calls", "results"))
    context = (summary, replace(base.history[1], parent_id="summary"), *base.history[2:])
    return replace(base, context=context, checkpoints=(checkpoint,))


def claude_records(cwd, messages):
    return [
        {
            "type": item["role"],
            "uuid": f"entry-{index}",
            "parentUuid": f"entry-{index - 1}" if index else None,
            "sessionId": SESSION_ID,
            "cwd": str(cwd),
            "timestamp": TIMESTAMP,
            "isSidechain": False,
            "message": item,
        }
        for index, item in enumerate(messages)
    ]


def pi_records(cwd, messages):
    return [
        {
            "type": "session",
            "version": 3,
            "id": SESSION_ID,
            "timestamp": TIMESTAMP,
            "cwd": str(cwd),
        },
        *[
            {
                "type": "message",
                "id": f"{index:08x}",
                "parentId": f"{index - 1:08x}" if index else None,
                "timestamp": TIMESTAMP,
                "message": {
                    **({"stopReason": "stop"} if item["role"] == "assistant" else {}),
                    **item,
                },
            }
            for index, item in enumerate(messages)
        ],
    ]


def codex_records(cwd, response_items):
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


def opencode_export(cwd, messages):
    return {
        "info": {
            "id": "ses_fixture",
            "slug": "fixture",
            "projectID": "global",
            "directory": str(cwd),
            "title": "Fixture",
            "version": "1.18.30",
            "time": {"created": 1, "updated": 1},
        },
        "messages": messages,
    }


def codex_tool_records(cwd, kind="exec", success=True):
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
    rows = codex_records(cwd, [])
    events = [
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
    ]
    if kind == "exec":
        common = {
            "call_id": "native-call",
            "turn_id": "native-turn",
            "command": ["/bin/sh", "-c", "printf example"],
            "cwd": str(cwd),
            "parsed_cmd": [],
            "source": "agent",
        }
        events.extend(
            [
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
        )
    elif kind == "patch":
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
        events.append(
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
        )
    else:
        events.append(
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
        )
    events.extend(
        [
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
    )
    return [
        *rows,
        *(
            {"timestamp": TIMESTAMP, "type": row_type, "payload": payload}
            for row_type, payload in events
        ),
    ]


def codex_mcp_records(cwd):
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
