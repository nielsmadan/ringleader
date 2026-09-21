import json
from dataclasses import replace

import pytest
from ringleader.portability import PreservationError, compare, dumps, loads, validate
from ringleader.portability.compare import Normalization
from ringleader.portability.model import Instruction, Text, ToolCall, ToolResult

from .fixtures import checkpoint_conversation, conversation, message, provenance


def test_owned_attachment_and_versioned_roundtrip():
    original = conversation()
    parsed = loads(dumps(original))
    assert parsed == original
    assert parsed.attachments[0].data == b"fixture-image"
    assert len(parsed.attachments[0].digest) == 64
    compare(original, parsed)
    compare(checkpoint_conversation(), loads(dumps(checkpoint_conversation())))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(schema_version=9),
        lambda value: value.update(opaque_native_blob="this cannot preserve conversation"),
        lambda value: value["history"][0]["content"][0].update(type="unknown"),
        lambda value: value["history"][0].update(role="alien"),
        lambda value: value["attachments"][0].update(data="invalid base64@"),
    ],
)
def test_unknown_serialized_shapes_fail(mutation):
    value = json.loads(dumps(conversation()))
    mutation(value)
    with pytest.raises(PreservationError):
        loads(json.dumps(value).encode())


@pytest.mark.parametrize(
    "messages",
    [
        (message("one", "assistant", ToolCall("a", "read", "{}", "json")),),
        (message("one", "tool", ToolResult("a", (Text("x"),))),),
        (
            message(
                "one",
                "assistant",
                ToolCall("a", "read", "{}", "json"),
                ToolCall("a", "read", "{}", "json"),
            ),
        ),
        (message("one", "assistant", ToolCall("a", "read", "invalid", "json")),),
        (message("one", "assistant", ToolCall("a", "read", "NaN", "json")),),
        (message("one", "user", Text("x"), parent="one"),),
        (message("one", "user", Text("x")), message("one", "assistant", Text("y"))),
        (
            message("one", "user", Text("x")),
            message("two", "assistant", Text("y"), parent="missing"),
        ),
        (message("one", "user", object()),),
    ],
)
def test_invalid_graphs_and_content_fail(messages):
    with pytest.raises(PreservationError):
        validate(replace(conversation(), history=messages, context=messages))


def test_branch_fork_is_not_a_selected_linear_history():
    messages = (
        message("one", "user", Text("x")),
        message("two", "assistant", Text("y"), parent="one"),
        message("three", "assistant", Text("z"), parent="one"),
    )
    with pytest.raises(PreservationError, match="parent"):
        validate(replace(conversation(), history=messages, context=messages))


def test_bootstrap_lookalike_is_preserved_and_harness_needs_evidence():
    original = conversation()
    assert loads(dumps(original)).history[1].content[0].text.startswith("# AGENTS.md")
    bad = replace(original.history[0], provenance=provenance(origin="harness"))
    with pytest.raises(PreservationError, match="structural evidence"):
        validate(replace(original, history=(bad,)))


def test_missing_attachment_and_invalid_checkpoint_tail_fail():
    original = checkpoint_conversation()
    with pytest.raises(PreservationError, match="attachment"):
        validate(replace(original, attachments=()))
    checkpoint = replace(original.checkpoints[0], retained_message_ids=("results", "request"))
    with pytest.raises(PreservationError, match="retained"):
        validate(replace(original, checkpoints=(checkpoint,)))
    with pytest.raises(PreservationError, match="Active context"):
        validate(replace(original, context=original.history))


def test_compare_checks_context_instructions_provenance_and_attachments():
    original = conversation()
    instruction = original.history[0].content[0]
    assert isinstance(instruction, Instruction)
    variants = [
        replace(original, context=()),
        replace(original, branch_id="different branch"),
        replace(original, attachments=(replace(original.attachments[0], data=b"changed"),)),
        replace(
            original,
            history=(
                replace(original.history[0], content=(replace(instruction, authority="system"),)),
                *original.history[1:],
            ),
        ),
        replace(
            original,
            history=(
                replace(original.history[0], provenance=provenance("changed")),
                *original.history[1:],
            ),
        ),
    ]
    for variant in variants:
        with pytest.raises(PreservationError, match="Preservation mismatch"):
            compare(original, variant)


def test_normalization_requires_explicit_documented_opt_in():
    original = conversation()
    first = original.history[2]
    altered = replace(first, content=(Text("Checking "), Text("both."), *first.content[1:]))
    actual = replace(original, history=(*original.history[:2], altered, *original.history[3:]))
    with pytest.raises(PreservationError, match="history"):
        compare(original, actual)
    compare(
        original,
        actual,
        (Normalization("adjacent-text", "fixture-v1", "Native reader splits text blocks"),),
    )


def test_remapped_call_ids_preserve_pairs_but_swapping_results_fails():
    original = conversation()
    history = tuple(
        replace(
            item,
            content=tuple(
                replace(block, call_id={"a": "new-a", "b": "new-b"}[block.call_id])
                if isinstance(block, ToolCall | ToolResult)
                else block
                for block in item.content
            ),
        )
        for item in original.history
    )
    compare(original, replace(original, history=history, context=history))
    swapped = replace(
        history[3],
        content=tuple(
            replace(block, call_id="new-b" if block.call_id == "new-a" else "new-a")
            for block in history[3].content
        ),
    )
    with pytest.raises(PreservationError, match="history"):
        compare(original, replace(original, history=(*history[:3], swapped, *history[4:])))


