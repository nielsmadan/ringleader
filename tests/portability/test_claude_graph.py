from dataclasses import replace

import pytest
from ringleader.portability import claude_chain, native
from ringleader.portability.claude import (
    INTERRUPTED_RESULT,
    PROFILE,
    ClaudeAdapter,
    _checkpoint_stream,
)
from ringleader.portability.claude import read as read_claude
from ringleader.portability.compare import compare
from ringleader.portability.model import (
    Checkpoint,
    Conversation,
    Identity,
    Message,
    PreservationError,
    Provenance,
    Text,
    ToolCall,
    ToolResult,
)
from tests.portability.fixtures import SESSION_ID, TIMESTAMP
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


def user(uuid, parent, text, **fields):
    return row("user", uuid, parent, message={"role": "user", "content": text}, **fields)


def assistant(uuid, parent, response, *blocks, **fields):
    message = {"role": "assistant", "id": response, "content": list(blocks)}
    return row("assistant", uuid, parent, message=message, **fields)


def text(value):
    return {"type": "text", "text": value}


def call(key):
    return {"type": "tool_use", "id": key, "name": "read", "input": {"path": key}}


def result(uuid, parent, key, **fields):
    content = [{"type": "tool_result", "tool_use_id": key, "content": [text(f"out {key}")]}]
    return row("user", uuid, parent, message={"role": "user", "content": content}, **fields)


def summary(messages):
    return [
        [
            (type(item).__name__, getattr(item, "text", None) or getattr(item, "call_id", None))
            for item in message.content
        ]
        for message in messages
    ]


def read(rows):
    return read_claude(native.jsonl(rows), IDENTITY)


def roundtrip(conversation, tmp_path):
    original = replace(conversation, title="Graph")
    stage = stage_for(ClaudeAdapter(), PROFILE, tmp_path, original)
    compare(original, ClaudeAdapter().read_stage(stage), ClaudeAdapter().normalizations(PROFILE))


def fan_out():
    return [
        user("u0", None, "question"),
        assistant("a1", "u0", "msg_fan", call("t1")),
        assistant("a2", "a1", "msg_fan", call("t2")),
        assistant("a3", "a2", "msg_fan", call("t3")),
        result("r3", "a3", "t3", sourceToolAssistantUUID="a3"),
        result("r2", "a2", "t2", sourceToolAssistantUUID="a2"),
        result("r1", "a1", "t1", sourceToolAssistantUUID="a1"),
        assistant("a4", "r1", "msg_done", text("done")),
    ]


def test_parallel_fan_out_recovers_results_from_sibling_branches(tmp_path):
    conversation = read(fan_out())
    expected = [
        [("Text", "question")],
        [("ToolCall", "t1"), ("ToolCall", "t2"), ("ToolCall", "t3")],
        [("ToolResult", "t3")],
        [("ToolResult", "t2")],
        [("ToolResult", "t1")],
        [("Text", "done")],
    ]
    assert summary(conversation.context) == expected
    assert summary(conversation.history) == expected
    assert conversation.branch_id == "a4"
    roundtrip(conversation, tmp_path)


def test_result_parented_elsewhere_is_recovered_through_its_source_assistant():
    rows = [
        user("u0", None, "question"),
        assistant("a1", "u0", "msg_fan", call("t1")),
        assistant("a2", "a1", "msg_fan", call("t2")),
        result("r1", "a1", "t1", sourceToolAssistantUUID="a1"),
        result("r2", "u0", "t2", sourceToolAssistantUUID="a2"),
        assistant("a3", "r1", "msg_done", text("done")),
    ]
    assert summary(read(rows).context) == [
        [("Text", "question")],
        [("ToolCall", "t1"), ("ToolCall", "t2")],
        [("ToolResult", "t1")],
        [("ToolResult", "t2")],
        [("Text", "done")],
    ]


def test_parents_resolve_by_identity_not_file_order():
    rows = [
        assistant("a1", "u0", "msg_answer", text("answer")),
        user("u0", None, "question"),
    ]
    conversation = read(rows)
    assert summary(conversation.history) == [[("Text", "question")], [("Text", "answer")]]
    assert conversation.branch_id == "a1"


