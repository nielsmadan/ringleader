import json
from dataclasses import replace

import pytest
from ringleader.portability import claude_attachments, native
from ringleader.portability.claude import PROFILE, ClaudeAdapter
from ringleader.portability.claude import read as read_claude
from ringleader.portability.compact import portable
from ringleader.portability.compare import compare
from ringleader.portability.model import Identity, Opaque, PreservationError, Text, ToolResult
from tests.portability.fixtures import SESSION_ID, TIMESTAMP, claude_records
from tests.portability.test_native_adapters import stage_for

IDENTITY = Identity("claude-code", SESSION_ID)


def row(kind, uuid, parent, **fields):
    return {
        "type": kind,
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": SESSION_ID,
        "timestamp": TIMESTAMP,
        **fields,
    }


def attachment(uuid, parent, payload, **fields):
    return row("attachment", uuid, parent, attachment=payload, **fields)


def base(tmp_path):
    return claude_records(
        tmp_path,
        [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
        ],
    )


def read_with(tmp_path, *extra):
    return read_claude(native.jsonl([*base(tmp_path), *extra]), IDENTITY)


def texts(messages):
    return [item.text for message in messages for item in message.content]


@pytest.mark.parametrize(
    "kind",
    [
        "permission-mode",
        "atis-latch",
        "cost-state",
        "file-history-delta",
        "agent-setting",
        "frame-link",
        "artifact-comment-monitor",
        "artifact-autoreact-ledger",
        "ended-by-model",
        "relocated",
        "bridge-session",
        "attribution-snapshot",
        "isolation-latch",
        "dev-mods",
        "memory-mode",
        "worktree-state",
        "observer-ref",
        "api-request-shape",
        "api-request-blob",
        "api-request",
    ],
)
def test_session_metadata_entries_are_dropped(tmp_path, kind):
    result = read_with(tmp_path, {"type": kind, "sessionId": SESSION_ID})
    assert texts(result.context) == ["question", "answer"]


@pytest.mark.parametrize("kind", ["history-suppression", "fork-context-ref", "continued-in"])
def test_unverified_entries_refuse(tmp_path, kind):
    with pytest.raises(PreservationError, match=f"Unsupported Claude transcript entry: {kind}"):
        read_with(tmp_path, {"type": kind, "sessionId": SESSION_ID})


def test_ai_title_is_the_fallback_title(tmp_path):
    ai_title = {"type": "ai-title", "sessionId": SESSION_ID, "aiTitle": "Generated"}
    custom = {"type": "custom-title", "sessionId": SESSION_ID, "customTitle": "Chosen"}
    assert read_with(tmp_path, ai_title).title == "Generated"
    assert read_with(tmp_path, ai_title, custom).title == "Chosen"


def test_local_command_output_reaches_the_model_as_a_user_turn(tmp_path):
    command = row("system", "local", "entry-1", subtype="local_command", content="stdout text")
    result = read_with(tmp_path, command)
    assert texts(result.history) == ["question", "answer", "stdout text"]
    assert result.context[-1].role == "user"
    assert result.context[-1].provenance.evidence == "system:local_command"


@pytest.mark.parametrize(
    "subtype",
    ["informational", "away_summary", "agents_killed", "api_error", "turn_duration"],
)
def test_system_telemetry_is_dropped(tmp_path, subtype):
    result = read_with(tmp_path, row("system", "tail", "entry-1", subtype=subtype, content="x"))
    assert texts(result.context) == ["question", "answer"]


def test_refusal_fallback_retracts_listed_rows(tmp_path):
    retracted = "11111111-2222-3333-4444-555555555555"
    rows = [
        *base(tmp_path),
        row(
            "assistant",
            retracted,
            "entry-1",
            message={"role": "assistant", "content": [{"type": "text", "text": "refused"}]},
        ),
        row(
            "system",
            "fallback-notice",
            retracted,
            subtype="model_refusal_fallback",
            retractedMessageUuids=[retracted[:24] + "000000000000"],
        ),
        row(
            "assistant",
            "retry",
            "fallback-notice",
            message={"role": "assistant", "content": [{"type": "text", "text": "retried"}]},
        ),
    ]
    result = read_claude(native.jsonl(rows), IDENTITY)
    assert texts(result.context) == ["question", "answer", "retried"]


