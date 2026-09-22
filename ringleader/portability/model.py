from __future__ import annotations

import hashlib
import json
import types
from dataclasses import dataclass, fields, is_dataclass, replace
from functools import cache
from typing import Any, Literal, Union, cast, get_args, get_origin, get_type_hints


class PreservationError(ValueError):
    pass


@dataclass(frozen=True)
class Identity:
    agent: str
    native_id: str


@dataclass(frozen=True)
class Provenance:
    origin: Literal["authored", "native", "harness"]
    source: Identity
    locator: str
    evidence: str = ""


@dataclass(frozen=True)
class HistoricalTimestamp:
    value: str
    provenance: Provenance


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Instruction:
    text: str
    authority: Literal["system", "developer", "user"]
    provenance: Provenance


@dataclass(frozen=True)
class AttachmentRef:
    attachment_id: str


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str
    argument_format: Literal["json", "freeform"]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    output: tuple[Text | AttachmentRef, ...]
    is_error: bool = False


@dataclass(frozen=True)
class ToolOmission:
    names: tuple[str, ...]
    calls: int
    results: int


Content = Text | Instruction | AttachmentRef | ToolCall | ToolResult | ToolOmission


@dataclass(frozen=True)
class Message:
    id: str
    role: Literal["user", "assistant", "system", "developer", "tool"]
    content: tuple[Content, ...]
    provenance: Provenance
    parent_id: str | None = None
    timestamp: HistoricalTimestamp | None = None


@dataclass(frozen=True)
class Attachment:
    id: str
    media_type: str
    data: bytes
    name: str | None = None

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class Checkpoint:
    id: str
    after_message_id: str
    summary: tuple[Message, ...]
    retained_message_ids: tuple[str, ...] = ()
    retained_context: tuple[Message, ...] | None = None
    following_context: tuple[Message, ...] | None = None


