from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from ringleader.portability import native
from ringleader.portability.adapters import builtin_adapters
from ringleader.portability.capabilities import SourceRequest, TargetRequest
from ringleader.portability.claude import PROFILE as CLAUDE_PROFILE
from ringleader.portability.claude import ClaudeAdapter
from ringleader.portability.codex import PROFILE, CodexAdapter, read_rollout
from ringleader.portability.compact import compact, portable
from ringleader.portability.compare import compare
from ringleader.portability.launch import ProbeRunner
from ringleader.portability.model import (
    Conversation,
    Identity,
    Instruction,
    Message,
    Opaque,
    PreservationError,
    Text,
    ToolCall,
    ToolOmission,
    ToolResult,
    canonical_json,
)
from ringleader.portability.pi import PROFILE as PI_PROFILE
from ringleader.portability.pi import PiAdapter
from ringleader.portability.service import ConversionRequest, ConversionService
from ringleader.portability.verification import HARNESS_VERSION, VerificationRecord
from tests.portability.fixtures import SESSION_ID, TIMESTAMP, conversation
from tests.portability.test_codex import initialized, stage
from tests.portability.test_native_adapters import roots
from tests.portability.test_service import request, service, source

TURN = "019f9b5d-a840-7f7f-aae9-b65fc6f1a601"
NEXT = "019f9b5d-a840-7f7f-aae9-b65fc6f1a602"
BASE = {"text": "Synthetic base prompt", "provenance": {"kind": "default"}}


def row(kind: str, payload: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"timestamp": TIMESTAMP, "ordinal": 0, "type": kind, "payload": payload, **extra}


def model(role: str, kinds: list[str] | None, *texts: str, **extra: Any) -> dict[str, Any]:
    block = "output_text" if role == "assistant" else "input_text"
    passthrough: dict[str, Any] = {"turn_id": TURN, "create_time": 1.5}
    if kinds is not None:
        passthrough["content_item_kinds"] = kinds
    return row(
        "response_item",
        {
            "type": "message",
            "role": role,
            "content": [{"type": block, "text": text} for text in texts],
            "internal_chat_message_metadata_passthrough": passthrough,
            **extra,
        },
    )


