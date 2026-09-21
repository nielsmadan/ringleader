from dataclasses import replace

import pytest
from ringleader.portability import compact, dumps, loads, validate
from ringleader.portability.model import Instruction, Text, ToolCall, ToolOmission, ToolResult

from .fixtures import checkpoint_conversation, conversation, message


def test_compact_preserves_prose_instructions_and_complete_omission_markers():
    original = conversation()
    result = compact(original)
    validate(result.conversation)
    assert dumps(result.conversation) == dumps(compact(original).conversation)
    assert result.report.omitted_calls == 2
    assert result.report.omitted_results == 2
    assert result.report.omitted_exchanges == 1
    assert result.report.full_tokens_approx > 0
    assert result.report.compact_tokens_approx > 0
    assert result.conversation.history[2].content == (
        Text("Checking both."),
        ToolOmission(("read", "shell"), 2, 2),
    )
    assert result.conversation.history[-1].content == (Text("One succeeded."),)
    assert result.conversation.history[0].content == original.history[0].content
    assert result.conversation.attachments == original.attachments


def test_checkpoint_keeps_summary_retained_tail_and_historical_instruction_authority():
    result = compact(checkpoint_conversation())
    validate(result.conversation)
    assert [item.id for item in result.conversation.history] == [
        "instructions",
        "summary",
        "request",
        "calls",
        "answer",
    ]
    assert [item.id for item in result.conversation.context] == [
        "summary",
        "request",
        "calls",
        "answer",
    ]
    assert result.conversation.checkpoints[0].summary[0].content == (
        Text("Earlier work was summarized."),
    )
    instruction = result.conversation.history[0].content[0]
    assert isinstance(instruction, Instruction)
    assert instruction.authority == "user"


def test_checkpoint_discards_only_history_outside_active_context_and_instructions():
    original = checkpoint_conversation()
    checkpoint = replace(original.checkpoints[0], retained_message_ids=())
    summary = checkpoint.summary[0]
    original = replace(
        original,
        checkpoints=(checkpoint,),
        context=(summary, replace(original.history[-1], parent_id="summary")),
    )
    result = compact(original)
    assert result.report.checkpoint_discarded_messages == 3
    assert [item.id for item in result.conversation.history] == [
        "instructions",
        "summary",
        "answer",
    ]
    assert result.conversation.attachments == ()


def test_prose_between_call_and_result_keeps_exchange_intact():
    history = (
        message(
            "call",
            "assistant",
            ToolCall("a", "read", "{}", "json"),
            Text("Explanation between call and result."),
        ),
        message("result", "tool", ToolResult("a", (Text("value"),)), parent="call"),
    )
    original = replace(conversation(), history=history, context=history, attachments=())
    result = compact(original)
    assert result.conversation.history == history
    assert result.report.omitted_calls == 0
    assert result.report.omitted_results == 0


def test_multiple_tool_runs_have_separate_markers():
    original = conversation()
    history = (
        *original.history,
        message("call2", "assistant", ToolCall("c", "third", "{}", "json"), parent="answer"),
        message("result2", "tool", ToolResult("c", (Text("done"),)), parent="call2"),
    )
    result = compact(replace(original, history=history, context=history))
    assert result.report.omitted_exchanges == 2
    assert result.report.omitted_calls == 3
    assert result.conversation.history[-1].content == (ToolOmission(("third",), 1, 1),)


def test_instruction_between_retained_messages_keeps_historical_order():
    original = checkpoint_conversation()
    instruction = replace(original.history[0], id="instruction-between", parent_id="request")
    history = (
        original.history[1],
        instruction,
        replace(original.history[2], parent_id="instruction-between"),
        *original.history[3:],
    )
    history = (replace(history[0], parent_id=None), *history[1:])
    original = replace(original, history=history)
    result = compact(original)
    assert [item.id for item in result.conversation.history] == [
        "summary",
        "request",
        "instruction-between",
        "calls",
        "answer",
    ]
    assert [item.id for item in result.conversation.context] == [
        "summary",
        "request",
        "calls",
        "answer",
    ]
    validate(result.conversation)


