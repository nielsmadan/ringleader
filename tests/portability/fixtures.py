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
                "message": item,
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