def item(payload: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return row(
        "response_item", {"internal_chat_message_metadata_passthrough": {}, **payload}, **extra
    )


def visible(value: dict[str, Any], turn: str = TURN) -> dict[str, Any]:
    return row(
        "event_msg",
        {
            "type": "item_completed",
            "thread_id": SESSION_ID,
            "turn_id": turn,
            "item": {"id": "item", **value},
            "completed_at_ms": 2,
        },
    )


def event(kind: str, turn: str = TURN, **extra: Any) -> dict[str, Any]:
    return row("event_msg", {"type": kind, "turn_id": turn, **extra})


def meta(**extra: Any) -> dict[str, Any]:
    return row(
        "session_meta",
        {
            "id": SESSION_ID,
            "session_id": SESSION_ID,
            "timestamp": TIMESTAMP,
            "cwd": "/synthetic/workspace",
            "originator": "codex-tui",
            "cli_version": "0.161.0",
            "source": "cli",
            "history_mode": "paginated",
            "history_base": None,
            "model_provider": "openai",
            "base_instructions": BASE,
            **extra,
        },
    )


def every_record() -> list[dict[str, Any]]:
    return [
        meta(),
        event("thread_settings_applied", thread_settings={}),
        event("task_started", started_at=1),
        row("turn_context", {"turn_id": TURN, "cwd": "/synthetic/workspace"}),
        row("world_state", {"full": True, "state": {}}),
        model(
            "developer",
            ["permissions.instructions", "host_skills.instructions"],
            "<permissions instructions>\nrules\n</permissions instructions>",
            "<skills_instructions>\ncatalog\n</skills_instructions>",
        ),
        model(
            "user",
            ["agents_md.instructions", "environments.environment_context"],
            "# AGENTS.md instructions for /synthetic\n\n<INSTRUCTIONS>\nx\n</INSTRUCTIONS>",
            "<environment_context>\n  <cwd>/synthetic</cwd>\n</environment_context>",
        ),
        model("developer", None, "Configured developer prompt"),
        model("user", None, "<environment_context>\nold\n</environment_context>"),
        model(
            "user",
            ["user.text"],
            "Spawn a helper",
            metadata={"client_authored": False, "user_input_order": 1},
        ),
        visible(
            {
                "type": "UserMessage",
                "content": [
                    {"type": "text", "text": "Spawn a helper", "text_elements": []},
                    {"type": "skill", "name": "helper-skill", "path": "/synthetic/SKILL.md"},
                    {"type": "local_image", "path": "/synthetic/shot.png"},
                ],
            }
        ),
        model("user", ["skills.selected_skill_instructions"], "<skill>\nsteps\n</skill>"),
        item({"type": "reasoning", "summary": [], "encrypted_content": "opaque"}),
        model("assistant", ["unknown"], "On it.", phase="commentary"),
        visible({"type": "AgentMessage", "content": [{"type": "Text", "text": "On it."}]}),
        visible({"type": "Reasoning", "summary_text": [], "raw_content": []}),
        item({"type": "tool_search_call", "call_id": "search", "execution": "client"}),
        item({"type": "tool_search_output", "call_id": "search", "tools": []}),
        item(
            {
                "type": "function_call",
                "call_id": "spawn",
                "namespace": "collaboration",
                "name": "spawn_agent",
                "arguments": '{"task":"help"}',
            }
        ),
        item(
            {
                "type": "custom_tool_call",
                "call_id": "code",
                "name": "exec",
                "input": "await run()",
                "status": "completed",
            }
        ),
        item({"type": "function_call_output", "call_id": "spawn", "output": "spawned"}),
        item(
            {
                "type": "custom_tool_call_output",
                "call_id": "code",
                "output": [{"type": "input_text", "text": "ran"}],
            },
            metadata={"client_authored": False, "fallback_token_limit_override": 9},
        ),
        row("inter_agent_communication_metadata", {"trigger_turn": True}),
        item(
            {
                "type": "agent_message",
                "author": "/root/helper",
                "recipient": "/root",
                "content": [
                    {"type": "input_text", "text": "Message Type: FINAL_ANSWER\nDone"},
                    {"type": "encrypted_content", "encrypted_content": "sealed"},
                ],
            }
        ),
        visible({"type": "CommandExecution", "command": ["ls", "-a b"], "status": "completed"}),
        visible(
            {"type": "FileChange", "changes": {"b.txt": {}, "a.txt": {}}, "status": "completed"}
        ),
        visible({"type": "McpToolCall", "server": "docs", "tool": "search", "status": "failed"}),
        visible({"type": "CollabAgentToolCall", "tool": "spawn_agent", "status": "completed"}),
        visible({"type": "SubAgentActivity", "kind": "completed", "agent_path": "/root/helper"}),
        visible({"type": "Extension", "kind": "web.search", "query": "synthetic query"}),
        visible({"type": "WebSearch", "query": "hosted query", "action": {"type": "search"}}),
        visible({"type": "ImageView", "path": "/synthetic/shot.png"}),
        visible({"type": "Plan", "text": "First step\nSecond step"}),
        visible({"type": "ContextCompaction"}),
        model("assistant", ["unknown"], "Finished.", phase="final_answer"),
        visible(
            {
                "type": "AgentMessage",
                "content": [{"type": "Text", "text": "Finished."}],
                "phase": "final_answer",
                "delivery": "async",
                "questions": [{"title": "Proceed?", "options": []}],
            }
        ),
        event("token_count", info=None),
        row("token_usage_record", {"usage": {}}),
        row("security_risk_score", {"score": 0}),
        row("realtime_item", {"kind": "noise"}),
        event("task_complete", completed_at=3, error={"message": "late", "codex_error_info": None}),
    ]


def read(rows: list[dict[str, Any]]) -> Conversation:
    return read_rollout(native.jsonl(rows), Identity("codex", SESSION_ID))


def texts(messages: tuple[Message, ...]) -> list[tuple[str, str]]:
    return [
        (item.role, part.text)
        for item in messages
        for part in item.content
        if isinstance(part, Text)
    ]


def test_paginated_rollout_reads_every_record_type():
    result = read(every_record())

    assert texts(result.history) == [
        ("user", "Spawn a helper"),
        ("user", "[Skill: helper-skill]"),
        ("user", "[Image: /synthetic/shot.png]"),
        ("assistant", "On it."),
        ("assistant", "[CommandExecution completed] ls '-a b'"),
        ("assistant", "[FileChange completed] a.txt, b.txt"),
        ("assistant", "[McpToolCall failed] docs.search"),
        ("assistant", "[CollabAgentToolCall completed] spawn_agent"),
        ("assistant", "[SubAgentActivity] completed /root/helper"),
        ("assistant", "[Extension] web.search synthetic query"),
        ("assistant", "[WebSearch] hosted query"),
        ("assistant", "[ImageView] /synthetic/shot.png"),
        ("assistant", "[Plan] First step"),
        ("assistant", "[ContextCompaction]"),
        ("assistant", "Finished."),
        ("assistant", "[Question: Proceed?]"),
    ]
    assert [item.provenance.evidence.split("\n")[0] for item in result.history] == [
        f"codex-turn:{TURN}"
    ] * 13
    context = result.context
    assert context[0].content == (Opaque("codex", "codex.base_instructions", canonical_json(BASE)),)
    assert [(item.role, item.hidden) for item in context[1:]] == [
        ("user", False),
        ("user", True),
        ("assistant", False),
        ("assistant", False),
        ("tool", False),
        ("tool", False),
        ("user", True),
        ("assistant", False),
    ]
    assert texts(context[1:3]) == [("user", "Spawn a helper"), ("user", "<skill>\nsteps\n</skill>")]
    commentary = Opaque("codex", "codex.phase", canonical_json({"phase": "commentary"}))
    assert context[3].content == (commentary, Text("On it."))
    assert context[4].content == (
        ToolCall("spawn", "collaboration.spawn_agent", '{"task":"help"}', "json"),
        ToolCall("code", "exec", "await run()", "freeform"),
    )
    assert [part for item in context[5:7] for part in item.content] == [
        ToolResult("spawn", (Text("spawned"),)),
        ToolResult("code", (Text("ran"),)),
    ]
    assert texts(context[7:8]) == [
        ("user", "[Agent message from /root/helper to /root]"),
        ("user", "Message Type: FINAL_ANSWER\nDone"),
    ]
    assert context[8].content[0] == Opaque(
        "codex", "codex.phase", canonical_json({"phase": "final_answer"})
    )


def test_paginated_rollout_converts_natively_to_codex_and_portably_elsewhere(tmp_path):
    original = read(every_record())
    staged = stage(tmp_path, original)
    compare(original, CodexAdapter().read_stage(staged), CodexAdapter().normalizations(PROFILE))
    meta_row = native.records(staged.files[0].data)[0]["payload"]
    assert meta_row["base_instructions"] == BASE
    elsewhere = portable(original, "pi")
    assert not any(isinstance(part, Opaque) for item in elsewhere.context for part in item.content)
    assert texts(elsewhere.context)[0] == ("user", "Spawn a helper")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda rows: rows[9].update(metadata={"delivered_assistant_message": "x"}),
            "metadata: delivered",
        ),
        (
            lambda rows: rows[9]["payload"]["internal_chat_message_metadata_passthrough"].update(
                content_item_kinds=["guardian.policy"]
            ),
            "content kind: guardian.policy",
        ),
        (
            lambda rows: rows.append(visible({"type": "DynamicToolCall"})),
            "turn item: DynamicToolCall",
        ),
        (
            lambda rows: rows.append(event("collab_waiting_end")),
            "paginated event: collab_waiting_end",
        ),
        (
            lambda rows: rows.append(
                row("compacted", {"message": "summary", "replacement_history": []})
            ),
            "^Codex paginated compaction requires checkpoint support$",
        ),
        (lambda rows: rows.append(row("retained_context", {})), "rollout record: retained_context"),
    ],
)
def test_unknown_or_unsupported_paginated_shapes_fail_closed(mutate, message):
    rows = every_record()
    mutate(rows)
    with pytest.raises(PreservationError, match=message):
        read(rows)


