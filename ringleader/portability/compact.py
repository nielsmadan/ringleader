from __future__ import annotations

from dataclasses import dataclass, replace

from .model import (
    AttachmentRef,
    Checkpoint,
    Content,
    Conversation,
    Instruction,
    Message,
    Text,
    ToolCall,
    ToolOmission,
    ToolResult,
    validate,
)


@dataclass(frozen=True)
class CompactReport:
    full_tokens_approx: int
    compact_tokens_approx: int
    omitted_calls: int
    omitted_results: int
    omitted_exchanges: int
    checkpoint_discarded_messages: int


@dataclass(frozen=True)
class CompactResult:
    conversation: Conversation
    report: CompactReport


def _size(messages: tuple[Message, ...]) -> int:
    size = 0
    for message in messages:
        for item in message.content:
            if isinstance(item, Text | Instruction):
                size += len(item.text.encode("utf-8"))
            elif isinstance(item, ToolCall):
                size += len((item.name + item.arguments).encode("utf-8"))
            elif isinstance(item, ToolResult):
                size += sum(
                    len(part.text.encode("utf-8")) for part in item.output if isinstance(part, Text)
                )
            elif isinstance(item, ToolOmission):
                size += len(
                    f"Omitted {item.calls} tool calls and {item.results} results: {', '.join(item.names)}".encode()
                )
    return (size + 3) // 4


def _reparent(messages: list[Message]) -> tuple[Message, ...]:
    return tuple(
        replace(message, parent_id=messages[index - 1].id if index else None)
        for index, message in enumerate(messages)
    )


def _omit_tools(
    messages: tuple[Message, ...], blocked: set[str] | None = None
) -> tuple[tuple[Message, ...], int, int]:
    flattened = [(message, item) for message in messages for item in message.content]
    output: dict[str, list[Content]] = {message.id: [] for message in messages}
    index = calls = exchanges = 0
    while index < len(flattened):
        message, item = flattened[index]
        if not isinstance(item, ToolCall | ToolResult):
            output[message.id].append(item)
            index += 1
            continue
        start = index
        pending: set[str] = set()
        incomplete = False
        names = []
        while index < len(flattened) and isinstance(flattened[index][1], ToolCall | ToolResult):
            current = flattened[index][1]
            if isinstance(current, ToolCall):
                pending.add(current.call_id)
                names.append(current.name)
            elif isinstance(current, ToolResult):
                if current.call_id not in pending:
                    incomplete = True
                else:
                    pending.remove(current.call_id)
            index += 1
        run_calls = {
            block.call_id for _, block in flattened[start:index] if isinstance(block, ToolCall)
        }
        if pending or incomplete or run_calls & (blocked or set()):
            for original, block in flattened[start:index]:
                output[original.id].append(block)
            continue
        output[message.id].append(ToolOmission(tuple(names), len(names), len(names)))
        calls += len(names)
        exchanges += 1
    kept = [
        replace(message, content=tuple(output[message.id]))
        for message in messages
        if output[message.id] or not message.content
    ]
    return _reparent(kept), calls, exchanges


def _used_assets(messages: tuple[Message, ...]) -> set[str]:
    return {
        part.attachment_id
        for message in messages
        for item in message.content
        for part in (item.output if isinstance(item, ToolResult) else (item,))
        if isinstance(part, AttachmentRef)
    }


def _paired_history_ids(history: tuple[Message, ...], selected: set[str]) -> set[str]:
    calls: dict[str, str] = {}
    partners: dict[str, set[str]] = {}
    for message in history:
        for item in message.content:
            if isinstance(item, ToolCall):
                calls[item.call_id] = message.id
            elif isinstance(item, ToolResult):
                caller = calls[item.call_id]
                partners.setdefault(caller, set()).add(message.id)
                partners.setdefault(message.id, set()).add(caller)
    retained = set(selected)
    pending = list(selected)
    while pending:
        for partner in partners.get(pending.pop(), ()):
            if partner not in retained:
                retained.add(partner)
                pending.append(partner)
    return retained


def _checkpoint_history(conversation: Conversation) -> tuple[tuple[Message, ...], int]:
    summary = conversation.checkpoints[-1].summary
    tail_ids = _paired_history_ids(
        conversation.history, {message.id for message in conversation.context[len(summary) :]}
    )
    selected: list[Message] = []
    inserted = False
    discarded = 0
    for message in conversation.history:
        if message.id in tail_ids:
            if not inserted:
                selected.extend(summary)
                inserted = True
            selected.append(message)
        else:
            instructions = tuple(item for item in message.content if isinstance(item, Instruction))
            if instructions:
                selected.append(replace(message, content=instructions))
            else:
                discarded += 1
    if not inserted:
        selected.extend(summary)
    return _reparent(selected), discarded


def _tool_ids(messages: tuple[Message, ...]) -> set[str]:
    return {
        item.call_id
        for message in messages
        for item in message.content
        if isinstance(item, ToolCall)
    }


def _compact_context(conversation: Conversation, blocked: set[str]) -> tuple[Message, ...]:
    if not conversation.checkpoints:
        return _omit_tools(conversation.context, blocked)[0]
    summary = conversation.checkpoints[-1].summary
    compact_summary = _omit_tools(summary, blocked)[0]
    compact_tail = _omit_tools(conversation.context[len(summary) :], blocked)[0]
    return _reparent([*compact_summary, *compact_tail])


def _coordinated_omissions(
    selected: tuple[Message, ...], conversation: Conversation
) -> tuple[tuple[Message, ...], tuple[Message, ...], int, int, set[str]]:
    shared = _tool_ids(selected) & _tool_ids(conversation.context)
    blocked: set[str] = set()
    while True:
        history, calls, exchanges = _omit_tools(selected, blocked)
        context = _compact_context(conversation, blocked)
        preserved = (_tool_ids(history) | _tool_ids(context)) & shared
        if preserved <= blocked:
            return history, context, calls, exchanges, blocked
        blocked |= preserved


def compact(conversation: Conversation) -> CompactResult:
    validate(conversation)
    if conversation.checkpoints:
        selected, discarded = _checkpoint_history(conversation)
    else:
        selected = conversation.history
        discarded = 0
    history, context, calls, exchanges, blocked = _coordinated_omissions(selected, conversation)
    used = _used_assets(history) | _used_assets(context)
    checkpoints: tuple[Checkpoint, ...] = ()
    if conversation.checkpoints:
        latest = conversation.checkpoints[-1]
        summary, _, _ = _omit_tools(latest.summary, blocked)
        checkpoints = (
            replace(
                latest,
                after_message_id=history[-1].id,
                summary=summary,
                retained_message_ids=(),
                retained_context=_reparent(list(context[len(summary) :])),
            ),
        )
    result = replace(
        conversation,
        history=history,
        context=context,
        checkpoints=checkpoints,
        attachments=tuple(item for item in conversation.attachments if item.id in used),
    )
    validate(result)
    return CompactResult(
        result,
        CompactReport(
            _size(conversation.history), _size(history), calls, calls, exchanges, discarded
        ),
    )