def test_historical_instruction_between_retained_call_and_result_blocks_omission():
    from ringleader.portability.model import Checkpoint

    original = conversation()
    call = message("call", "assistant", ToolCall("tool", "read", "{}", "json"))
    instruction = replace(original.history[0], id="instruction-between", parent_id="call")
    result = message(
        "result", "tool", ToolResult("tool", (Text("done"),)), parent="instruction-between"
    )
    summary = message("summary", "user", Text("Summary"))
    checkpoint = Checkpoint("checkpoint", "result", (summary,), ("call", "result"))
    context = (summary, replace(call, parent_id="summary"), replace(result, parent_id="call"))
    original = replace(
        original, history=(call, instruction, result), context=context, checkpoints=(checkpoint,)
    )
    validate(original)
    transformed = compact(original)
    validate(transformed.conversation)
    assert [item.id for item in transformed.conversation.history] == [
        "summary",
        "call",
        "instruction-between",
        "result",
    ]
    assert transformed.conversation.context == context
    assert transformed.report.omitted_calls == 0
    assert transformed.report.omitted_results == 0
    assert loads(dumps(transformed.conversation)) == transformed.conversation


def test_compact_preserves_owned_rewritten_checkpoint_context():
    original = checkpoint_conversation()
    latest = original.checkpoints[0]
    retained = message("request", "user", Text("Rewritten model input"))
    embedded = message(
        "new-retained-id", "assistant", Text("Embedded retained output"), parent="request"
    )
    checkpoint = replace(latest, retained_message_ids=(), retained_context=(retained, embedded))
    context = (
        *latest.summary,
        replace(retained, parent_id="summary"),
        embedded,
        replace(original.history[-1], parent_id="new-retained-id"),
    )
    original = replace(original, checkpoints=(checkpoint,), context=context)
    transformed = compact(original).conversation
    validate(transformed)
    assert transformed.context == context
    assert (
        next(item for item in transformed.history if item.id == "request").content
        == original.history[1].content
    )
    assert transformed.checkpoints[0].retained_context[1].id == "new-retained-id"
    assert loads(dumps(transformed)) == transformed


@pytest.mark.parametrize("retained_id", ["call", "result"])
def test_owned_rewritten_tool_member_retains_its_historical_partner(retained_id):
    from ringleader.portability.model import Checkpoint

    call = message("call", "assistant", ToolCall("tool", "read", "{}", "json"))
    result = message("result", "tool", ToolResult("tool", (Text("done"),)), parent="call")
    summary = message("summary", "user", Text("Summary"))
    retained = message(retained_id, "assistant", Text("Rewritten active context"))
    checkpoint = Checkpoint("checkpoint", "result", (summary,), retained_context=(retained,))
    context = (summary, replace(retained, parent_id="summary"))
    original = replace(
        conversation(), history=(call, result), context=context, checkpoints=(checkpoint,)
    )
    validate(original)
    transformed = compact(original)
    validate(transformed.conversation)
    assert transformed.conversation.context == context
    assert transformed.conversation.history[-1].content == (ToolOmission(("read",), 1, 1),)
    assert transformed.report.omitted_calls == 1
    assert transformed.report.omitted_results == 1
    assert transformed.report.checkpoint_discarded_messages == 0
    assert loads(dumps(transformed.conversation)) == transformed.conversation


def test_historical_partner_selection_closes_over_grouped_tool_results():
    from ringleader.portability.model import Checkpoint

    first = message("first-call", "assistant", ToolCall("a", "read", "{}", "json"))
    second = message(
        "second-call", "assistant", ToolCall("b", "shell", "pwd", "freeform"), parent="first-call"
    )
    results = message(
        "results",
        "tool",
        ToolResult("a", (Text("contents"),)),
        ToolResult("b", (Text("directory"),)),
        parent="second-call",
    )
    summary = message("summary", "user", Text("Summary"))
    rewritten = message("first-call", "assistant", Text("Rewritten active context"))
    checkpoint = Checkpoint("checkpoint", "results", (summary,), retained_context=(rewritten,))
    context = (summary, replace(rewritten, parent_id="summary"))
    original = replace(
        conversation(), history=(first, second, results), context=context, checkpoints=(checkpoint,)
    )
    validate(original)
    transformed = compact(original)
    validate(transformed.conversation)
    assert transformed.conversation.context == context
    assert transformed.conversation.history[-1].content == (ToolOmission(("read", "shell"), 2, 2),)
    assert transformed.report.omitted_calls == 2
    assert transformed.report.omitted_results == 2