def command(item_id: str, status: str, exit_code: int) -> dict[str, Any]:
    return visible(
        {
            "type": "CommandExecution",
            "id": item_id,
            "command": ["synthetic-step"],
            "status": status,
            "exit_code": exit_code,
        }
    )


def exec_call(call_id: str, script: str) -> dict[str, Any]:
    return item({"type": "custom_tool_call", "call_id": call_id, "name": "exec", "input": script})


def exec_output(call_id: str, status: str, *body: str) -> dict[str, Any]:
    header = f"{status}\nWall time 0.4 seconds\nOutput:\n"
    return item(
        {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": [{"type": "input_text", "text": text} for text in (header, *body)],
        }
    )


def code_mode_rows() -> list[dict[str, Any]]:
    return [
        meta(base_instructions=None),
        event("task_started"),
        model("user", ["user.text"], "Run the steps"),
        visible({"type": "UserMessage", "content": [{"type": "text", "text": "Run the steps"}]}),
        exec_call("call_first", "await tools.exec_command({cmd: 'one'})"),
        command("exec-0001", "failed", 1),
        command("exec-0002", "completed", 0),
        command("exec-0003", "completed", 0),
        exec_output("call_first", "Script failed", "Script error:\nboom"),
        exec_call("call_second", "await tools.exec_command({cmd: 'two'})"),
        command("exec-0004", "failed", 2),
        exec_output("call_second", "Script completed", "exit 2"),
        model("assistant", ["unknown"], "Done"),
        visible({"type": "AgentMessage", "content": [{"type": "Text", "text": "Done"}]}),
        event("task_complete"),
    ]


def outcomes(conversation: Conversation) -> dict[str, bool]:
    return {
        part.call_id: part.is_error
        for item in conversation.context
        for part in item.content
        if isinstance(part, ToolResult)
    }


def test_code_mode_tool_status_is_what_the_model_saw_in_the_script_header():
    assert outcomes(read(code_mode_rows())) == {"call_first": True, "call_second": False}


def test_codex_target_reports_a_failed_script_as_failed(tmp_path):
    staged = stage(tmp_path, read(code_mode_rows()))
    written = {
        row["payload"]["call_id"]: row["payload"]["output"][0]["text"]
        for row in native.records(staged.files[0].data)
        if row.get("payload", {}).get("type") == "custom_tool_call_output"
    }
    assert written == {
        "call_first": "[Historical tool result: failed]",
        "call_second": "[Historical tool result: succeeded]",
    }


def test_direct_command_status_comes_from_the_item_sharing_its_call_id():
    rows = [
        meta(base_instructions=None),
        event("task_started"),
        model("user", ["user.text"], "Run it"),
        visible({"type": "UserMessage", "content": [{"type": "text", "text": "Run it"}]}),
        item(
            {
                "type": "function_call",
                "call_id": "call_direct",
                "name": "exec_command",
                "arguments": "{}",
            }
        ),
        item({"type": "function_call_output", "call_id": "call_direct", "output": "it stopped"}),
        command("call_direct", "failed", 1),
        model("assistant", ["unknown"], "Done"),
        visible({"type": "AgentMessage", "content": [{"type": "Text", "text": "Done"}]}),
        event("task_complete"),
    ]
    assert outcomes(read(rows)) == {"call_direct": True}


def test_paginated_thread_goal_updated_is_tolerated():
    rows = [
        meta(base_instructions=None),
        event("task_started"),
        row("event_msg", {"type": "thread_goal_updated", "turn_id": TURN, "goal": "ship it"}),
        model("user", ["user.text"], "Go"),
        visible({"type": "UserMessage", "content": [{"type": "text", "text": "Go"}]}),
        event("task_complete"),
    ]
    result = read(rows)
    assert texts(result.history) == [("user", "Go")]


