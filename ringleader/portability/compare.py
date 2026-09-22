from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from .model import (
    AttachmentRef,
    Content,
    Conversation,
    Instruction,
    Message,
    PreservationError,
    Text,
    ToolCall,
    ToolOmission,
    ToolResult,
    validate,
)


@dataclass(frozen=True)
class Normalization:
    kind: Literal["adjacent-text", "json-argument-whitespace"]
    profile: str
    reason: str

    def __post_init__(self) -> None:
        if (
            self.kind not in ("adjacent-text", "json-argument-whitespace")
            or not self.profile
            or not self.reason
        ):
            raise PreservationError(
                "Normalization requires a documented supported profile and reason"
            )


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
        return (
            "result",
            value.is_error,
            tuple(_content(item, assets, kinds) for item in value.output),
        )
    if isinstance(value, Instruction):
        return ("instruction", value.text, value.authority, value.provenance)
    if isinstance(value, ToolOmission):
        return ("omission", value.names, value.calls, value.results)
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
    return tuple(result)


def preservation_projection(
    conversation: Conversation, normalizations: tuple[Normalization, ...] = ()
) -> dict[str, Any]:
    validate(conversation)
    kinds: set[str] = {item.kind for item in normalizations}
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
    left = preservation_projection(expected, normalizations)
    right = preservation_projection(actual, normalizations)
    differences = [key for key in left if left[key] != right[key]]
    if differences:
        raise PreservationError(f"Preservation mismatch: {', '.join(differences)}")
