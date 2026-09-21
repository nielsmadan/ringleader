from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from ringleader.portability import native
from ringleader.portability.adapters import builtin_adapters
from ringleader.portability.capabilities import NativeRoots, RunResult, SourceRequest, TargetRequest
from ringleader.portability.claude import PROFILE as CLAUDE_PROFILE
from ringleader.portability.claude import ClaudeAdapter
from ringleader.portability.claude import read as read_claude
from ringleader.portability.compare import compare
from ringleader.portability.model import (
    Attachment,
    AttachmentRef,
    Checkpoint,
    Conversation,
    HistoricalTimestamp,
    Identity,
    Instruction,
    Message,
    PreservationError,
    Provenance,
    Text,
    ToolCall,
    ToolOmission,
    ToolResult,
)
from ringleader.portability.pi import PROFILE as PI_PROFILE
from ringleader.portability.pi import SUMMARY_PREFIX, SUMMARY_SUFFIX, PiAdapter
from ringleader.portability.pi import read as read_pi
from tests.portability.fixtures import SESSION_ID, TIMESTAMP, claude_records, pi_records

ADAPTERS = ((ClaudeAdapter(), CLAUDE_PROFILE), (PiAdapter(), PI_PROFILE))


def roots(tmp_path):
    return NativeRoots(
        tmp_path / "home",
        tmp_path / "store",
        tmp_path / "cwd",
        tmp_path / "config",
        tmp_path / "cache",
    )


def shared():
    source = Identity("source", "original")
    provenance = Provenance("authored", source, "message")
    history = native.linked(
        [
            Message(
                "1",
                "user",
                (
                    Text("literal # AGENTS.md instruction"),
                    AttachmentRef("img"),
                    Instruction("Use tabs.", "user", provenance),
                ),
                provenance,
            ),
            Message(
                "2",
                "assistant",
                (Text("Checking"), ToolCall("call", "read", '{ "path": "a" }', "json")),
                provenance,
            ),
            Message(
                "3",
                "tool",
                (ToolResult("call", (Text("Error"), AttachmentRef("img")), True),),
                provenance,
            ),
            Message(
                "4", "assistant", (Text("Finished"), ToolOmission(("old_tool",), 1, 1)), provenance
            ),
        ]
    )
    return Conversation(
        source,
        "4",
        history,
        history,
        attachments=(Attachment("img", "image/png", b"image bytes", "capture.png"),),
        title="Imported example",
    )


def stage_for(adapter, profile, tmp_path, conversation=None):
    conversation = conversation or shared()
    request = TargetRequest(
        Identity(profile.agent, str(uuid4())), roots(tmp_path), profile, conversation.title
    )
    return adapter.stage(conversation, request)


@pytest.mark.parametrize(("adapter", "profile"), ADAPTERS)
def test_native_roundtrip_preserves_content_provenance_and_owned_bytes(adapter, profile, tmp_path):
    original = shared()
    stage = stage_for(adapter, profile, tmp_path, original)
    compare(original, adapter.read_stage(stage), adapter.normalizations(profile))
    assert list(tmp_path.iterdir()) == []
    rows = native.records(stage.files[0].data)
    messages = [row["message"] for row in rows if "message" in row]
    assert messages[0]["content"][0]["text"] == "literal # AGENTS.md instruction"
    assert "Historical user instruction" in messages[0]["content"][2]["text"]
    assert all(message["role"] in ("user", "assistant", "toolResult") for message in messages)
    assert stage.files[0].visibility
    assert stage.source_ref.startswith(
        "projects/" if profile.agent == "claude-code" else "sessions/"
    )
    for message in messages:
        if message["role"] == "assistant":
            assert all(value == 0 for key, value in message["usage"].items() if key != "cost")


@pytest.mark.parametrize(("adapter", "profile"), ADAPTERS)
def test_native_annotation_cannot_restore_modified_historical_content(adapter, profile, tmp_path):
    stage = stage_for(adapter, profile, tmp_path)
    rows = native.records(stage.files[0].data)
    row = next(row for row in rows if "message" in row)
    row["message"]["content"][2]["text"] = "dropped instruction"
    changed = replace(stage, files=(replace(stage.files[0], data=native.jsonl(rows)),))
    with pytest.raises(PreservationError, match="annotation disagrees"):
        adapter.read_stage(changed)