def test_a_session_meta_only_rollout_has_no_conversation():
    with pytest.raises(PreservationError, match="no conversation turns"):
        read([meta()])


def test_turn_aborted_closes_the_turn_and_completes_the_open_call():
    rows = [
        meta(base_instructions=None),
        event("task_started"),
        model("user", ["user.text"], "Run it"),
        visible({"type": "UserMessage", "content": [{"type": "text", "text": "Run it"}]}),
        item(
            {"type": "function_call", "call_id": "slow", "name": "exec_command", "arguments": "{}"}
        ),
        model("user", ["generic.turn_aborted"], "<turn_aborted>\ninterrupted\n</turn_aborted>"),
        event("turn_aborted", reason="interrupted"),
        event("task_started", NEXT),
        model("user", ["user.text"], "Again"),
        visible({"type": "UserMessage", "content": [{"type": "text", "text": "Again"}]}, NEXT),
    ]
    result = read(rows)
    assert texts(result.history) == [("user", "Run it"), ("user", "Again")]
    assert [item.content[0] for item in result.context[1:3]] == [
        ToolCall("slow", "exec_command", "{}", "json"),
        ToolResult("slow", (Text("aborted"),), False),
    ]
    assert result.context[2].provenance.origin == "harness"
    assert (result.context[3].hidden, texts(result.context[3:])) == (
        True,
        [("user", "<turn_aborted>\ninterrupted\n</turn_aborted>"), ("user", "Again")],
    )


def parallel_rows(answered: str) -> list[dict[str, Any]]:
    return [
        meta(base_instructions=None),
        event("task_started"),
        model("user", ["user.text"], "Run both"),
        visible({"type": "UserMessage", "content": [{"type": "text", "text": "Run both"}]}),
        *(
            item({"type": "function_call", "call_id": call, "name": "probe", "arguments": "{}"})
            for call in ("a", "b")
        ),
        item({"type": "function_call_output", "call_id": answered, "output": f"done {answered}"}),
        model("user", ["generic.turn_aborted"], "<turn_aborted>\ninterrupted\n</turn_aborted>"),
        event("turn_aborted", reason="interrupted"),
    ]


def tool_sequence(conversation: Conversation) -> list[list[str]]:
    return [
        [
            f"call {part.call_id}" if isinstance(part, ToolCall) else f"output {part.call_id}"
            for part in item.content
        ]
        for item in conversation.context
        if item.content and isinstance(item.content[0], ToolCall | ToolResult)
    ]


@pytest.mark.parametrize(
    ("answered", "sequence"),
    [
        ("b", [["call a"], ["output a"], ["call b"], ["output b"]]),
        ("a", [["call a", "call b"], ["output b"], ["output a"]]),
    ],
)
def test_aborted_output_lands_right_after_the_call_lacking_one(answered, sequence):
    assert tool_sequence(read(parallel_rows(answered))) == sequence


def test_codex_target_writes_the_aborted_output_where_codex_resume_would(tmp_path):
    staged = stage(tmp_path, read(parallel_rows("b")))
    written = [
        (row["payload"]["type"], row["payload"]["call_id"])
        for row in native.records(staged.files[0].data)
        if row.get("payload", {}).get("type") in ("function_call", "function_call_output")
    ]
    assert written == [
        ("function_call", "a"),
        ("function_call_output", "a"),
        ("function_call", "b"),
        ("function_call_output", "b"),
    ]


def test_kindless_harness_markers_are_dropped_or_carried_not_authored():
    rows = [
        meta(base_instructions=None),
        event("task_started"),
        model("user", None, "<recommended_plugins>\nfoo\n</recommended_plugins>"),
        model(
            "user",
            None,
            "<agent_message_board_notification>\nhi\n</agent_message_board_notification>",
        ),
        model(
            "user", None, '<codex_internal_context source="ext">\nbody\n</codex_internal_context>'
        ),
        model("user", None, "<goal_context>\nold goal\n</goal_context>"),
        model("user", None, '<hook_prompt hook_run_id="h1">\nhook text\n</hook_prompt>'),
        model("user", None, "Ask the question"),
        visible({"type": "UserMessage", "content": [{"type": "text", "text": "Ask the question"}]}),
    ]
    result = read(rows)
    assert texts(result.history) == [("user", "Ask the question")]
    assert texts(result.context) == [
        ("user", "<agent_message_board_notification>\nhi\n</agent_message_board_notification>"),
        ("user", '<codex_internal_context source="ext">\nbody\n</codex_internal_context>'),
        ("user", "<goal_context>\nold goal\n</goal_context>"),
        ("user", '<hook_prompt hook_run_id="h1">\nhook text\n</hook_prompt>'),
        ("user", "Ask the question"),
    ]
    assert [item.hidden for item in result.context[:-1]] == [True] * 4
    assert result.context[-1].hidden is False


def test_unknown_kind_user_block_still_checks_text_markers():
    rows = [
        meta(base_instructions=None),
        event("task_started"),
        model("user", ["unknown"], "<recommended_plugins>\nfoo\n</recommended_plugins>"),
        model("user", ["unknown"], "Ask the question"),
        visible({"type": "UserMessage", "content": [{"type": "text", "text": "Ask the question"}]}),
    ]
    result = read(rows)
    assert texts(result.context) == [("user", "Ask the question")]