def test_unresolved_calls_inside_the_conversation_are_dropped():
    rows = [
        user("u0", None, "question"),
        assistant("a1", "u0", "msg_lost", call("lost")),
        user("u1", "a1", "next"),
        assistant("a2", "u1", "msg_ok", text("ok")),
    ]
    assert summary(read(rows).context) == [
        [("Text", "question")],
        [("Text", "next")],
        [("Text", "ok")],
    ]


def test_trailing_unresolved_call_is_closed_as_interrupted(tmp_path):
    rows = [
        user("u0", None, "question"),
        assistant("a1", "u0", "msg_last", text("working")),
        assistant("a2", "a1", "msg_last", call("pending")),
    ]
    conversation = read(rows)
    assert conversation.context[-1].content == (
        ToolResult("pending", (Text(INTERRUPTED_RESULT),), True),
    )
    assert conversation.context[-2].content == (
        Text("working"),
        ToolCall("pending", "read", '{"path":"pending"}', "json"),
    )
    assert conversation.history == conversation.context
    roundtrip(conversation, tmp_path)


def compacted(**boundary):
    metadata = {
        "trigger": "auto",
        "preTokens": 100,
        "postTokens": 10,
        "cumulativeDroppedTokens": 90,
        "durationMs": 5,
        "preCompactDiscoveredTools": [],
        "preservedSegment": {"headUuid": "u1", "anchorUuid": "summary", "tailUuid": "a1"},
        "preservedMessages": {
            "anchorUuid": "summary",
            "uuids": ["u1", "a1"],
            "allUuids": ["u1", "a1", "gone"],
        },
    }
    return [
        user("u0", None, "old question"),
        assistant("a0", "u0", "msg_old", text("old answer")),
        user("u1", "a0", "kept question"),
        assistant("a1", "u1", "msg_kept", text("kept answer")),
        row(
            "system",
            "boundary",
            None,
            subtype="compact_boundary",
            content="Conversation compacted",
            compactMetadata=metadata,
            **boundary,
        ),
        user(
            "summary",
            "boundary",
            "the summary",
            isCompactSummary=True,
            isVisibleInTranscriptOnly=True,
        ),
        user("meta", "summary", "hidden reminder", isMeta=True),
        row(
            "attachment",
            "memory",
            "meta",
            attachment={"type": "nested_memory", "path": "/m"},
            rendered=[{"content": "carried memory"}],
        ),
        user("u2", "memory", "new question"),
        assistant("a2", "u2", "msg_new", text("new answer")),
    ]


@pytest.mark.parametrize("boundary", [{"logicalParentUuid": "a1"}, {"logicalParentUuid": "u2"}, {}])
def test_preserved_segment_compaction_keeps_hidden_context_and_roundtrips(tmp_path, boundary):
    conversation = read(compacted(**boundary))
    assert summary(conversation.history) == [
        [("Text", "old question")],
        [("Text", "old answer")],
        [("Text", "kept question")],
        [("Text", "kept answer")],
        [("Text", "new question")],
        [("Text", "new answer")],
    ]
    assert summary(conversation.context) == [
        [("Text", "the summary")],
        [("Text", "kept question")],
        [("Text", "kept answer")],
        [("Text", "carried memory")],
        [("Text", "hidden reminder")],
        [("Text", "new question")],
        [("Text", "new answer")],
    ]
    (checkpoint,) = conversation.checkpoints
    assert (checkpoint.after_message_id, checkpoint.retained_message_ids) == ("a1", ("u1", "a1"))
    assert [message.hidden for message in checkpoint.following_context] == [
        True,
        True,
        False,
        False,
    ]
    roundtrip(conversation, tmp_path)


def test_context_between_summary_and_first_assistant_hoists_ahead_of_the_summary(tmp_path):
    rows = [
        user("u0", None, "old question"),
        assistant("a0", "u0", "msg_old", text("old answer")),
        row(
            "system",
            "boundary",
            None,
            subtype="compact_boundary",
            logicalParentUuid="a0",
            compactMetadata={"trigger": "manual", "preTokens": 4},
        ),
        user(
            "summary",
            "boundary",
            "the summary",
            isCompactSummary=True,
            isVisibleInTranscriptOnly=True,
        ),
        row(
            "attachment",
            "memory",
            "summary",
            attachment={"type": "nested_memory", "path": "/m"},
            rendered=[{"content": "carried memory"}],
        ),
        user("u1", "memory", "new question"),
        assistant("a1", "u1", "msg_new", text("new answer")),
    ]
    conversation = read(rows)
    assert summary(conversation.context) == [
        [("Text", "carried memory")],
        [("Text", "the summary")],
        [("Text", "new question")],
        [("Text", "new answer")],
    ]
    assert [message.hidden for message in conversation.checkpoints[0].summary] == [True, False]
    roundtrip(conversation, tmp_path)