def test_duplicate_keys_fail_in_canonical_json_and_tool_arguments():
    with pytest.raises(PreservationError, match="Duplicate JSON key"):
        loads(b'{"type":"Conversation","type":"Conversation"}')
    history = (
        message("call", "assistant", ToolCall("a", "read", '{"path":"a","path":"b"}', "json")),
        message("result", "tool", ToolResult("a", (Text("done"),)), parent="call"),
    )
    with pytest.raises(PreservationError, match="Invalid JSON tool arguments"):
        validate(replace(conversation(), history=history, context=history))


def test_json_and_freeform_arguments_are_distinct_semantics():
    original = conversation()
    calls = original.history[2]
    blocks = tuple(
        replace(item, argument_format="freeform") if isinstance(item, ToolCall) else item
        for item in calls.content
    )
    changed = replace(
        original,
        history=(*original.history[:2], replace(calls, content=blocks), *original.history[3:]),
    )
    with pytest.raises(PreservationError, match="history"):
        compare(original, changed)


def test_owned_checkpoint_context_can_differ_from_visible_history():
    original = checkpoint_conversation()
    summary = original.checkpoints[0].summary
    rewritten = replace(
        original.history[1], content=(Text("Rewritten retained model input"),), parent_id=None
    )
    embedded = message(
        "embedded-only",
        "assistant",
        Text("Retained context absent from the visible transcript"),
        parent="request",
    )
    checkpoint = replace(
        original.checkpoints[0], retained_message_ids=(), retained_context=(rewritten, embedded)
    )
    context = (
        *summary,
        replace(rewritten, parent_id=summary[-1].id),
        embedded,
        replace(original.history[-1], parent_id="embedded-only"),
    )
    result = replace(original, checkpoints=(checkpoint,), context=context)
    validate(result)
    assert result.history == original.history
    assert result.checkpoints[0].retained_context[0].content == (
        Text("Rewritten retained model input"),
    )
    assert loads(dumps(result)) == result
    with pytest.raises(PreservationError, match="checkpoints"):
        compare(original, result)


def test_owned_checkpoint_context_checks_pairs_order_and_exclusive_representation():
    original = checkpoint_conversation()
    latest = original.checkpoints[0]
    call = message("embedded-call", "assistant", ToolCall("embedded-tool", "read", "{}", "json"))
    result = message(
        "embedded-result",
        "tool",
        ToolResult("embedded-tool", (Text("done"),)),
        parent="embedded-call",
    )
    checkpoints = (
        replace(latest, retained_context=()),
        replace(latest, retained_message_ids=(), retained_context=(call,)),
        replace(latest, retained_message_ids=(), retained_context=(result, call)),
    )
    for checkpoint in checkpoints:
        with pytest.raises(PreservationError):
            validate(replace(original, checkpoints=(checkpoint,)))


def test_historical_timestamps_roundtrip_and_are_compared_separately():
    from ringleader.portability.model import HistoricalTimestamp

    original = conversation()
    timestamp = HistoricalTimestamp("2026-09-20T11:12:13.456Z", provenance("native.timestamp"))
    history = (replace(original.history[0], timestamp=timestamp), *original.history[1:])
    original = replace(
        original,
        history=history,
        context=history,
        source_created_at=timestamp,
        source_updated_at=timestamp,
    )
    assert loads(dumps(original)) == original
    changed_time = replace(timestamp, value="2026-09-21T11:12:13.456Z")
    changed_source = replace(timestamp, provenance=provenance("other.timestamp"))
    variants = (
        replace(original, history=(replace(history[0], timestamp=changed_time), *history[1:])),
        replace(original, source_created_at=changed_time),
        replace(original, source_updated_at=changed_time),
        replace(original, source_created_at=changed_source),
    )
    for variant in variants:
        with pytest.raises(PreservationError, match="Preservation mismatch"):
            compare(original, variant)


@pytest.mark.parametrize(
    "variant",
    [
        lambda original: replace(original, schema_version=True),
        lambda original: replace(original, title="\ud800"),
        lambda original: replace(original, source=replace(original.source, native_id=42)),
        lambda original: replace(original, history=list(original.history)),
        lambda original: replace(
            original, attachments=(replace(original.attachments[0], data=bytearray(b"image")),)
        ),
        lambda original: replace(original, title=42),
        lambda original: replace(
            original, history=(replace(original.history[0], content=[Text("list")]),)
        ),
        lambda original: replace(
            original,
            history=(message("call", "assistant", ToolCall("call", "tool", 42, "freeform")),),
        ),
        lambda original: replace(
            original, history=(message("call", "assistant", ToolCall("call", 42, "{}", "json")),)
        ),
    ],
)
def test_native_reader_objects_cannot_bypass_strict_shapes(variant):
    malformed = variant(conversation())
    for boundary in (validate, dumps):
        with pytest.raises(PreservationError):
            boundary(malformed)


def test_direct_omission_names_and_counts_must_match_declared_types():
    from ringleader.portability.model import ToolOmission

    for omission in (
        ToolOmission(("read",), True, True),
        ToolOmission((42,), 1, 1),
        ToolOmission(["read"], 1, 1),
    ):
        malformed = replace(conversation(), history=(message("omission", "assistant", omission),))
        with pytest.raises(PreservationError):
            validate(malformed)
        with pytest.raises(PreservationError):
            dumps(malformed)