def test_synthetic_api_error_assistant_rows_are_dropped(tmp_path):
    synthetic = row(
        "assistant",
        "err",
        "entry-1",
        isApiErrorMessage=True,
        message={
            "role": "assistant",
            "model": "<synthetic>",
            "content": [{"type": "text", "text": "API Error: overloaded"}],
        },
    )
    result = read_with(tmp_path, synthetic)
    assert texts(result.context) == ["question", "answer"]
    assert texts(result.history) == ["question", "answer"]


def test_virtual_rows_are_dropped(tmp_path):
    virtual = row(
        "user", "virt", "entry-1", isVirtual=True, message={"role": "user", "content": "virtual"}
    )
    result = read_with(tmp_path, virtual)
    assert texts(result.context) == ["question", "answer"]


def test_user_row_with_null_message_refuses_cleanly(tmp_path):
    malformed = row("user", "null-msg", "entry-1", message=None)
    with pytest.raises(PreservationError, match="Invalid Claude message role"):
        read_with(tmp_path, malformed)


def test_carried_attachment_is_hidden_harness_context_from_rendered(tmp_path):
    carried = attachment(
        "att",
        "entry-1",
        {"type": "nested_memory", "path": "/p", "content": {"path": "/p", "content": "old"}},
        rendered=[{"content": "rendered text"}, {"content": [{"type": "text", "text": "more"}]}],
    )
    result = read_with(tmp_path, carried)
    assert texts(result.history) == ["question", "answer"]
    message = result.context[-1]
    assert (message.role, message.hidden) == ("user", True)
    assert texts([message]) == ["rendered text", "more"]
    assert (message.provenance.origin, message.provenance.evidence) == (
        "harness",
        "attachment:nested_memory",
    )


def test_human_queued_prompt_is_a_visible_user_turn(tmp_path):
    queued = attachment(
        "queued",
        "entry-1",
        {
            "type": "queued_command",
            "commandMode": "prompt",
            "prompt": "next",
            "origin": {"kind": "human"},
        },
        rendered=[{"content": "rendered prompt"}],
    )
    result = read_with(tmp_path, queued)
    assert texts(result.history) == ["question", "answer", "rendered prompt"]
    assert result.history[-1].provenance.origin == "authored"
    assert result.history[-1].hidden is False


def test_task_notification_after_a_human_prompt_uses_its_human_turn_rendering(tmp_path):
    notification = {"type": "queued_command", "commandMode": "task-notification", "prompt": "done"}
    rows = [
        *base(tmp_path),
        row(
            "user",
            "prompt",
            "entry-1",
            origin={"kind": "human"},
            message={"role": "user", "content": "follow-up"},
        ),
        attachment(
            "notice",
            "prompt",
            notification,
            rendered=[{"content": "plain"}],
            renderedInHumanTurn=[{"content": "in human turn"}],
        ),
        attachment("later", "notice", notification, rendered=[{"content": "plain later"}]),
    ]
    assistant_between = row(
        "assistant",
        "reply",
        "notice",
        message={"role": "assistant", "content": [{"type": "text", "text": "reply"}]},
    )
    rows.insert(-1, assistant_between)
    rows[-1]["parentUuid"] = "reply"
    result = read_claude(native.jsonl(rows), IDENTITY)
    assert texts(result.context) == [
        "question",
        "answer",
        "in human turn",
        "follow-up",
        "reply",
        "plain later",
    ]


