from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import accumulate

from .model import (
    AttachmentRef,
    Checkpoint,
    Content,
    Conversation,
    Identity,
    Instruction,
    Message,
    Opaque,
    PreservationError,
    Provenance,
    Text,
    ToolCall,
    ToolOmission,
    ToolResult,
    validate,
)

PORTABLE_TRANSFORM_VERSION = 4
MAX_PORTABLE_TOKENS = 150_000


@dataclass(frozen=True)
class CompactReport:
    full_tokens_approx: int
    compact_tokens_approx: int
    omitted_calls: int
    omitted_exchanges: int
    checkpoint_discarded_messages: int


@dataclass(frozen=True)
class CompactResult:
    conversation: Conversation
    report: CompactReport


def _size(messages: tuple[Message, ...]) -> int:
    return _tokens(_bytes(messages))


def _tokens(size: int) -> int:
    return (size + 3) // 4


def _bytes(messages: tuple[Message, ...]) -> int:
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
                ) + sum(
                    len(part.payload.encode("utf-8"))
                    for part in item.output
                    if isinstance(part, Opaque)
                )
            elif isinstance(item, Opaque):
                size += len(item.payload.encode("utf-8"))
            elif isinstance(item, ToolOmission):
                size += len(
                    f"Omitted {item.calls} tool calls and {item.results} results: {', '.join(item.names)}".encode()
                )
    return size


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
    latest = conversation.checkpoints[-1]
    summary = tuple(
        message
        for message in latest.summary
        if not message.content or not all(isinstance(item, Opaque) for item in message.content)
    )
    boundary = next(
        index
        for index, message in enumerate(conversation.history)
        if message.id == latest.after_message_id
    )
    tail_ids = _paired_history_ids(
        conversation.history,
        {message.id for message in conversation.context[len(latest.summary) :]}
        | {message.id for message in conversation.history[boundary + 1 :]},
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
                following_context=None,
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
        CompactReport(_size(conversation.history), _size(history), calls, exchanges, discarded),
    )


def _portable_content(item: Content, target_agent: str) -> tuple[Content, ...]:
    if isinstance(item, Opaque):
        return (item,) if item.agent == target_agent else item.fallback
    if isinstance(item, ToolResult):
        output = tuple(
            part
            for original in item.output
            for part in (
                original.fallback
                if isinstance(original, Opaque) and original.agent != target_agent
                else (original,)
            )
        )
        return (replace(item, output=output),)
    return (item,)


def _portable_messages(messages: tuple[Message, ...], target_agent: str) -> tuple[Message, ...]:
    converted = [
        replace(
            message,
            content=tuple(
                part for item in message.content for part in _portable_content(item, target_agent)
            ),
        )
        for message in messages
    ]
    kept = [
        message
        for message, original in zip(converted, messages, strict=True)
        if message.content or not original.content
    ]
    return tuple(
        replace(
            message,
            parent_id=kept[index - 1].id if index and message.parent_id is not None else None,
        )
        for index, message in enumerate(kept)
    )


def _portable_optional(
    messages: tuple[Message, ...] | None, target_agent: str
) -> tuple[Message, ...] | None:
    return None if messages is None else _portable_messages(messages, target_agent)


def _portable_checkpoint(checkpoint: Checkpoint, target_agent: str) -> Checkpoint:
    return replace(
        checkpoint,
        summary=_portable_messages(checkpoint.summary, target_agent),
        retained_context=_portable_optional(checkpoint.retained_context, target_agent),
        following_context=_portable_optional(checkpoint.following_context, target_agent),
        replaced_context=None,
    )


def _referenced(conversation: Conversation) -> set[str]:
    streams = [conversation.history, conversation.context]
    for checkpoint in conversation.checkpoints:
        streams.extend(
            (
                checkpoint.summary,
                checkpoint.retained_context or (),
                checkpoint.following_context or (),
            )
        )
    return set().union(*(_used_assets(stream) for stream in streams))


def _opaque_summary(checkpoint: Checkpoint, target_agent: str) -> bool:
    return any(
        isinstance(item, Opaque) and item.agent != target_agent and not item.fallback
        for message in checkpoint.summary
        for item in message.content
    )


def _result_ids(messages: tuple[Message, ...]) -> set[str]:
    return {
        item.call_id
        for message in messages
        for item in message.content
        if isinstance(item, ToolResult)
    }


def _tool_free(message: Message) -> bool:
    return not any(isinstance(item, ToolCall | ToolResult) for item in message.content)


def _equivalent(left: Message, right: Message) -> bool:
    return (left.role, left.content, left.hidden) == (right.role, right.content, right.hidden)


