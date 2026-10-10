from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Literal

from .model import (
    AttachmentRef,
    Content,
    Conversation,
    Instruction,
    Message,
    Opaque,
    PreservationError,
    Text,
    ToolCall,
    ToolOmission,
    ToolResult,
    validate,
)

KINDS = (
    "adjacent-text",
    "call-ordered-results",
    "flattened-checkpoint",
    "joined-tool-output",
    "json-argument-whitespace",
    "model-view-history",
)
TOOL_TEXT_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class Normalization:
    kind: Literal[
        "adjacent-text",
        "call-ordered-results",
        "flattened-checkpoint",
        "joined-tool-output",
        "json-argument-whitespace",
        "model-view-history",
    ]
    profile: str
    reason: str
    hides: bool = False

    def __post_init__(self) -> None:
        if (
            self.kind not in KINDS
            or not self.profile
            or not self.reason
            or (self.hides and self.kind != "model-view-history")
        ):
            raise PreservationError(
                "Normalization requires a documented supported profile and reason"
            )


class CheckpointShape(PreservationError):
    pass


def flattened_checkpoint(profile: str) -> Normalization:
    return Normalization(
        "flattened-checkpoint",
        profile,
        "The target cannot express this checkpoint natively; its model view became ordinary history",
    )


def _attachment_ids(messages: tuple[Message, ...]) -> set[str]:
    return {
        part.attachment_id
        for message in messages
        for item in message.content
        for part in (item.output if isinstance(item, ToolResult) else (item,))
        if isinstance(part, AttachmentRef)
    }


def flattened(conversation: Conversation) -> Conversation:
    if not conversation.checkpoints:
        return conversation
    used = _attachment_ids(conversation.context)
    return model_view(
        replace(
            conversation,
            checkpoints=(),
            attachments=tuple(item for item in conversation.attachments if item.id in used),
        ),
        hides=True,
    )


def joined_tool_output(result: ToolResult) -> tuple[Text | AttachmentRef | Opaque, ...]:
    text = TOOL_TEXT_SEPARATOR.join(item.text for item in result.output if isinstance(item, Text))
    return (Text(text), *(item for item in result.output if not isinstance(item, Text)))


def model_view(conversation: Conversation, *, hides: bool) -> Conversation:
    if conversation.checkpoints:
        return conversation
    context = (
        conversation.context
        if hides
        else tuple(replace(message, hidden=False) for message in conversation.context)
    )
    visible = [message for message in context if not message.hidden]
    history = tuple(
        replace(message, parent_id=visible[index - 1].id if index else None)
        for index, message in enumerate(visible)
    )
    return replace(conversation, history=history, context=context)


def _content(
    value: Content, assets: dict[str, tuple[str, str, str | None]], kinds: set[str]
) -> Any:
    if isinstance(value, AttachmentRef):
        return ("attachment", *assets[value.attachment_id])
    if isinstance(value, ToolCall):
        arguments = value.arguments
        if value.argument_format == "json" and "json-argument-whitespace" in kinds:
            arguments = json.dumps(json.loads(arguments), sort_keys=True, separators=(",", ":"))
        return ("call", value.name, value.argument_format, arguments)
    if isinstance(value, ToolResult):
        output = joined_tool_output(value) if "joined-tool-output" in kinds else value.output
        return (
            "result",
            value.is_error,
            tuple(_content(item, assets, kinds) for item in output),
        )
    if isinstance(value, Instruction):
        return ("instruction", value.text, value.authority, value.provenance)
    if isinstance(value, ToolOmission):
        return ("omission", value.names, value.calls, value.results)
    if isinstance(value, Opaque):
        return ("opaque", value.agent, value.kind, value.payload, value.fallback)
    return ("text", value.text)


def _messages(
    messages: tuple[Message, ...], conversation: Conversation, kinds: set[str]
) -> tuple[Any, ...]:
    assets = {
        item.id: (item.digest, item.media_type, item.name) for item in conversation.attachments
    }
    calls: dict[str, int] = {}
    result = []
    for message in messages:
        content: list[Any] = []
        for item in message.content:
            value = _content(item, assets, kinds)
            if isinstance(item, ToolCall):
                calls[item.call_id] = len(calls)
                value = (*value, calls[item.call_id])
            elif isinstance(item, ToolResult):
                value = (*value, calls[item.call_id])
            if (
                isinstance(item, Text)
                and "adjacent-text" in kinds
                and content
                and content[-1][0] == "text"
            ):
                content[-1] = ("text", content[-1][1] + item.text)
            else:
                content.append(value)
        result.append((message.role, message.provenance, message.timestamp, tuple(content)))
    return _call_ordered(result) if "call-ordered-results" in kinds else tuple(result)


def _results_only(message: Any) -> bool:
    return bool(message[3]) and all(item[0] == "result" for item in message[3])


def _call_order(message: Any) -> int:
    return min(int(item[-1]) for item in message[3])


def _call_ordered(messages: list[Any]) -> tuple[Any, ...]:
    result: list[Any] = []
    run: list[Any] = []
    for message in [*messages, None]:
        if message is not None and _results_only(message):
            run.append(message)
            continue
        result.extend(sorted(run, key=_call_order))
        run = []
        if message is not None:
            result.append(message)
    return tuple(result)


def preservation_projection(
    conversation: Conversation, normalizations: tuple[Normalization, ...] = ()
) -> dict[str, Any]:
    validate(conversation)
    kinds: set[str] = {item.kind for item in normalizations}
    views = [item for item in normalizations if item.kind == "model-view-history"]
    if views:
        conversation = model_view(conversation, hides=all(item.hides for item in views))
    positions = {message.id: index for index, message in enumerate(conversation.history)}
    return {
        "branch": conversation.branch_id,
        "history": _messages(conversation.history, conversation, kinds),
        "context": _messages(conversation.context, conversation, kinds),
        "checkpoints": tuple(
            (
                positions[item.after_message_id],
                _messages(item.summary, conversation, kinds),
                tuple(positions[retained] for retained in item.retained_message_ids),
                _messages(item.retained_context, conversation, kinds)
                if item.retained_context is not None
                else None,
                _messages(item.following_context, conversation, kinds)
                if item.following_context is not None
                else None,
            )
            for item in conversation.checkpoints
        ),
        "attachments": sorted(
            (item.digest, item.media_type, item.name or "") for item in conversation.attachments
        ),
        "title": conversation.title,
        "source_created_at": conversation.source_created_at,
        "source_updated_at": conversation.source_updated_at,
    }


def compare(
    expected: Conversation, actual: Conversation, normalizations: tuple[Normalization, ...] = ()
) -> None:
    if (
        expected.checkpoints
        and not actual.checkpoints
        and any(item.kind == "model-view-history" for item in normalizations)
    ):
        expected = flattened(expected)
    left = preservation_projection(expected, normalizations)
    right = preservation_projection(actual, normalizations)
    differences = [key for key in left if left[key] != right[key]]
    if differences:
        raise PreservationError(f"Preservation mismatch: {', '.join(differences)}")