def test_attachments_hoist_behind_the_nearest_assistant_or_tool_result(tmp_path):
    memory = {"type": "nested_memory", "path": "/p", "content": {"path": "/p", "content": "m"}}
    rows = [
        attachment("first", None, memory, rendered=[{"content": "leading"}]),
        row("user", "u0", "first", message={"role": "user", "content": "question"}),
        row(
            "assistant",
            "a0",
            "u0",
            message={
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call", "name": "read", "input": {}}],
            },
        ),
        row(
            "user",
            "r0",
            "a0",
            message={
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call", "content": "out"}],
            },
        ),
        row("user", "u1", "r0", message={"role": "user", "content": "next"}),
        attachment("late", "u1", memory, rendered=[{"content": "hoisted"}]),
        row(
            "assistant",
            "a1",
            "late",
            message={"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        ),
    ]
    result = read_claude(native.jsonl(rows), IDENTITY)
    assert [message.id for message in result.context] == [
        "first",
        "u0",
        "a0",
        "r0",
        "late",
        "u1",
        "a1",
    ]
    assert [message.id for message in result.history] == ["u0", "a0", "r0", "u1", "a1"]


@pytest.mark.parametrize(
    "kind",
    [*sorted(claude_attachments.DROP_REGENERATED), "tool_hosts_notice", "ultra_effort_enter"],
)
def test_regenerated_attachments_are_dropped_even_with_rendered(tmp_path, kind):
    dropped = attachment("att", "entry-1", {"type": kind}, rendered=[{"content": "regenerated"}])
    assert texts(read_with(tmp_path, dropped).context) == ["question", "answer"]


@pytest.mark.parametrize(
    "kind", [*sorted(claude_attachments.DROP_TELEMETRY), "batching_reminder_sent"]
)
def test_telemetry_attachments_are_dropped_without_rendered(tmp_path, kind):
    dropped = attachment("att", "entry-1", {"type": kind})
    assert texts(read_with(tmp_path, dropped).context) == ["question", "answer"]


@pytest.mark.parametrize(
    "kind", [*sorted(claude_attachments.DROP_TELEMETRY), "batching_reminder_sent"]
)
def test_telemetry_attachment_with_stored_rendered_refuses(tmp_path, kind):
    leaked = attachment("att", "entry-1", {"type": kind}, rendered=[{"content": "telemetry"}])
    with pytest.raises(PreservationError, match=f"Unsupported Claude attachment kind: {kind}"):
        read_with(tmp_path, leaked)


@pytest.mark.parametrize(
    ("kind", "event", "content", "carried"),
    [
        ("hook_success", "SessionStart", "boot", False),
        ("hook_additional_context", "SessionStart", ["boot"], False),
        ("hook_success", "Stop", "stopped", False),
        ("hook_success", "PreToolUse", "pre", False),
        ("hook_success", "PostToolUseFailure", "failed", False),
        ("hook_success", "UserPromptSubmit", "", False),
        ("hook_additional_context", "UserPromptSubmit", [], False),
        ("hook_success", "UserPromptSubmit", "prompt hook", True),
        ("hook_success", "UserPromptExpansion", "expansion hook", True),
        ("hook_additional_context", "PostToolUse", ["post hook"], True),
    ],
)
def test_hook_attachments_follow_their_event(tmp_path, kind, event, content, carried):
    hook = attachment(
        "hook",
        "entry-1",
        {"type": kind, "hookEvent": event, "hookName": event, "content": content},
        rendered=[{"content": "hook output"}],
    )
    expected = ["question", "answer", "hook output"] if carried else ["question", "answer"]
    assert texts(read_with(tmp_path, hook).context) == expected


def test_hook_attachment_for_an_unclassified_event_refuses(tmp_path):
    hook = attachment(
        "hook",
        "entry-1",
        {"type": "hook_additional_context", "hookEvent": "PreToolUse", "content": ["x"]},
    )
    with pytest.raises(
        PreservationError,
        match="Unsupported Claude attachment kind: hook_additional_context for PreToolUse",
    ):
        read_with(tmp_path, hook)


def test_delivered_poll_events_are_dropped(tmp_path):
    poll = {"type": "poll_events", "deliveredVia": "tool"}
    dropped = attachment("poll", "entry-1", poll, rendered=[{"content": "events"}])
    assert texts(read_with(tmp_path, dropped).context) == ["question", "answer"]
    kept = attachment("poll", "entry-1", {"type": "poll_events"}, rendered=[{"content": "events"}])
    assert texts(read_with(tmp_path, kept).context)[-1] == "events"


def test_unknown_attachment_kind_refuses(tmp_path):
    with pytest.raises(PreservationError, match="Unsupported Claude attachment kind: future_kind"):
        read_with(tmp_path, attachment("att", "entry-1", {"type": "future_kind"}))


def test_carry_kind_without_rendered_or_template_refuses(tmp_path):
    record = attachment("att", "entry-1", {"type": "file", "filename": "/a", "content": {}})
    with pytest.raises(PreservationError, match="no verified template: file"):
        read_with(tmp_path, record)


def test_nested_memory_missing_nested_field_raises_preservation_error(tmp_path):
    malformed = attachment(
        "att", "entry-1", {"type": "nested_memory", "content": {"content": "body"}}
    )
    with pytest.raises(
        PreservationError, match=r"Invalid Claude attachment field: nested_memory\.path"
    ):
        read_with(tmp_path, malformed)


def reminder(text):
    return f"<system-reminder>\n{text}\n</system-reminder>"


EDITED = (
    "Note: /w/a<b>.txt changed on disk since you last read it. That's usually deliberate, so take "
    "it as the current state rather than reverting it; if the change looks wrong, say so rather "
    "than undoing it yourself \u2014 otherwise no need to call it out."
)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"type": "edited_text_file", "filename": "/w/a<b>.txt", "snippet": "1\tnew"},
            [
                reminder(
                    EDITED.replace("<b>", "&lt;b&gt;")
                    + " Here are the relevant changes (shown with line numbers):\n1\tnew"
                )
            ],
        ),
        (
            {"type": "edited_text_file", "filename": "/w/a<b>.txt", "snippet": ""},
            [
                reminder(
                    EDITED.replace("<b>", "&lt;b&gt;")
                    + " The changes are not shown here; use Read if you need the current content."
                )
            ],
        ),
        (
            {
                "type": "nested_memory",
                "path": "/w/M.md",
                "content": {"path": "/w/M.md", "content": "rules"},
            },
            [reminder("Contents of /w/M.md:\n\nrules")],
        ),
        (
            {"type": "compact_file_reference", "filename": "/w/big.py"},
            [
                reminder(
                    "Note: /w/big.py was read before the last conversation was summarized, but the "
                    "contents are too large to include. Use Read tool if you need to access it."
                )
            ],
        ),
        (
            {"type": "plan_mode_exit", "planExists": True, "planFilePath": "/w/plan.md"},
            [
                reminder(
                    "## Exited Plan Mode\n\nYou have exited plan mode. You can now make edits, run "
                    "tools, and take actions. The plan file is located at /w/plan.md if you need "
                    "to reference it."
                )
            ],
        ),
        (
            {"type": "invoked_skills", "skills": [{"name": "s", "path": "/s", "content": "body"}]},
            [reminder(claude_attachments.INVOKED_SKILLS + "### Skill: s\nPath: /s\n\nbody")],
        ),
        ({"type": "invoked_skills", "skills": []}, []),
        (
            {
                "type": "diagnostics",
                "files": [
                    {
                        "uri": "file:///w/a.ts",
                        "diagnostics": [
                            {
                                "message": "bad",
                                "severity": "Error",
                                "range": {"start": {"line": 1, "character": 2}},
                                "code": "E1",
                                "source": "ts",
                            },
                            {
                                "message": "hint",
                                "severity": 4,
                                "range": {"start": {"line": 0, "character": 0}},
                            },
                        ],
                    }
                ],
            },
            [
                reminder(
                    "<new-diagnostics>The following new diagnostic issues were detected:\n\n"
                    "a.ts:\n  \u2718 [Line 2:3] bad [E1] (ts)\n  \u25cf [Line 1:1] hint"
                    "</new-diagnostics>"
                )
            ],
        ),
        ({"type": "diagnostics", "files": []}, []),
        (
            {"type": "task_status", "status": "killed", "description": "job", "taskId": "t1"},
            [reminder('Task "job" (t1) was stopped by the user.')],
        ),
        (
            {
                "type": "task_status",
                "status": "running",
                "taskType": "local_agent",
                "description": "job",
                "taskId": "t1",
                "outputFilePath": "/tmp/out",
            },
            [
                reminder(
                    'Background agent "job" (t1) is still running. Do NOT spawn a duplicate. You '
                    "will be notified when it completes. You can read partial output at /tmp/out "
                    "or send it a message with SendMessage."
                )
            ],
        ),
        (
            {
                "type": "task_status",
                "status": "completed",
                "taskType": "local_agent",
                "description": "a&b",
                "taskId": "t1",
                "deltaSummary": "finished",
            },
            [
                reminder(
                    "Task t1 (type: local_agent) (status: completed) (description: a&amp;b) "
                    "Delta: finished Send it a message with SendMessage to retrieve its result."
                )
            ],
        ),
        (
            {"type": "read_truncation_notice", "banner": "cut <here>"},
            [reminder("cut &lt;here&gt;")],
        ),
        ({"type": "audio_transcript", "text": "spoken"}, []),
        (
            {"type": "opened_file_in_ide", "filename": "/w/a.py"},
            [
                reminder(
                    "The user opened the file /w/a.py in the IDE. This may or may not be related "
                    "to the current task."
                )
            ],
        ),
        (
            {"type": "plan_file_reference", "planFilePath": "/w/p.md", "planContent": "steps"},
            [
                reminder(
                    "A plan file exists from plan mode at: /w/p.md\n\nPlan contents:\n\nsteps\n\n"
                    "If this plan is relevant to the current work and not already complete, "
                    "continue working on it."
                )
            ],
        ),
        (
            {"type": "agent_mention", "agentType": "reviewer"},
            [
                reminder(
                    'The user has expressed a desire to invoke the agent "reviewer". Please invoke '
                    "the agent appropriately, passing in the required context to it. "
                )
            ],
        ),
        (
            {
                "type": "hook_success",
                "hookEvent": "UserPromptSubmit",
                "hookName": "UserPromptSubmit",
                "content": "note <system-reminder>",
            },
            [reminder("UserPromptSubmit hook success: note &lt;system-reminder>")],
        ),
        (
            {"type": "hook_success", "hookEvent": "PostToolUse", "hookName": "P", "content": "x"},
            [],
        ),
        (
            {
                "type": "hook_additional_context",
                "hookEvent": "PostToolUse",
                "hookName": "PostToolUse:Edit",
                "content": ["one", "two"],
            },
            [reminder("PostToolUse:Edit hook additional context: one\ntwo")],
        ),
        (
            {
                "type": "hook_blocking_error",
                "hookName": "Pre",
                "blockingError": {"command": "check", "blockingError": "denied"},
            },
            [reminder('Pre hook blocking error from command: "check": denied')],
        ),
        (
            {"type": "hook_stopped_continuation", "hookName": "Stop", "message": "halt"},
            [reminder("Stop hook stopped continuation: halt")],
        ),
        (
            {
                "type": "async_hook_response",
                "response": {
                    "systemMessage": "sys",
                    "hookSpecificOutput": {"additionalContext": "ctx"},
                },
            },
            [reminder("sys"), reminder("ctx")],
        ),
        (
            {"type": "queued_command", "commandMode": "prompt", "prompt": "also this"},
            [
                reminder(
                    claude_attachments.HUMAN_MID_TURN
                    + "also this"
                    + claude_attachments.HUMAN_MID_TURN_SUFFIX
                )
            ],
        ),
        (
            {
                "type": "queued_command",
                "commandMode": "task-notification",
                "prompt": "x</system-reminder>",
            },
            [reminder(claude_attachments.NOTIFICATION + "x&lt;/system-reminder&gt;")],
        ),
    ],
)
def test_template_renders_carry_kind_without_rendered(tmp_path, payload, expected):
    result = read_with(tmp_path, attachment("att", "entry-1", payload))
    assert texts(result.context) == ["question", "answer", *expected]