def notification_after(*between):
    notice = row(
        "attachment",
        "notice",
        between[-1]["uuid"],
        attachment={"type": "queued_command", "commandMode": "task-notification"},
        rendered=[{"content": "plain"}],
        renderedInHumanTurn=[{"content": "in human turn"}],
    )
    return [
        user("u0", None, "question"),
        assistant("a0", "u0", "msg_answer", text("answer")),
        *between,
        notice,
    ]


def notice(conversation):
    (message,) = [
        item
        for item in conversation.context
        if item.provenance.evidence == "attachment:queued_command"
    ]
    return list(message.content)


def test_image_placeholder_rows_do_not_end_the_human_turn():
    rows = notification_after(
        user("prompt", "a0", "look", origin={"kind": "human"}),
        user("image", "prompt", [text("[Image: source: shot.png]")], isMeta=True),
    )
    assert notice(read(rows)) == [Text("in human turn")]


def test_interruption_rows_end_the_human_turn():
    rows = notification_after(
        user("prompt", "a0", "[Request interrupted by user]", origin={"kind": "human"}),
    )
    assert notice(read(rows)) == [Text("plain")]


def test_compact_summary_rows_end_the_human_turn():
    boundary = row(
        "system",
        "boundary",
        None,
        subtype="compact_boundary",
        content="Conversation compacted",
        compactMetadata={"trigger": "manual", "preTokens": 4},
        logicalParentUuid="a0",
    )
    summary = user(
        "summary", "boundary", "the summary", isCompactSummary=True, isVisibleInTranscriptOnly=True
    )
    rows = notification_after(boundary, summary)
    assert notice(read(rows)) == [Text("plain")]


def test_interleaved_parallel_call_results_merge_into_one_response():
    rows = [
        user("u0", None, "question"),
        assistant("a1", "u0", "msg_x", call("a")),
        result("ra", "a1", "a"),
        assistant("a2", "ra", "msg_x", call("b")),
        result("rb", "a2", "b"),
        assistant("a3", "rb", "msg_done", text("done")),
    ]
    assert summary(read(rows).context) == [
        [("Text", "question")],
        [("ToolCall", "a"), ("ToolCall", "b")],
        [("ToolResult", "a")],
        [("ToolResult", "b")],
        [("Text", "done")],
    ]


def test_delivery_rows_fenced_between_response_parts_move_after_it():
    rows = [
        user("u0", None, "question"),
        assistant("a1", "u0", "msg_x", text("thinking")),
        user("companion", "a1", "note", turnCompanion=True),
        assistant("a2", "companion", "msg_x", call("a")),
        result("ra", "a2", "a"),
    ]
    assert summary(read(rows).context) == [
        [("Text", "question")],
        [("Text", "thinking"), ("ToolCall", "a")],
        [("ToolResult", "a")],
        [("Text", "note")],
    ]


def test_non_string_logical_parent_uuid_refuses():
    rows = [
        user("u0", None, "question"),
        assistant("a0", "u0", "msg_a", text("answer")),
        row(
            "system",
            "boundary",
            None,
            subtype="compact_boundary",
            content="Conversation compacted",
            compactMetadata={"trigger": "manual", "preTokens": 4},
            logicalParentUuid=["a0"],
        ),
    ]
    with pytest.raises(PreservationError, match="Invalid Claude logical parent identity"):
        read(rows)


def test_interrupted_tool_result_has_harness_provenance():
    rows = [
        user("u0", None, "question"),
        assistant("a1", "u0", "msg_last", text("working")),
        assistant("a2", "a1", "msg_last", call("pending")),
    ]
    conversation = read(rows)
    assert conversation.context[-1].provenance.origin == "harness"