@pytest.mark.parametrize(("adapter", "profile"), ADAPTERS)
def test_unknown_content_and_freeform_tools_fail_before_any_files(adapter, profile, tmp_path):
    original = shared()
    messages = list(original.history)
    messages[1] = replace(messages[1], content=(ToolCall("call", "read", "read a", "freeform"),))
    original = replace(original, history=tuple(messages), context=tuple(messages))
    with pytest.raises(PreservationError, match="freeform"):
        stage_for(adapter, profile, tmp_path, original)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(("adapter", "profile"), ADAPTERS)
def test_source_snapshot_is_owned_and_version_probe_is_separate(adapter, profile, tmp_path):
    stage = stage_for(adapter, profile, tmp_path)
    source = tmp_path / "source.jsonl"
    source.write_bytes(stage.files[0].data)
    snapshot = adapter.snapshot(SourceRequest(stage.identity, stage.roots, str(source)))
    source.unlink()
    assert snapshot.conversation.attachments[0].data == b"image bytes"
    assert snapshot.identity() == snapshot.identity()
    assert adapter.launch_request(stage.identity, stage.roots).argv == (
        adapter.executable,
        "--resume" if profile.agent == "claude-code" else "--session",
        stage.identity.native_id,
    )
    assert adapter.launch_request(stage.identity, stage.roots).cwd == stage.roots.cwd


@pytest.mark.parametrize(
    ("adapter", "profile", "version"),
    [
        (ClaudeAdapter(), CLAUDE_PROFILE, "2.1.278 (Claude Code)"),
        (PiAdapter(), PI_PROFILE, "0.85.1"),
    ],
)
def test_writer_profile_requires_verified_version(adapter, profile, version, tmp_path):
    class Runner:
        def run(self, request):
            assert request.argv[-1] == "--version"
            return RunResult(0, version.encode(), b"")

    assert adapter.inspect_target(roots(tmp_path), Runner()).require() == profile
    version = "future unknown version"
    assert not adapter.inspect_target(roots(tmp_path), Runner()).supported


def test_pi_tree_selects_native_last_entry_and_explicit_branch(tmp_path):
    rows = pi_records(
        tmp_path,
        [
            {"role": "user", "content": "root"},
            {"role": "assistant", "content": [{"type": "text", "text": "left"}]},
        ],
    )
    rows.append(
        {
            "type": "message",
            "id": "right",
            "parentId": "00000000",
            "timestamp": TIMESTAMP,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "right"}],
                "stopReason": "stop",
            },
        }
    )
    identity = Identity("pi", SESSION_ID)
    assert read_pi(native.jsonl(rows), identity).history[-1].content == (Text("right"),)
    assert read_pi(native.jsonl(rows), identity, "00000001").history[-1].content == (Text("left"),)
    with pytest.raises(PreservationError, match="Unknown selected branch"):
        read_pi(native.jsonl(rows), identity, "missing")
    rows[-1]["parentId"] = "right"
    with pytest.raises(PreservationError, match="cyclic"):
        read_pi(native.jsonl(rows), identity)


def test_pi_custom_context_and_branch_summary_are_not_extension_state(tmp_path):
    rows = pi_records(tmp_path, [{"role": "user", "content": "root"}])
    rows.extend(
        [
            {
                "type": "custom",
                "id": "state",
                "parentId": "00000000",
                "timestamp": TIMESTAMP,
                "customType": "state",
                "data": {"secret": 2},
            },
            {
                "type": "custom_message",
                "id": "context",
                "parentId": "state",
                "timestamp": TIMESTAMP,
                "customType": "context",
                "content": "keep context",
                "display": False,
            },
            {
                "type": "branch_summary",
                "id": "branch",
                "parentId": "context",
                "timestamp": TIMESTAMP,
                "fromId": "other",
                "summary": "other branch work",
            },
        ]
    )
    result = read_pi(native.jsonl(rows), Identity("pi", SESSION_ID))
    assert len(result.history) == 2
    assert result.context[1].content == (Text("keep context"),)
    assert result.context[2].content[0].text.endswith("other branch work</summary>")
    assert result.context[1].provenance.evidence == "custom_message:context;display=False"