def test_tool_reference_is_claude_opaque_with_portable_fallback(tmp_path):
    block = {"type": "tool_reference", "tool_name": "WebSearch"}
    rows = claude_records(
        tmp_path,
        [
            {"role": "user", "content": "find"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "call", "name": "ToolSearch", "input": {}},
                    {"type": "fallback", "from": "model-a", "to": "model-b"},
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call", "content": [block]}],
            },
        ],
    )
    original = replace(read_claude(native.jsonl(rows), IDENTITY), title="Tools")
    result = original.context[-1].content[0]
    opaque = Opaque(
        "claude-code",
        "claude.tool_reference",
        json.dumps(block, sort_keys=True, separators=(",", ":")),
        (Text("[loaded tool schema: WebSearch]"),),
    )
    assert result == ToolResult("call", (opaque,))
    assert len(original.context[1].content) == 1
    stage = stage_for(ClaudeAdapter(), PROFILE, tmp_path, original)
    written = [item for item in native.records(stage.files[0].data) if item.get("type") == "user"]
    assert written[-1]["message"]["content"][0]["content"] == [block]
    reread = ClaudeAdapter().read_stage(stage)
    compare(original, reread, ClaudeAdapter().normalizations(PROFILE))
    assert reread.context[-1].content[0].output[0].payload == opaque.payload
    foreign = portable(original, "pi").context[-1].content[0]
    assert foreign == ToolResult("call", (Text("[loaded tool schema: WebSearch]"),))