def test_invalid_compact_segment_metadata_refuses():
    rows = [
        row(
            "system",
            "boundary",
            None,
            subtype="compact_boundary",
            content="Conversation compacted",
            compactMetadata={"preservedSegment": {"headUuid": "x"}},
        ),
    ]
    with pytest.raises(
        PreservationError, match="Unsupported Claude retained-segment compaction metadata"
    ):
        read(rows)


def test_cyclic_parent_chain_refuses():
    rows = [
        assistant("a1", "a2", "msg_x", text("one")),
        assistant("a2", "a1", "msg_y", text("two")),
    ]
    with pytest.raises(PreservationError, match="Cyclic Claude parent chain"):
        read_claude(native.jsonl(rows), IDENTITY, branch_id="a1")


def test_missing_parent_entry_refuses():
    rows = [assistant("a1", "ghost", "msg_x", text("one"))]
    with pytest.raises(
        PreservationError, match="Claude transcript references a missing parent entry"
    ):
        read_claude(native.jsonl(rows), IDENTITY, branch_id="a1")


def test_compacted_context_refuses_for_a_leaf_outside_the_preserved_segment():
    rows = [
        user("u0", None, "question"),
        assistant("a0", "u0", "msg_a", text("answer")),
        row(
            "system",
            "boundary",
            None,
            subtype="compact_boundary",
            content="Conversation compacted",
            compactMetadata={
                "trigger": "manual",
                "preTokens": 4,
                "preservedMessages": {
                    "anchorUuid": "u0",
                    "uuids": ["u0", "a0"],
                    "allUuids": ["u0", "a0"],
                },
            },
        ),
    ]
    entries = claude_chain.index(rows)
    boundary = entries.rows["boundary"]
    with pytest.raises(
        PreservationError, match="Claude compaction removed the selected branch from context"
    ):
        claude_chain.context(entries, "missing-leaf", boundary, [])


def test_compacted_context_refuses_when_it_does_not_start_at_its_boundary():
    rows = [
        user("u0", None, "question"),
        assistant("a0", "u0", "msg_a", text("answer")),
        row(
            "system",
            "boundary",
            None,
            subtype="compact_boundary",
            content="Conversation compacted",
            compactMetadata={
                "trigger": "manual",
                "preTokens": 4,
                "preservedMessages": {
                    "anchorUuid": "summary",
                    "uuids": ["u0", "a0"],
                    "allUuids": ["u0", "a0"],
                },
            },
        ),
        user("summary", None, "the summary"),
    ]
    entries = claude_chain.index(rows)
    boundary = entries.rows["boundary"]
    with pytest.raises(
        PreservationError, match="Claude compacted context does not start at its boundary"
    ):
        claude_chain.context(entries, "u0", boundary, [])


def checkpoint_message(key, body, **fields):
    return Message(key, "user", (Text(body),), Provenance("authored", IDENTITY, key), **fields)


def test_writer_refuses_an_independent_stream_from_an_earlier_checkpoint():
    history = (checkpoint_message("m1", "one"), checkpoint_message("m2", "two"))
    conversation = Conversation(
        IDENTITY,
        "m2",
        history,
        history,
        checkpoints=(
            Checkpoint(
                "cp1",
                "m1",
                (checkpoint_message("s1", "summary"),),
                following_context=(checkpoint_message("f1", "later"),),
            ),
            Checkpoint("cp2", "m2", (checkpoint_message("s2", "summary"),)),
        ),
    )
    with pytest.raises(
        PreservationError,
        match="This native profile cannot preserve an independent post-checkpoint model stream",
    ):
        _checkpoint_stream(conversation)


def test_writer_refuses_a_following_context_that_hides_visible_history():
    history = (
        checkpoint_message("m1", "one"),
        checkpoint_message("m2", "two"),
        checkpoint_message("m3", "three"),
    )
    conversation = Conversation(
        IDENTITY,
        "m3",
        history,
        history,
        checkpoints=(
            Checkpoint(
                "cp1",
                "m1",
                (checkpoint_message("s1", "summary"),),
                following_context=(checkpoint_message("f1", "hidden", hidden=True),),
            ),
        ),
    )
    with pytest.raises(
        PreservationError,
        match="Claude cannot preserve an independent post-checkpoint model stream that hides visible history",
    ):
        _checkpoint_stream(conversation)