def pi_compacted(tmp_path, retained_tail=False):
    rows = pi_records(
        tmp_path, [{"role": "user", "content": "old"}, {"role": "user", "content": "retained"}]
    )
    checkpoint = {
        "type": "compaction",
        "id": "compacted",
        "parentId": "00000001",
        "timestamp": TIMESTAMP,
        "summary": "summary",
        "tokensBefore": 42,
        "firstKeptEntryId": "00000001",
    }
    if retained_tail:
        checkpoint.pop("firstKeptEntryId")
        checkpoint["retainedTail"] = [{"role": "user", "content": "embedded replacement"}]
    rows.append(checkpoint)
    rows.append(
        {
            "type": "message",
            "id": "after",
            "parentId": "compacted",
            "timestamp": TIMESTAMP,
            "message": {"role": "user", "content": "after"},
        }
    )
    return read_pi(native.jsonl(rows), Identity("pi", SESSION_ID))


def test_pi_both_checkpoint_forms_preserve_active_context(tmp_path):
    old = pi_compacted(tmp_path)
    assert [item.content[0].text for item in old.context] == [
        SUMMARY_PREFIX + "summary" + SUMMARY_SUFFIX,
        "retained",
        "after",
    ]
    stage = stage_for(PiAdapter(), PI_PROFILE, tmp_path, replace(old, title="Compacted"))
    compare(replace(old, title="Compacted"), PiAdapter().read_stage(stage))
    newer = pi_compacted(tmp_path, True)
    assert newer.checkpoints[0].retained_context[0].content == (Text("embedded replacement"),)
    assert newer.context[1].content == (Text("embedded replacement"),)
    with pytest.raises(PreservationError, match="does not honor embedded retainedTail"):
        stage_for(PiAdapter(), PI_PROFILE, tmp_path, newer)


def test_claude_compaction_preserves_logical_history_and_active_chain(tmp_path):
    rows = claude_records(
        tmp_path,
        [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
        ],
    )
    rows.append(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary",
            "parentUuid": None,
            "logicalParentUuid": "entry-1",
            "sessionId": SESSION_ID,
            "timestamp": TIMESTAMP,
            "compactMetadata": {"trigger": "auto", "preTokens": 4},
        }
    )
    rows.append(
        {
            "type": "user",
            "uuid": "summary",
            "parentUuid": "boundary",
            "sessionId": SESSION_ID,
            "timestamp": TIMESTAMP,
            "isCompactSummary": True,
            "message": {"role": "user", "content": "summary"},
        }
    )
    rows.append(
        {
            "type": "assistant",
            "uuid": "after",
            "parentUuid": "summary",
            "sessionId": SESSION_ID,
            "timestamp": TIMESTAMP,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "after"}]},
        }
    )
    result = read_claude(native.jsonl(rows), Identity("claude-code", SESSION_ID))
    assert [item.content[0].text for item in result.history] == ["old", "answer", "after"]
    assert [item.content[0].text for item in result.context] == ["summary", "after"]
    original = replace(result, title="Compacted")
    stage = stage_for(ClaudeAdapter(), CLAUDE_PROFILE, tmp_path, original)
    compare(original, ClaudeAdapter().read_stage(stage))


def test_claude_retained_history_and_owned_context_roundtrip(tmp_path):
    original = shared()
    summary = Message("summary", "user", (Text("summary"),), original.history[0].provenance)
    checkpoint = Checkpoint("compact", "3", (summary,), ("1", "2", "3"))
    context = native.linked([summary, *original.history])
    original = replace(original, checkpoints=(checkpoint,), context=context)
    stage = stage_for(ClaudeAdapter(), CLAUDE_PROFILE, tmp_path, original)
    compare(
        original, ClaudeAdapter().read_stage(stage), ClaudeAdapter().normalizations(CLAUDE_PROFILE)
    )
    checkpoint = replace(
        checkpoint, retained_message_ids=(), retained_context=(replace(summary, id="retained"),)
    )
    original = replace(
        original,
        checkpoints=(checkpoint,),
        context=native.linked([summary, *checkpoint.retained_context, original.history[-1]]),
    )
    stage = stage_for(ClaudeAdapter(), CLAUDE_PROFILE, tmp_path, original)
    compare(
        original, ClaudeAdapter().read_stage(stage), ClaudeAdapter().normalizations(CLAUDE_PROFILE)
    )