def test_claude_writer_still_rejects_other_opaque_kinds(tmp_path):
    original = read_with(tmp_path)
    foreign = Opaque("claude-code", "claude.future", "{}")
    message = replace(original.context[-1], content=(*original.context[-1].content, foreign))
    changed = replace(
        original,
        history=(*original.history[:-1], message),
        context=(*original.context[:-1], message),
        title="x",
    )
    with pytest.raises(PreservationError, match=r"Unsupported opaque kind: claude\.future"):
        stage_for(ClaudeAdapter(), PROFILE, tmp_path, changed)


def test_content_less_rows_are_dropped(tmp_path):
    thinking = row(
        "assistant",
        "think",
        "entry-1",
        message={"role": "assistant", "content": [{"type": "thinking", "thinking": "hm"}]},
    )
    assert [message.id for message in read_with(tmp_path, thinking).context] == [
        "entry-0",
        "entry-1",
    ]


def test_carried_context_round_trips_as_meta_rows(tmp_path):
    carried = attachment(
        "att",
        "entry-1",
        {"type": "agent_mention", "agentType": "reviewer"},
        rendered=[{"content": "mention"}],
    )
    original = replace(read_with(tmp_path, carried), title="Hidden")
    stage = stage_for(ClaudeAdapter(), PROFILE, tmp_path, original)
    rows = native.records(stage.files[0].data)
    meta = [item for item in rows if item.get("isMeta")]
    assert [item["message"]["content"][0]["text"] for item in meta] == ["mention"]
    compare(original, ClaudeAdapter().read_stage(stage), ClaudeAdapter().normalizations(PROFILE))