@dataclass(frozen=True)
class Conversation:
    source: Identity
    branch_id: str
    history: tuple[Message, ...]
    context: tuple[Message, ...]
    checkpoints: tuple[Checkpoint, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    title: str | None = None
    schema_version: int = 1
    source_created_at: HistoricalTimestamp | None = None
    source_updated_at: HistoricalTimestamp | None = None


def _unique(values: tuple[str, ...], label: str) -> None:
    if any(not value for value in values) or len(set(values)) != len(values):
        raise PreservationError(f"{label}: empty or duplicate identity")


def _provenance(value: Provenance) -> None:
    if value.origin not in ("authored", "native", "harness"):
        raise PreservationError("Unknown provenance origin")
    if not value.source.agent or not value.source.native_id or not value.locator:
        raise PreservationError("Provenance requires source identity and native locator")
    if value.origin == "harness" and not value.evidence:
        raise PreservationError("Harness provenance requires structural evidence")


def _attachment_refs(content: Content) -> tuple[str, ...]:
    if isinstance(content, AttachmentRef):
        return (content.attachment_id,)
    if isinstance(content, ToolResult):
        return tuple(
            item.attachment_id for item in content.output if isinstance(item, AttachmentRef)
        )
    return ()


def _validate_call(content: ToolCall) -> None:
    if not content.call_id or not content.name:
        raise PreservationError("Tool calls require an ID and name")
    if content.argument_format == "json":
        try:
            json.loads(
                content.arguments,
                parse_constant=_invalid_constant,
                object_pairs_hook=unique_object,
            )
        except (ValueError, TypeError) as exc:
            raise PreservationError("Invalid JSON tool arguments") from exc
    elif content.argument_format != "freeform":
        raise PreservationError("Unknown tool argument format")


def _validate_content(content: Content) -> None:
    if isinstance(content, Text | Instruction) and not isinstance(content.text, str):
        raise PreservationError("Conversational text must be a string")
    if isinstance(content, Instruction):
        _provenance(content.provenance)
        if content.authority not in ("system", "developer", "user"):
            raise PreservationError("Unknown instruction authority")
    elif isinstance(content, ToolCall):
        _validate_call(content)
    elif isinstance(content, ToolResult):
        if type(content.is_error) is not bool:
            raise PreservationError("Tool result error state must be boolean")
        for item in content.output:
            if isinstance(item, Text | AttachmentRef):
                _validate_content(item)
        if any(not isinstance(item, Text | AttachmentRef) for item in content.output):
            raise PreservationError("Unknown tool result content")
    elif isinstance(content, ToolOmission):
        if content.calls < 1 or content.results != content.calls:
            raise PreservationError("Omission must represent complete tool pairs")
        if len(content.names) != content.calls or any(not name for name in content.names):
            raise PreservationError("Omission must name each original call in order")
    elif not isinstance(content, Text | AttachmentRef):
        raise PreservationError(f"Unknown conversational content: {type(content).__name__}")


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def validate_messages(messages: tuple[Message, ...], attachment_ids: set[str]) -> None:
    _validate_shape(messages, tuple[Message, ...])
    _unique(tuple(message.id for message in messages), "Messages")
    pending: set[str] = set()
    calls: set[str] = set()
    previous: str | None = None
    for message in messages:
        _provenance(message.provenance)
        _timestamp(message.timestamp)
        if message.role not in ("user", "assistant", "system", "developer", "tool"):
            raise PreservationError(f"Unknown message role: {message.role}")
        if message.parent_id is not None and message.parent_id != previous:
            raise PreservationError(f"Missing, forward or cyclic parent: {message.parent_id}")
        previous = message.id
        for content in message.content:
            _validate_content(content)
            if set(_attachment_refs(content)) - attachment_ids:
                raise PreservationError("Missing portable attachment bytes")
            if isinstance(content, ToolCall):
                if content.call_id in calls or message.role != "assistant":
                    raise PreservationError(
                        f"Duplicate or non-assistant tool call: {content.call_id}"
                    )
                pending.add(content.call_id)
                calls.add(content.call_id)
            elif isinstance(content, ToolResult):
                if content.call_id not in pending or message.role not in ("user", "tool"):
                    raise PreservationError(
                        f"Unmatched or misplaced tool result: {content.call_id}"
                    )
                pending.remove(content.call_id)
    if pending:
        raise PreservationError(f"Incomplete tool exchanges: {', '.join(sorted(pending))}")


def validate(conversation: Conversation) -> None:
    _validate_shape(conversation, Conversation)
    _timestamp(conversation.source_created_at)
    _timestamp(conversation.source_updated_at)
    if conversation.schema_version != 1:
        raise PreservationError(f"Unsupported canonical version: {conversation.schema_version}")
    if (
        not conversation.source.agent
        or not conversation.source.native_id
        or not conversation.branch_id
    ):
        raise PreservationError("Source and selected branch identities are required")
    _unique(tuple(item.id for item in conversation.attachments), "Attachments")
    attachment_ids = {item.id for item in conversation.attachments}
    for item in conversation.attachments:
        if not item.media_type or not isinstance(item.data, bytes):
            raise PreservationError("Attachment requires media type and owned bytes")
    validate_messages(conversation.history, attachment_ids)
    validate_messages(conversation.context, attachment_ids)
    _unique(tuple(item.id for item in conversation.checkpoints), "Checkpoints")
    positions = {message.id: index for index, message in enumerate(conversation.history)}
    previous = -1
    for checkpoint in conversation.checkpoints:
        boundary = positions.get(checkpoint.after_message_id, -1)
        if boundary < previous or boundary < 0:
            raise PreservationError("Missing or unordered checkpoint boundary")
        previous = boundary
        _unique(checkpoint.retained_message_ids, "Checkpoint retained messages")
        retained = [positions.get(item, -1) for item in checkpoint.retained_message_ids]
        if any(index < 0 or index > boundary for index in retained) or retained != sorted(retained):
            raise PreservationError("Invalid checkpoint retained branch/order")
        if not checkpoint.summary:
            raise PreservationError("Checkpoint requires an explicit context summary")
        validate_messages(checkpoint.summary, attachment_ids)
        if checkpoint.following_context is not None:
            validate_messages(checkpoint.following_context, attachment_ids)
        if checkpoint.retained_context is not None:
            if checkpoint.retained_message_ids:
                raise PreservationError(
                    "Checkpoint cannot mix historical references and owned retained context"
                )
            validate_messages((*checkpoint.summary, *checkpoint.retained_context), attachment_ids)
    if conversation.checkpoints:
        latest = conversation.checkpoints[-1]
        by_id = {message.id: message for message in conversation.history}
        expected = (
            *latest.summary,
            *(
                latest.retained_context
                if latest.retained_context is not None
                else (by_id[item] for item in latest.retained_message_ids)
            ),
            *(
                latest.following_context
                if latest.following_context is not None
                else conversation.history[positions[latest.after_message_id] + 1 :]
            ),
        )
        if tuple(_without_parent(item) for item in conversation.context) != tuple(
            _without_parent(item) for item in expected
        ):
            raise PreservationError(
                "Active context disagrees with latest checkpoint and retained tail"
            )


def _without_parent(message: Message) -> Message:
    return replace(message, parent_id=None)


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PreservationError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


@cache
def _field_types(expected: type) -> dict[str, Any]:
    return get_type_hints(expected)


def _validate_shape(value: Any, expected: Any) -> None:
    origin = get_origin(expected)
    choices = get_args(expected)
    if origin in (Union, types.UnionType):
        for choice in choices:
            try:
                _validate_shape(value, choice)
            except PreservationError:
                continue
            return
        raise PreservationError("Value does not match a supported canonical content shape")
    if origin is Literal:
        if value not in choices or type(value) not in {type(item) for item in choices}:
            raise PreservationError(f"Unknown canonical enum value: {value!r}")
    elif origin is tuple:
        if type(value) is not tuple:
            raise PreservationError("Canonical sequences must be tuples")
        for item in value:
            _validate_shape(item, choices[0])
    elif isinstance(expected, type) and is_dataclass(expected):
        if type(value) is not expected:
            raise PreservationError(f"Expected canonical {expected.__name__}")
        hints = _field_types(cast(Any, expected))
        for field in fields(expected):
            _validate_shape(getattr(value, field.name), hints[field.name])
    elif type(value) is not expected:
        raise PreservationError(f"Expected canonical {expected.__name__}")
    elif expected is str:
        try:
            value.encode("utf-8")
        except UnicodeError as exc:
            raise PreservationError("Canonical strings must be valid UTF-8") from exc


def _timestamp(value: HistoricalTimestamp | None) -> None:
    if value is not None:
        if not value.value:
            raise PreservationError("Historical timestamp requires a source value")
        _provenance(value.provenance)