def test_claude_sidechains_progress_explicit_branch_and_replacements(tmp_path):
    rows = claude_records(
        tmp_path,
        [
            {"role": "user", "content": "root"},
            {"role": "assistant", "content": [{"type": "text", "text": "left"}]},
        ],
    )
    rows.append(
        {"type": "progress", "uuid": "progress", "parentUuid": "entry-0", "sessionId": SESSION_ID}
    )
    rows.append(
        {
            **rows[1],
            "uuid": "right",
            "parentUuid": "progress",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "right"}],
                "stopReason": "stop",
            },
        }
    )
    rows.append({**rows[1], "uuid": "side", "isSidechain": True})
    identity = Identity("claude-code", SESSION_ID)
    assert read_claude(native.jsonl(rows), identity).history[-1].content == (Text("right"),)
    assert read_claude(native.jsonl(rows), identity, "entry-1").history[-1].content == (
        Text("left"),
    )
    rows.append(
        {
            "type": "content-replacement",
            "sessionId": SESSION_ID,
            "replacements": [{"unknown": "required content"}],
        }
    )
    with pytest.raises(PreservationError, match="content-replacement"):
        read_claude(native.jsonl(rows), identity)


@pytest.mark.parametrize(
    ("reader", "builder", "agent"),
    [(read_claude, claude_records, "claude-code"), (read_pi, pi_records, "pi")],
)
def test_unknown_blocks_malformed_json_and_incomplete_tools_are_fatal(
    reader, builder, agent, tmp_path
):
    identity = Identity(agent, SESSION_ID)
    rows = builder(
        tmp_path, [{"role": "user", "content": [{"type": "future", "payload": "must preserve"}]}]
    )
    with pytest.raises(PreservationError, match=r"Unsupported .* block"):
        reader(native.jsonl(rows), identity)
    with pytest.raises(PreservationError, match="Invalid native JSONL"):
        reader(native.jsonl(rows) + b"{broken", identity)
    block = (
        {"type": "tool_use", "id": "call", "name": "read", "input": {}}
        if agent == "claude-code"
        else {"type": "toolCall", "id": "call", "name": "read", "arguments": {}}
    )
    rows = builder(tmp_path, [{"role": "assistant", "content": [block]}])
    with pytest.raises(PreservationError, match="Incomplete tool exchanges"):
        reader(native.jsonl(rows), identity)


def test_registry_factory_has_independent_capabilities():
    registry = builtin_adapters()
    assert registry.reader("claude") is registry.writer("claude-code")
    assert registry.launcher("pi").executable == "pi"


@pytest.mark.parametrize("excluded", [True, False])
def test_pi_bash_execution_fails_explicitly_for_both_context_modes(tmp_path, excluded):
    rows = pi_records(
        tmp_path,
        [{"role": "bashExecution", "command": "ls", "output": "a", "excludeFromContext": excluded}],
    )
    with pytest.raises(
        PreservationError, match=f"Unsupported Pi bashExecution with excludeFromContext={excluded}"
    ):
        read_pi(native.jsonl(rows), Identity("pi", SESSION_ID))


@pytest.mark.parametrize(("adapter", "profile"), ADAPTERS)
def test_historical_authority_remains_labeled_user_history(adapter, profile, tmp_path):
    original = shared()
    provenance = original.history[0].provenance
    instruction = Message(
        "historical-policy",
        "developer",
        (Instruction("Original project policy", "developer", provenance),),
        provenance,
    )
    history = native.linked([instruction, *original.history])
    original = replace(original, history=history, context=history)
    stage = stage_for(adapter, profile, tmp_path, original)
    compare(original, adapter.read_stage(stage), adapter.normalizations(profile))
    first = next(row["message"] for row in native.records(stage.files[0].data) if "message" in row)
    assert first["role"] == "user"
    assert first["content"][0]["text"].startswith(
        "[Historical developer instruction; origin source:original at message]"
    )


def test_hidden_pi_context_cannot_be_promoted_to_visible_history(tmp_path):
    rows = pi_records(tmp_path, [{"role": "user", "content": "visible"}])
    rows.append(
        {
            "type": "custom_message",
            "id": "hidden",
            "parentId": "00000000",
            "timestamp": TIMESTAMP,
            "customType": "context",
            "content": "hidden context",
            "display": False,
        }
    )
    conversation = read_pi(native.jsonl(rows), Identity("pi", SESSION_ID))
    assert tuple(message.content for message in conversation.history) == ((Text("visible"),),)
    assert tuple(message.content for message in conversation.context) == (
        (Text("visible"),),
        (Text("hidden context"),),
    )
    for adapter, profile in ADAPTERS:
        with pytest.raises(PreservationError, match="distinct history/context"):
            stage_for(adapter, profile, tmp_path, conversation)