def test_thread_rolled_back_drops_the_newest_user_turns():
    def turn(turn_id: str, text: str) -> list[dict[str, Any]]:
        return [
            event("task_started", turn_id),
            model("user", ["user.text"], text),
            visible({"type": "UserMessage", "content": [{"type": "text", "text": text}]}, turn_id),
            model("assistant", ["unknown"], f"re: {text}"),
            visible(
                {"type": "AgentMessage", "content": [{"type": "Text", "text": f"re: {text}"}]},
                turn_id,
            ),
            event("task_complete", turn_id),
        ]

    rows = [
        meta(base_instructions=None),
        *turn(TURN, "first"),
        *turn(NEXT, "second"),
        row("event_msg", {"type": "thread_rolled_back", "num_turns": 1}),
        *turn("019f9b5d-a840-7f7f-aae9-b65fc6f1a603", "third"),
    ]
    result = read(rows)
    expected = [
        ("user", "first"),
        ("assistant", "re: first"),
        ("user", "third"),
        ("assistant", "re: third"),
    ]
    assert texts(result.history) == expected
    assert texts(result.context) == expected


def test_synthetic_aborted_output_is_written_as_plain_native_text(tmp_path):
    rows = [
        meta(base_instructions=None),
        event("task_started"),
        model("user", ["user.text"], "Run it"),
        visible({"type": "UserMessage", "content": [{"type": "text", "text": "Run it"}]}),
        item(
            {"type": "function_call", "call_id": "slow", "name": "exec_command", "arguments": "{}"}
        ),
        model("user", ["generic.turn_aborted"], "<turn_aborted>\ninterrupted\n</turn_aborted>"),
        event("turn_aborted", reason="interrupted"),
    ]
    original = read(rows)
    staged = stage(tmp_path, original)
    written = [
        row["payload"]
        for row in native.records(staged.files[0].data)
        if row.get("payload", {}).get("type") == "function_call_output"
    ]
    assert written == [
        {
            "type": "function_call_output",
            "call_id": "slow",
            "output": [{"type": "input_text", "text": "aborted"}],
        }
    ]
    reread = CodexAdapter().read_stage(staged)
    outcome = next(
        part for item in reread.context for part in item.content if isinstance(part, ToolResult)
    )
    assert outcome == ToolResult("slow", (Text("aborted"),), False)


def test_thread_rolled_back_also_trims_the_hidden_context_right_before_the_cut():
    def turn(turn_id: str, text: str) -> list[dict[str, Any]]:
        return [
            event("task_started", turn_id),
            model("user", ["user.text"], text),
            visible({"type": "UserMessage", "content": [{"type": "text", "text": text}]}, turn_id),
            model("assistant", ["unknown"], f"re: {text}"),
            visible(
                {"type": "AgentMessage", "content": [{"type": "Text", "text": f"re: {text}"}]},
                turn_id,
            ),
            event("task_complete", turn_id),
        ]

    rows = [
        meta(base_instructions=None),
        *turn(TURN, "first"),
        model("user", ["hooks.additional_context"], "<context>extra</context>"),
        *turn(NEXT, "second"),
        row("event_msg", {"type": "thread_rolled_back", "num_turns": 1}),
        *turn("019f9b5d-a840-7f7f-aae9-b65fc6f1a603", "third"),
    ]
    result = read(rows)
    expected = [
        ("user", "first"),
        ("assistant", "re: first"),
        ("user", "third"),
        ("assistant", "re: third"),
    ]
    assert texts(result.history) == expected
    assert texts(result.context) == expected


def test_codex_continued_legacy_ringleader_rollout_rereads(tmp_path):
    staged = stage(tmp_path, conversation())
    rows = native.records(staged.files[0].data)
    continued = [
        *rows,
        event("thread_settings_applied", thread_settings={}),
        event("task_started", NEXT, root_turn_id=NEXT),
        model(
            "developer",
            ["host_skills.instructions", "permissions.instructions"],
            "<skills_instructions>\nx\n</skills_instructions>",
            "<permissions instructions>\ny\n</permissions instructions>",
            metadata={"client_authored": False, "mcp_attribution": {"status": "ok"}},
        ),
        model(
            "user",
            ["agents_md.instructions", "environments.environment_context"],
            "# AGENTS.md instructions for /w\n\n<INSTRUCTIONS>\nz\n</INSTRUCTIONS>",
            "<environment_context>\n</environment_context>",
        ),
        row("world_state", {"full": True, "state": {}}),
        row("turn_context", {"turn_id": NEXT, "developer_instructions": "configured"}),
        model("user", ["user.text"], "Continue", metadata={"user_input_order": 2}),
        row(
            "event_msg",
            {
                "type": "user_message",
                "message": "Continue",
                "images": [],
                "local_images": [],
                "audio": [],
                "local_audio": [],
                "text_elements": [],
            },
        ),
        model("assistant", ["unknown"], "Continued.", phase="final_answer"),
        row(
            "event_msg", {"type": "agent_message", "message": "Continued.", "phase": "final_answer"}
        ),
        event("task_complete", NEXT, last_agent_message="Continued."),
    ]
    original = CodexAdapter().read_stage(staged)
    result = read_rollout(native.jsonl(continued), staged.identity)
    assert result.history[: len(original.history)] == original.history
    assert texts(result.history[len(original.history) :]) == [
        ("user", "Continue"),
        ("assistant", "Continued."),
    ]
    assert texts(result.context[len(original.context) :]) == [
        ("user", "Continue"),
        ("assistant", "Continued."),
    ]


