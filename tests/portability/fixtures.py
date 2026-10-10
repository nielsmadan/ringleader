from ringleader.portability.verify.fixtures import (
    SESSION_ID,
    SOURCE,
    TIMESTAMP,
    checkpoint_conversation,
    codex_mcp_records,
    codex_records,
    codex_tool_records,
    conversation,
    message,
    provenance,
)

__all__ = [
    "SESSION_ID",
    "SOURCE",
    "TIMESTAMP",
    "checkpoint_conversation",
    "codex_mcp_records",
    "codex_records",
    "codex_tool_records",
    "conversation",
    "message",
    "provenance",
]

NATIVE_PROFILES = {
    "claude-code": "Claude Code 2.1.278; SDK v0.2.156 session parentUuid chains",
    "pi": "Pi 0.85.1 session header v3 and message entries",
    "codex": "openai/codex rust-v0.155.1 protocol SessionMeta and ResponseItem",
    "opencode": "anomalyco/opencode v1.18.30 v1 session schema and CLI import",
}


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