def test_claude_meta_context_remains_hidden_and_selected_after_visible_parent(tmp_path):
    rows = claude_records(
        tmp_path,
        [{"role": "user", "content": "visible"}, {"role": "user", "content": "hidden context"}],
    )
    rows[-1]["isMeta"] = True
    result = read_claude(native.jsonl(rows), Identity("claude-code", SESSION_ID))
    assert tuple(message.content for message in result.history) == ((Text("visible"),),)
    assert tuple(message.content for message in result.context) == (
        (Text("visible"),),
        (Text("hidden context"),),
    )


@pytest.mark.parametrize(("source_adapter", "source_profile"), ADAPTERS)
@pytest.mark.parametrize(("target_adapter", "target_profile"), ADAPTERS)
def test_same_and_cross_agent_source_independence(
    source_adapter, source_profile, target_adapter, target_profile, tmp_path
):
    source_stage = stage_for(source_adapter, source_profile, tmp_path)
    source_path = tmp_path / "source.jsonl"
    source_path.write_bytes(source_stage.files[0].data)
    snapshot = source_adapter.snapshot(
        SourceRequest(source_stage.identity, source_stage.roots, str(source_path))
    )
    source_path.unlink()
    target = stage_for(target_adapter, target_profile, tmp_path, snapshot.conversation)
    compare(
        snapshot.conversation,
        target_adapter.read_stage(target),
        target_adapter.normalizations(target_profile),
    )
    assert target_adapter.read_stage(target).attachments[0].data == b"image bytes"


@pytest.mark.parametrize(("adapter", "profile"), ADAPTERS)
def test_version_preflight_cannot_initialize_the_destination_store(adapter, profile, tmp_path):
    target_roots = roots(tmp_path)
    homes = []

    class Runner:
        def run(self, request):
            environment = dict(request.env)
            home = Path(environment["HOME"])
            assert home != target_roots.home
            assert request.cwd != target_roots.cwd
            (home / "eager-native-startup-config").write_text("probe only")
            homes.append(home)
            version = "2.1.278 (Claude Code)" if profile.agent == "claude-code" else "0.85.1"
            return RunResult(0, version.encode(), b"")

    assert adapter.inspect_target(target_roots, Runner()).supported
    assert list(tmp_path.iterdir()) == []
    assert all(not home.exists() for home in homes)


@pytest.mark.parametrize("reason", ["aborted", "error", "pending", "future", "", None])
@pytest.mark.parametrize("error_message", [None, ""])
def test_pi_rejects_incomplete_and_unknown_assistant_states(tmp_path, reason, error_message):
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": "partial answer"}],
        "stopReason": reason,
    }
    if error_message is not None:
        message["errorMessage"] = error_message
    rows = pi_records(tmp_path, [{"role": "user", "content": "question"}, message])
    with pytest.raises(PreservationError, match="Pi assistant stopReason"):
        read_pi(native.jsonl(rows), Identity("pi", SESSION_ID))


@pytest.mark.parametrize(("adapter", "profile"), ADAPTERS)
@pytest.mark.parametrize("with_text", [True, False])
def test_assistant_images_fail_before_staging(adapter, profile, tmp_path, with_text):
    original = shared()
    content = (Text("answer"), AttachmentRef("img")) if with_text else (AttachmentRef("img"),)
    history = (replace(original.history[0], role="assistant", content=content, parent_id=None),)
    original = replace(original, history=history, context=history)
    with pytest.raises(PreservationError, match="assistant images are unsupported"):
        stage_for(adapter, profile, tmp_path, original)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("reader", "builder", "agent"),
    [(read_pi, pi_records, "pi"), (read_claude, claude_records, "claude-code")],
)
def test_source_assistant_images_fail_explicitly(reader, builder, agent, tmp_path):
    image = (
        {"type": "image", "data": "eA==", "mimeType": "image/png"}
        if agent == "pi"
        else {
            "type": "image",
            "source": {"type": "base64", "data": "eA==", "media_type": "image/png"},
        }
    )
    rows = builder(tmp_path, [{"role": "assistant", "content": [image]}])
    with pytest.raises(PreservationError, match="assistant images are unsupported"):
        reader(native.jsonl(rows), Identity(agent, SESSION_ID))