def test_completed_codex_target_rewritten_in_place_is_identified_by_thread_id(tmp_path):
    conversion = request(tmp_path, "codex", target=initialized(tmp_path / "target"))
    first = service(tmp_path).convert(conversion)
    path = conversion.target_roots.store / first.source_ref
    rewritten = path.with_suffix(".tmp")
    rewritten.write_text(
        "\n".join(json.dumps(item) for item in native.records(path.read_bytes())) + "\n"
    )
    os.replace(rewritten, path)
    repeated = service(tmp_path).convert(conversion)
    assert (repeated.target, repeated.reused, repeated.lifecycle) == (first.target, True, "active")
    path.unlink()
    assert service(tmp_path).convert(conversion).lifecycle == "missing"


def _codex_env(base: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(base / "home"),
        "CODEX_HOME": str(base / "store"),
        "XDG_CONFIG_HOME": str(base / "config"),
        "XDG_CACHE_HOME": str(base / "cache"),
        "XDG_DATA_HOME": str(base / "data"),
        "XDG_STATE_HOME": str(base / "state"),
        "TMPDIR": str(base / "tmp"),
        "OTEL_SDK_DISABLED": "true",
    }


@pytest.mark.skipif(shutil.which("codex") is None, reason="needs the codex executable")
def test_converted_target_survives_codex_migrate_rollouts(tmp_path):
    from ringleader.portability.verify.codex import initialize, probe

    executable = shutil.which("codex") or "codex"
    version, schema = probe(executable, tmp_path / "probe")
    record = VerificationRecord(
        "codex",
        version,
        HARNESS_VERSION["codex"],
        "model-request-capture",
        datetime.now(UTC).isoformat(timespec="seconds"),
        schema,
    )
    home = tmp_path / "codex"
    target = initialize(executable, home)

    def worker() -> ConversionService:
        return ConversionService(
            builtin_adapters((record,)),
            ProbeRunner(),
            operations_path=tmp_path / "state" / "conversion",
            registry_path=tmp_path / "state" / "registry.json",
        )

    conversion = ConversionRequest(source(tmp_path), "codex", target)
    first = worker().convert(conversion)
    subprocess.run(
        [executable, "migrate-rollouts", "--apply"],
        cwd=home / "cwd",
        env=_codex_env(home),
        check=True,
        capture_output=True,
        timeout=120,
    )
    rows = native.records((target.store / first.source_ref).read_bytes())
    assert rows[0]["payload"]["history_mode"] == "paginated"
    assert all("ringleader" not in key for item in rows for key in item)

    snapshot = CodexAdapter(executable, (record,)).snapshot(
        SourceRequest(first.target, target, first.source_ref)
    )
    expected = worker().store.artifacts(worker().transactions()[0]).conversation
    authored = [
        item for item in snapshot.conversation.history if item.provenance.origin == "authored"
    ]
    assert [part for item in authored for part in item.content] == [
        Text(native.note(part)) if isinstance(part, Instruction | ToolOmission) else part
        for item in expected.history
        for part in item.content
        if not isinstance(part, ToolCall | ToolResult)
    ]
    repeated = worker().convert(conversion)
    assert (repeated.target, repeated.reused, repeated.lifecycle) == (first.target, True, "active")


SEALED = {
    "type": "compaction",
    "id": "cmp_synthetic",
    "encrypted_content": "SYNTHETIC_SEALED_SUMMARY",
    "internal_chat_message_metadata_passthrough": {"turn_id": NEXT},
}


def compacted_rows() -> list[dict[str, Any]]:
    bootstrap = model("developer", ["permissions.instructions"], "<permissions>x</permissions>")
    question = model("user", ["user.text"], "First question", id="msg_first")
    relayed = {
        "type": "agent_message",
        "id": "amsg_relayed",
        "author": "/root/helper",
        "recipient": "/root",
        "content": [{"type": "input_text", "text": "Helper finding"}],
    }
    return [
        meta(),
        event("task_started", started_at=1),
        bootstrap,
        question,
        visible({"type": "UserMessage", "content": [{"type": "text", "text": "First question"}]}),
        item({"type": "function_call", "call_id": "look", "name": "shell", "arguments": "{}"}),
        item({"type": "function_call_output", "call_id": "look", "output": "listing"}),
        model("assistant", ["unknown"], "First answer.", phase="final_answer"),
        visible({"type": "AgentMessage", "content": [{"type": "Text", "text": "First answer."}]}),
        event("task_complete", completed_at=2),
        event("task_started", NEXT, started_at=3),
        row(
            "compacted",
            {
                "message": "",
                "replacement_history": [
                    bootstrap["payload"],
                    question["payload"],
                    relayed,
                    {**SEALED},
                ],
                "replacement_history_metadata": [
                    {"client_authored": False},
                    {"client_authored": False, "user_input_order": 1},
                    {"client_authored": False},
                    {"client_authored": False, "compaction_model_hash": "synthetic"},
                ],
                "window_number": 1,
                "first_window_id": "window-1",
                "previous_window_id": None,
                "window_id": "window-2",
                "compaction_response_id": "resp-synthetic",
                "guardian_history": {"items": []},
                "latest_token_usage_record": None,
                "retained_context": {"user_messages": [], "incomplete": False},
                "resume_metadata": {"last_started_turn_id": NEXT},
            },
        ),
        visible({"type": "ContextCompaction"}, NEXT),
        model("user", ["user.text"], "Second question"),
        visible(
            {"type": "UserMessage", "content": [{"type": "text", "text": "Second question"}]}, NEXT
        ),
        model("assistant", ["unknown"], "Second answer.", phase="final_answer"),
        visible(
            {"type": "AgentMessage", "content": [{"type": "Text", "text": "Second answer."}]}, NEXT
        ),
        event("task_complete", NEXT, completed_at=4),
    ]


