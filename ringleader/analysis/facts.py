from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

type Agent = Literal["claude-code", "codex"]
type Root = Literal["claude", "codex", "archive"]
type ToolKind = Literal[
    "read", "search", "write", "validate", "shell", "mcp", "skill", "agent", "other"
]
type CallStatus = Literal["ok", "error", "unknown"]
type MessageOrigin = Literal["human", "agent", "task-notification", "peer", "unknown"]
type MessageKind = Literal[
    "correction", "frustration", "redirection", "new_task", "approval", "other"
]
type LastEvent = Literal[
    "assistant_text",
    "tool_pending",
    "tool_result",
    "user",
    "interrupt",
    "api_error",
    "turn_complete",
    "turn_aborted",
    "none",
]
type CompactionTrigger = Literal["auto", "manual", "inferred"]
type ErrorCategory = Literal[
    "interruption",
    "user_rejected",
    "permission_denied",
    "hook_blocked",
    "harness_blocked",
    "sibling_cancelled",
    "subagent_error",
    "timeout",
    "file_too_large",
    "bad_path",
    "fetch_failed",
    "file_not_read",
    "file_modified_since_read",
    "string_not_found",
    "no_changes",
    "tool_not_found",
    "input_validation",
    "syntax_error",
    "runtime_error",
    "exit_code",
]

MAIN_THREAD = "main"
COMMAND_CHARS = 500
ERROR_HEAD_CHARS = 600
ERROR_TAIL_CHARS = 400
FINAL_TEXT_CHARS = 2000
MESSAGE_CHARS = 2000
REBUILD_READ_RATIO = 0.5


@dataclass(frozen=True)
class TranscriptRef:
    path: Path
    agent: Agent
    root: Root
    archived: bool
    fingerprint: str


@dataclass(frozen=True)
class RequestFact:
    line: int
    dedupe_key: str
    timestamp: str
    model: str | None
    input: int
    output: int
    cache_read: int
    cache_write: int
    cache_write_5m: int
    cache_write_1h: int
    reasoning: int
    context_tokens: int
    context_window: int | None
    gap_seconds: float | None
    rebuild: bool
    on_main_branch: bool
    skill: str | None
    plugin: str | None


@dataclass(frozen=True)
class ToolCallFact:
    seq: int
    line: int
    call_id: str
    dedupe_key: str
    timestamp: str
    tool: str
    kind: ToolKind
    mcp_server: str | None
    mcp_tool: str | None
    skill: str | None
    command: str | None
    command_class: str
    file_path: str | None
    input_hash: str
    input_preview: str
    edit_old_hash: str | None
    edit_new_hash: str | None
    is_error: bool | None
    status: CallStatus
    error_category: ErrorCategory | None
    error_text: str | None
    signature: str | None
    on_main_branch: bool
    read_offset: int | None = None
    read_limit: int | None = None


@dataclass(frozen=True)
class UserMessageFact:
    before_call: int
    line: int
    message_id: str
    dedupe_key: str
    timestamp: str
    origin: MessageOrigin
    text: str
    is_interrupt: bool
    slash_command: str | None
    rule_kind: MessageKind | None
    content_hash: str
    on_main_branch: bool


@dataclass(frozen=True)
class CompactionFact:
    before_call: int
    line: int
    timestamp: str
    trigger: CompactionTrigger
    pre_tokens: int | None
    post_tokens: int | None


@dataclass(frozen=True)
class RewindFact:
    line: int
    timestamp: str
    branch_uuid: str
    before_call: int
    abandoned_calls: int


@dataclass(frozen=True)
class TranscriptFacts:
    ref: TranscriptRef
    session_id: str
    thread_id: str
    thread_key: str
    parent_thread_id: str | None
    parent_tool_use_id: str | None
    subagent_type: str | None
    subagent_description: str | None
    cwd: str | None
    git_branch: str | None
    first_ts: str | None
    last_ts: str | None
    final_text: str | None
    last_event: LastEvent
    api_errors: int
    requests: tuple[RequestFact, ...]
    tool_calls: tuple[ToolCallFact, ...]
    user_messages: tuple[UserMessageFact, ...]
    compactions: tuple[CompactionFact, ...]
    rewinds: tuple[RewindFact, ...]
