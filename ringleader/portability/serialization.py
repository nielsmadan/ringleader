from __future__ import annotations

import base64
import json
import types
from dataclasses import MISSING, fields, is_dataclass
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from .model import Conversation, PreservationError, unique_object, validate

SPARSE_FIELDS = {
    "Checkpoint": ("replaced_context",),
    "Message": ("hidden",),
    "StoredStage": ("normalizations",),
    "TransformOptions": ("portable_version",),
}
ADDITIVE_FIELDS = {
    "Checkpoint": ("following_context", "replaced_context"),
    "Compatibility": ("warnings", "needs_verification"),
    "Message": ("hidden",),
    "StoredStage": ("normalizations",),
    "TransformOptions": ("portable_version",),
}
# Fields older journals still carry; decoding drops them.
RETIRED_FIELDS = {
    "CompactReport": ("omitted_results",),
}


def _sparse(value: Any, field: Any) -> bool:
    return (
        field.name in SPARSE_FIELDS.get(type(value).__name__, ())
        and getattr(value, field.name) == field.default
    )


def encode_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "type": type(value).__name__,
            **{
                f.name: encode_value(getattr(value, f.name))
                for f in fields(value)
                if not _sparse(value, f)
            },
        }
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, tuple):
        return [encode_value(item) for item in value]
    return value


def decode_value(value: Any, expected: Any) -> Any:
    origin = get_origin(expected)
    args = get_args(expected)
    if origin in (Union, types.UnionType):
        for choice in args:
            try:
                return decode_value(value, choice)
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
        return tuple(decode_value(item, args[0]) for item in value)
    if isinstance(expected, type) and is_dataclass(expected):
        names = {field.name for field in fields(expected)}
        if not isinstance(value, dict) or value.get("type") != expected.__name__:
            raise PreservationError("Unknown canonical object type")
        value = {
            **{
                item.name: encode_value(item.default)
                for item in fields(expected)
                if item.name in ADDITIVE_FIELDS.get(expected.__name__, ())
                and item.default is not MISSING
            },
            **value,
        }
        for retired in RETIRED_FIELDS.get(expected.__name__, ()):
            value.pop(retired, None)
        if set(value) != names | {"type"}:
            raise PreservationError(f"Unknown or missing fields in {expected.__name__}")
        hints = get_type_hints(expected)
        return expected(**{name: decode_value(value[name], hints[name]) for name in names})
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
        encode_value(conversation), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def loads(data: bytes) -> Conversation:
    try:
        conversation: Conversation = decode_value(
            json.loads(data, object_pairs_hook=unique_object), Conversation
        )
    except (ValueError, TypeError, UnicodeError) as exc:
        raise PreservationError(f"Invalid canonical document: {exc}") from exc
    validate(conversation)
    return conversation