def shape(messages: tuple[Message, ...]) -> list[tuple[str, Any]]:
    result: list[tuple[str, Any]] = []
    for message in messages:
        for part in message.content:
            if isinstance(part, Text):
                result.append((message.role, part.text))
            elif isinstance(part, Opaque):
                result.append((message.role, part.kind))
            elif isinstance(part, ToolOmission):
                result.append((message.role, ("omitted", part.names)))
            else:
                result.append((message.role, type(part).__name__))
    return result


def test_encrypted_compaction_reads_as_an_opaque_checkpoint_over_the_plain_history():
    result = read(compacted_rows())

    (checkpoint,) = result.checkpoints
    assert checkpoint.after_message_id == next(
        item.id for item in result.history if texts((item,)) == [("assistant", "First answer.")]
    )
    assert shape(checkpoint.summary) == [
        ("system", "codex.base_instructions"),
        ("assistant", "codex.compaction"),
    ]
    assert checkpoint.summary[1].content[0].payload == canonical_json(SEALED)
    assert shape(checkpoint.replaced_context) == [
        ("system", "codex.base_instructions"),
        ("user", "First question"),
        ("assistant", "ToolCall"),
        ("tool", "ToolResult"),
        ("assistant", "codex.phase"),
        ("assistant", "First answer."),
    ]
    assert shape(checkpoint.retained_context) == [
        ("user", "First question"),
        ("user", "[Agent message from /root/helper to /root]"),
        ("user", "Helper finding"),
    ]
    assert checkpoint.retained_context[0].id == checkpoint.replaced_context[1].id
    assert shape(checkpoint.following_context) == [
        ("user", "Second question"),
        ("assistant", "codex.phase"),
        ("assistant", "Second answer."),
    ]
    assert [item.id for item in result.context] == [
        item.id
        for item in (
            *checkpoint.summary,
            *checkpoint.retained_context,
            *checkpoint.following_context,
        )
    ]


def test_compacted_session_reaches_pi_as_collapsed_history_then_retained_then_later_turns(
    tmp_path,
):
    original = replace(read(compacted_rows()), title="Compacted")
    converted = portable(original, "pi")

    assert converted.checkpoints == ()
    assert shape(converted.context) == [
        ("user", "First question"),
        ("assistant", ("omitted", ("shell",))),
        ("assistant", "First answer."),
        ("user", "[Agent message from /root/helper to /root]"),
        ("user", "Helper finding"),
        ("user", "Second question"),
        ("assistant", "Second answer."),
    ]
    adapter = PiAdapter()
    staged = adapter.stage(
        converted,
        TargetRequest(Identity("pi", str(uuid4())), roots(tmp_path), PI_PROFILE, "Compacted"),
    )
    compare(converted, adapter.read_stage(staged), adapter.normalizations(PI_PROFILE))


def test_compacted_session_converts_to_codex_keeping_the_sealed_item_after_its_retained_messages(
    tmp_path,
):
    original = portable(read(compacted_rows()), "codex")
    staged = stage(tmp_path, original)
    reread = CodexAdapter().read_stage(staged)

    compare(original, reread, CodexAdapter().normalizations(PROFILE))
    (compacted,) = [
        item for item in native.records(staged.files[0].data) if item["type"] == "compacted"
    ]
    replacement = compacted["payload"]["replacement_history"]
    assert [item["type"] for item in replacement] == ["message", "message", "compaction"]
    assert {
        key: value for key, value in replacement[-1].items() if not key.startswith("ringleader")
    } == SEALED
    assert shape(portable(reread, "pi").context) == [
        ("user", "First question"),
        ("assistant", "First answer."),
        ("user", "[Agent message from /root/helper to /root]"),
        ("user", "Helper finding"),
        ("user", "Second question"),
        ("assistant", "Second answer."),
    ]


def test_retained_messages_without_a_codex_id_reach_pi_once():
    rows = json.loads(json.dumps(compacted_rows()).replace(', "id": "msg_first"', ""))
    assert "msg_first" not in json.dumps(rows)
    assert shape(portable(read(rows), "pi").context) == [
        ("user", "First question"),
        ("assistant", ("omitted", ("shell",))),
        ("assistant", "First answer."),
        ("user", "[Agent message from /root/helper to /root]"),
        ("user", "Helper finding"),
        ("user", "Second question"),
        ("assistant", "Second answer."),
    ]