@pytest.mark.parametrize("interruption_role", ["user", "assistant"])
def test_pi_rejects_tool_exchange_interruption_in_writer_and_reader(tmp_path, interruption_role):
    original = shared()
    interruption = replace(
        original.history[-1],
        id="interrupt",
        role=interruption_role,
        content=(Text("interruption"),),
    )
    history = native.linked([*original.history[:2], interruption, *original.history[2:]])
    original = replace(original, history=history, context=history)
    with pytest.raises(PreservationError, match=r"tool results must follow.*contiguously"):
        stage_for(PiAdapter(), PI_PROFILE, tmp_path, original)
    rows = pi_records(
        tmp_path,
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking"},
                    {"type": "toolCall", "id": "a", "name": "read", "arguments": {}},
                ],
            },
            {"role": interruption_role, "content": [{"type": "text", "text": "interruption"}]},
            {
                "role": "toolResult",
                "toolCallId": "a",
                "toolName": "read",
                "content": [{"type": "text", "text": "result"}],
                "isError": False,
            },
        ],
    )
    with pytest.raises(PreservationError, match=r"tool results must follow.*contiguously"):
        read_pi(native.jsonl(rows), Identity("pi", SESSION_ID))


def test_pi_supports_prose_with_calls_and_contiguous_multiple_results(tmp_path):
    rows = pi_records(
        tmp_path,
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking both"},
                    {"type": "toolCall", "id": "a", "name": "read", "arguments": {}},
                    {"type": "toolCall", "id": "b", "name": "read", "arguments": {}},
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "toolResult",
                "toolCallId": "a",
                "toolName": "read",
                "content": [{"type": "text", "text": "first"}],
                "isError": False,
            },
            {
                "role": "toolResult",
                "toolCallId": "b",
                "toolName": "read",
                "content": [{"type": "text", "text": "second"}],
                "isError": True,
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
                "stopReason": "length",
            },
        ],
    )
    source = replace(read_pi(native.jsonl(rows), Identity("pi", SESSION_ID)), title="Ordered tools")
    stage = stage_for(PiAdapter(), PI_PROFILE, tmp_path, source)
    compare(source, PiAdapter().read_stage(stage))
    assert source.history[0].content[0] == Text("Checking both")
    assert [message.content[0].call_id for message in source.history[1:3]] == ["a", "b"]


@pytest.mark.parametrize(("adapter", "profile"), ADAPTERS)
@pytest.mark.parametrize("value", ["2020-01-02T03:04:05.000Z", "1577934245000"])
def test_native_historical_timestamps_survive_import(adapter, profile, tmp_path, value):
    original = shared()
    historical = HistoricalTimestamp(
        value, Provenance("native", Identity("pi", "old"), "old timestamp")
    )
    history = tuple(replace(message, timestamp=historical) for message in original.history)
    original = replace(original, history=history, context=history)
    before = datetime.now(UTC)
    stage = stage_for(adapter, profile, tmp_path, original)
    after = datetime.now(UTC)
    rows = native.records(stage.files[0].data)
    native_messages = [row for row in rows if "message" in row]
    expected = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert all(
        datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) == expected
        for row in native_messages
    )
    if profile.agent == "pi":
        assert all(row["message"]["timestamp"] == 1577934245000 for row in native_messages)
    created = datetime.fromisoformat(rows[0]["timestamp"].replace("Z", "+00:00"))
    assert before.timestamp() - 0.001 <= created.timestamp() <= after.timestamp()
    compare(original, adapter.read_stage(stage), adapter.normalizations(profile))


@pytest.mark.parametrize(("adapter", "profile"), ADAPTERS)
def test_unrepresentable_historical_time_is_not_silently_redated(adapter, profile, tmp_path):
    original = shared()
    historical = HistoricalTimestamp("yesterday afternoon", original.history[0].provenance)
    history = tuple(replace(message, timestamp=historical) for message in original.history)
    original = replace(original, history=history, context=history)
    with pytest.raises(PreservationError, match="Cannot preserve native historical timestamp"):
        stage_for(adapter, profile, tmp_path, original)