# Codex retains copies of its newest user messages, so each pairs with its latest equivalent.
def _duplicates(retained: tuple[Message, ...], replaced: tuple[Message, ...]) -> dict[str, str]:
    index_of = {message.id: index for index, message in enumerate(replaced)}
    exact = {message.id: index_of[message.id] for message in retained if message.id in index_of}
    matches = {key: key for key in exact}
    claimed = set(exact.values())
    floors = list(accumulate((exact.get(message.id, -1) for message in retained), max, initial=-1))
    position = len(replaced)
    for offset in range(len(retained) - 1, -1, -1):
        message = retained[offset]
        if message.id in exact:
            position = min(position, exact[message.id])
        elif _tool_free(message):
            found = next(
                (
                    index
                    for index in range(position - 1, floors[offset], -1)
                    if index not in claimed and _equivalent(replaced[index], message)
                ),
                None,
            )
            if found is not None:
                matches[message.id] = replaced[found].id
                claimed.add(found)
                position = found
    return matches


def _units(messages: tuple[Message, ...]) -> dict[str, int]:
    units: dict[str, int] = {}
    pending: set[str] = set()
    unit = -1
    for message in messages:
        if unit < 0 or (message.role == "user" and _tool_free(message) and not pending):
            unit += 1
        units[message.id] = unit
        for item in message.content:
            if isinstance(item, ToolCall):
                pending.add(item.call_id)
            elif isinstance(item, ToolResult):
                pending.discard(item.call_id)
    return units


def _omission_marker(checkpoint: Checkpoint, source: Identity, count: int) -> Message:
    key = f"{checkpoint.id}:omitted"
    noun = "message" if count == 1 else "messages"
    return Message(
        key,
        "user",
        (Text(f"[{count} earlier {noun} omitted to fit the converted context]"),),
        Provenance("harness", source, key, "oldest collapsed history dropped over token budget"),
    )


def _substituted_context(
    conversation: Conversation,
    checkpoint: Checkpoint,
    replaced: tuple[Message, ...],
    target_agent: str,
) -> tuple[Message, ...]:
    by_id = {message.id: message for message in conversation.history}
    retained = (
        checkpoint.retained_context
        if checkpoint.retained_context is not None
        else tuple(by_id[item] for item in checkpoint.retained_message_ids)
    )
    tail = conversation.context[len(checkpoint.summary) + len(retained) :]
    live = _tokens(_bytes(_portable_messages((*retained, *tail), target_agent)))
    if live > MAX_PORTABLE_TOKENS:
        raise PreservationError(f"converted context too large: ~{live} tokens")
    matches = _duplicates(retained, replaced)
    kept = tuple(message for message in retained if message.id not in matches)
    blocked = _result_ids((*kept, *tail))
    collapsed = _omit_tools(replaced, blocked)[0]
    unit_of = _units(replaced)
    count = max(unit_of.values(), default=-1) + 1
    visible = _portable_messages(collapsed, target_agent)
    visible_ids = {message.id for message in visible}
    unit_bytes = [0] * count
    unit_messages = [0] * count
    for message in visible:
        unit_bytes[unit_of[message.id]] += _bytes((message,))
        unit_messages[unit_of[message.id]] += 1
    restored_bytes = [0] * count
    restored_messages = [0] * count
    for message in retained:
        if message.id in matches:
            unit = unit_of[matches[message.id]]
            restored_bytes[unit] += _bytes(_portable_messages((message,), target_agent))
            restored_messages[unit] += matches[message.id] in visible_ids
    size = _bytes(_portable_messages((*kept, *tail), target_agent)) + sum(unit_bytes)
    omitted = 0
    for dropped in range(count + 1):
        marker = (_omission_marker(checkpoint, conversation.source, omitted),) if omitted else ()
        if _tokens(size + _bytes(marker)) <= MAX_PORTABLE_TOKENS:
            restored = tuple(
                message
                for message in retained
                if message.id not in matches or unit_of[matches[message.id]] < dropped
            )
            survivors = tuple(message for message in collapsed if unit_of[message.id] >= dropped)
            return _reparent([*marker, *survivors, *restored, *tail])
        if dropped < count:
            size += restored_bytes[dropped] - unit_bytes[dropped]
            omitted += unit_messages[dropped] - restored_messages[dropped]
    raise PreservationError(
        f"converted context too large: ~{_tokens(size + _bytes(marker))} tokens"
    )


def portable(conversation: Conversation, target_agent: str) -> Conversation:
    validate(conversation)
    opaque = [
        index
        for index, checkpoint in enumerate(conversation.checkpoints)
        if _opaque_summary(checkpoint, target_agent)
    ]
    checkpoints = conversation.checkpoints
    context = conversation.context
    if opaque:
        newest = opaque[-1]
        if newest == len(checkpoints) - 1:
            replaced = checkpoints[newest].replaced_context
            if replaced is None:
                raise PreservationError("opaque checkpoint has no portable history")
            context = _substituted_context(
                conversation, checkpoints[newest], replaced, target_agent
            )
        checkpoints = checkpoints[newest + 1 :]
    result = replace(
        conversation,
        history=_portable_messages(conversation.history, target_agent),
        context=_portable_messages(context, target_agent),
        checkpoints=tuple(_portable_checkpoint(item, target_agent) for item in checkpoints),
    )
    used = _referenced(result)
    result = replace(
        result, attachments=tuple(item for item in conversation.attachments if item.id in used)
    )
    validate(result)
    return result