def test_compacted_session_compacts_for_a_codex_target_and_reads_back(tmp_path):
    compacted = compact(portable(read(compacted_rows()), "codex")).conversation
    assert all(not isinstance(part, Opaque) for item in compacted.history for part in item.content)
    adapter = CodexAdapter()
    reread = adapter.read_stage(stage(tmp_path, compacted))
    compare(compacted, reread, adapter.normalizations(PROFILE))
    assert shape(reread.checkpoints[-1].summary) == [
        ("system", "codex.base_instructions"),
        ("assistant", "codex.compaction"),
    ]


def test_over_budget_compacted_session_drops_its_oldest_turn_and_reads_back(tmp_path, monkeypatch):
    long = "First answer. " + "x" * 400
    rows = json.loads(json.dumps(compacted_rows()).replace("First answer.", long))
    monkeypatch.setattr(sys.modules[portable.__module__], "MAX_PORTABLE_TOKENS", 60)
    for adapter, profile in ((PiAdapter(), PI_PROFILE), (ClaudeAdapter(), CLAUDE_PROFILE)):
        converted = replace(portable(read(rows), profile.agent), title="Compacted")
        assert shape(converted.context) == [
            ("user", "[2 earlier messages omitted to fit the converted context]"),
            ("user", "First question"),
            ("user", "[Agent message from /root/helper to /root]"),
            ("user", "Helper finding"),
            ("user", "Second question"),
            ("assistant", "Second answer."),
        ]
        staged = adapter.stage(
            converted,
            TargetRequest(
                Identity(profile.agent, str(uuid4())),
                roots(tmp_path / profile.agent),
                profile,
                "Compacted",
            ),
        )
        compare(converted, adapter.read_stage(staged), adapter.normalizations(profile))


def natively_recompacted(tmp_path, **changes: Any) -> tuple[list[dict[str, Any]], Identity]:
    staged = stage(tmp_path, portable(read(compacted_rows()), "codex"))
    rows = native.records(staged.files[0].data)
    question = model("user", ["user.text"], "First question")["payload"]
    payload = {
        "message": "",
        "replacement_history": [question, {**SEALED, "id": "cmp_native"}],
        "replacement_history_metadata": [
            {"client_authored": False, "user_input_order": 1},
            {"client_authored": False, "compaction_model_hash": "synthetic"},
        ],
        "window_number": 2,
        "resume_metadata": {"last_started_turn_id": TURN},
        **changes,
    }
    rows += [
        event("task_started", TURN, started_at=5),
        row("compacted", payload),
        event("task_complete", TURN, completed_at=6),
    ]
    return rows, staged.identity


def test_codex_native_compaction_of_a_converted_legacy_rollout_reads_like_paginated(tmp_path):
    rows, identity = natively_recompacted(tmp_path / "first")
    reread = read_rollout(native.jsonl(rows), identity)
    checkpoint = reread.checkpoints[-1]
    assert shape(checkpoint.summary) == [
        ("system", "codex.base_instructions"),
        ("assistant", "codex.compaction"),
    ]
    assert shape(checkpoint.retained_context) == [("user", "First question")]
    again = portable(reread, "codex")
    adapter = CodexAdapter()
    compare(
        again,
        adapter.read_stage(stage(tmp_path / "second", again)),
        adapter.normalizations(PROFILE),
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"mcp_resource_origins": {}}, "Unsupported Codex compaction field: mcp_resource_origins"),
        ({"message": "plaintext"}, "encrypted compaction with a plaintext summary"),
        (
            {"replacement_history_metadata": [{"novel": 1}, None]},
            "Unsupported Codex response metadata: novel",
        ),
        (
            {"replacement_history": [{**SEALED}, model("user", None, "x")["payload"]]},
            "compaction item must close replacement_history",
        ),
    ],
)
def test_legacy_native_compaction_fails_closed_like_paginated(tmp_path, changes, message):
    rows, identity = natively_recompacted(tmp_path, **changes)
    with pytest.raises(PreservationError, match=message):
        read_rollout(native.jsonl(rows), identity)


def _compacted(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return next(item["payload"] for item in rows if item["type"] == "compacted")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda rows: _compacted(rows)["replacement_history"].reverse(),
            "compaction item must close replacement_history",
        ),
        (
            lambda rows: _compacted(rows).update(mcp_resource_origins={}),
            "Unsupported Codex compaction field: mcp_resource_origins",
        ),
        (
            lambda rows: _compacted(rows).update(message="plaintext"),
            "encrypted compaction with a plaintext summary",
        ),
        (
            lambda rows: _compacted(rows)["replacement_history_metadata"].pop(),
            "replacement_history_metadata disagrees",
        ),
        (
            lambda rows: _compacted(rows)["replacement_history"][-1].update(summary="x"),
            "Unsupported Codex compaction item fields",
        ),
        (
            lambda rows: _compacted(rows)["replacement_history"][-1].update(encrypted_content=""),
            "compaction item has no encrypted content",
        ),
        (
            lambda rows: rows.append(item(dict(SEALED))),
            "compaction item outside a compacted record",
        ),
        (
            lambda rows: rows.append(event("thread_rolled_back", NEXT, num_turns=1)),
            "rollback after a compaction is unsupported",
        ),
    ],
)
def test_unsupported_encrypted_compaction_shapes_fail_closed(mutate, message):
    rows = compacted_rows()
    mutate(rows)
    with pytest.raises(PreservationError, match=message):
        read(rows)
