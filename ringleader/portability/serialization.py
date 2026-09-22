from __future__ import annotations

import base64
import json
import types
from dataclasses import fields, is_dataclass
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from .model import Conversation, PreservationError, unique_object, validate


def _encode(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "type": type(value).__name__,
            **{f.name: _encode(getattr(value, f.name)) for f in fields(value)},
        }
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    return value


def _decode(value: Any, expected: Any) -> Any:
    origin = get_origin(expected)
    args = get_args(expected)
    if origin in (Union, types.UnionType):
        for choice in args:
            try:
                return _decode(value, choice)
            except PreservationError:
                continue
        raise PreservationError("Value does not match a supported canonical content shape")
    if origin is Literal:
        if value not in args or type(value) not in {type(item) for item in args}:
            raise PreservationError(f"Unknown canonical enum value: {value!r}")
        return value
    if origin is tuple:
        if not isinstance(value, list):
            raise PreservationError("Expected canonical list")
        return tuple(_decode(item, args[0]) for item in value)
    if isinstance(expected, type) and is_dataclass(expected):
        names = {field.name for field in fields(expected)}
        if not isinstance(value, dict) or value.get("type") != expected.__name__:
            raise PreservationError("Unknown canonical object type")
        if expected.__name__ == "Checkpoint" and "following_context" not in value:
            value = {**value, "following_context": None}
        if set(value) != names | {"type"}:
            raise PreservationError(f"Unknown or missing fields in {expected.__name__}")
        hints = get_type_hints(expected)
        return expected(**{name: _decode(value[name], hints[name]) for name in names})
    if expected is bytes:
        try:
            return base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as exc:
            raise PreservationError("Invalid attachment encoding") from exc
    if type(value) is not expected:
        raise PreservationError(f"Expected canonical {expected.__name__}")
    return value


def dumps(conversation: Conversation) -> bytes:
    validate(conversation)
    return json.dumps(
        _encode(conversation), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def loads(data: bytes) -> Conversation:
    try:
        conversation = _decode(json.loads(data, object_pairs_hook=unique_object), Conversation)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise PreservationError(f"Invalid canonical document: {exc}") from exc
    validate(conversation)
    return conversation
