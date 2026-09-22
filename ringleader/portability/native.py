from __future__ import annotations

import base64
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from .capabilities import NativeRoots, Runner, RunRequest, RunResult
from .model import (
    Attachment,
    AttachmentRef,
    Content,
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
    unique_object,
)
from .serialization import _decode, _encode

MAX_BYTES = 128 * 1024 * 1024


def records(data: bytes) -> list[dict[str, Any]]:
    try:
        result = [
            json.loads(line, object_pairs_hook=unique_object)
            for line in data.splitlines()
            if line.strip()
        ]
    except (ValueError, UnicodeError) as exc:
        raise PreservationError(f"Invalid native JSONL: {exc}") from exc
    if not result or any(not isinstance(item, dict) for item in result):
        raise PreservationError("Native JSONL requires object records")
    return result


def jsonl(rows: list[dict[str, Any]]) -> bytes:
    return (
        "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows) + "\n"
    ).encode()


def string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise PreservationError(f"Expected native string: {label}")
    return value


def chain(
    rows: list[dict[str, Any]], id_key: str, parent_key: str, leaf: str | None
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = string(row.get(id_key), id_key)
        if not key or key in by_id:
            raise PreservationError("Duplicate or empty native entry ID")
        parent = row.get(parent_key)
        if parent is not None and not isinstance(parent, str):
            raise PreservationError("Invalid native parent identity")
        if parent is not None and parent not in by_id:
            raise PreservationError(f"Missing, cyclic or forward native parent: {parent}")
        by_id[key] = row
    if not rows:
        raise PreservationError("Empty native branch")
    leaf = leaf or rows[-1][id_key]
    if leaf not in by_id:
        raise PreservationError(f"Unknown selected branch: {leaf}")
    result = []
    while leaf is not None:
        row = by_id[leaf]
        result.append(row)
        leaf = row.get(parent_key)
    return list(reversed(result))


def linked(messages: list[Message] | tuple[Message, ...]) -> tuple[Message, ...]:
    return tuple(
        replace(item, parent_id=messages[index - 1].id if index else None)
        for index, item in enumerate(messages)
    )


def timestamp(value: Any, provenance: Provenance) -> HistoricalTimestamp | None:
    return HistoricalTimestamp(str(value), provenance) if value is not None else None


def env(roots: NativeRoots, agent: str) -> tuple[tuple[str, str], ...]:
    values = {
        "HOME": str(roots.home),
        "XDG_CONFIG_HOME": str(roots.config),
        "XDG_CACHE_HOME": str(roots.cache),
        "XDG_DATA_HOME": str(roots.data or roots.home / "data"),
        "XDG_STATE_HOME": str(roots.state or roots.home / "state"),
        "TMPDIR": str(roots.temporary or roots.cache / "tmp"),
    }
    values[
        {"claude-code": "CLAUDE_CONFIG_DIR", "pi": "PI_CODING_AGENT_DIR", "codex": "CODEX_HOME"}[
            agent
        ]
    ] = str(roots.store)
    return tuple(values.items())


def run_request(
    executable: str, args: tuple[str, ...], roots: NativeRoots, agent: str
) -> RunRequest:
    return RunRequest((executable, *args), roots.cwd, env(roots, agent))


def safe_id(identity: Identity, agent: str) -> None:
    if (
        identity.agent != agent
        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", identity.native_id)
        or identity.native_id.endswith(".")
    ):
        raise PreservationError(f"Invalid {agent} target identity")


def note(item: Instruction | ToolOmission) -> str:
    if isinstance(item, Instruction):
        return f"[Historical {item.authority} instruction; origin {item.provenance.source.agent}:{item.provenance.source.native_id} at {item.provenance.locator}]\n{item.text}"
    return f"[Omitted {item.calls} tool calls and {item.results} results: {', '.join(item.names)}]"


def message_annotations(message: Message) -> dict[str, Any]:
    return {
        "provenance": _encode(message.provenance),
        "timestamp": _encode(message.timestamp),
        "role": message.role,
        "content": {
            str(i): _encode(item)
            for i, item in enumerate(message.content)
            if isinstance(item, Instruction | ToolOmission)
        },
    }


def restore(message: Message, row: dict[str, Any]) -> Message:
    metadata = row.get("ringleader")
    if metadata is None:
        return message
    if not isinstance(metadata, dict) or set(metadata) != {
        "provenance",
        "timestamp",
        "role",
        "content",
    }:
        raise PreservationError("Invalid native portability annotations")
    if not isinstance(metadata["content"], dict):
        raise PreservationError("Invalid historical content annotations")
    content = list(message.content)
    for key, raw in metadata["content"].items():
        try:
            index = int(key)
            if str(index) != key:
                raise PreservationError("Invalid historical content index")
            item = _decode(raw, Instruction | ToolOmission)
            actual = content[index] if index >= 0 else None
        except (ValueError, IndexError, TypeError) as exc:
            raise PreservationError("Invalid historical annotation") from exc
        if actual != Text(note(item)):
            raise PreservationError("Historical annotation disagrees with native content")
        content[index] = item
    role = metadata["role"]
    tool_role = (
        role in ("user", "tool")
        and message.role in ("user", "tool")
        and all(isinstance(item, ToolResult) for item in content)
    )
    historical_role = (
        role in ("system", "developer")
        and message.role == "user"
        and bool(content)
        and all(isinstance(item, Instruction) and item.authority == role for item in content)
    )
    if role != message.role and not (tool_role or historical_role):
        raise PreservationError("Native role disagrees with portability annotations")
    return replace(
        message,
        role=role,
        content=tuple(content),
        provenance=_decode(metadata["provenance"], Provenance),
        timestamp=_decode(metadata["timestamp"], HistoricalTimestamp | None),
    )


def session_metadata(conversation: Conversation) -> dict[str, Any]:
    return {
        "branch": conversation.branch_id,
        "created": _encode(conversation.source_created_at),
        "updated": _encode(conversation.source_updated_at),
    }


def restore_session(conversation: Conversation, metadata: Any) -> Conversation:
    if metadata is None:
        return conversation
    if not isinstance(metadata, dict) or set(metadata) != {"branch", "created", "updated"}:
        raise PreservationError("Invalid native session annotations")
    return replace(
        conversation,
        branch_id=string(metadata["branch"], "branch"),
        source_created_at=_decode(metadata["created"], HistoricalTimestamp | None),
        source_updated_at=_decode(metadata["updated"], HistoricalTimestamp | None),
    )


def _image(block: dict[str, Any], assets: list[Attachment], dialect: str) -> AttachmentRef:
    source = block.get("source", {}) if dialect == "claude" else block
    if not isinstance(source, dict):
        raise PreservationError("Invalid native embedded image source")
    if dialect == "claude" and source.get("type") != "base64":
        raise PreservationError(
            "Claude image requires embedded base64 bytes; remote images unsupported"
        )
    try:
        data = base64.b64decode(source["data"], validate=True)
        mime = string(
            source.get("media_type" if dialect == "claude" else "mimeType"),
            "image media type",
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise PreservationError("Invalid native embedded image") from exc
    asset = Attachment(f"asset-{len(assets)}", mime, data, block.get("ringleaderName"))
    existing = next(
        (
            item
            for item in assets
            if (item.media_type, item.data, item.name) == (asset.media_type, asset.data, asset.name)
        ),
        None,
    )
    if existing is None:
        assets.append(asset)
    return AttachmentRef((existing or asset).id)


def blocks(value: Any, assets: list[Attachment], dialect: str) -> tuple[Content, ...]:
    if isinstance(value, str):
        return (Text(value),)
    if not isinstance(value, list):
        raise PreservationError("Missing or invalid native message content")
    output: list[Content] = []
    for block in value:
        if not isinstance(block, dict):
            raise PreservationError("Invalid native content block")
        kind = block.get("type")
        if kind == "text":
            output.append(Text(string(block.get("text"), "text")))
        elif kind in ("thinking", "redacted_thinking"):
            continue
        elif kind == "image":
            output.append(_image(block, assets, dialect))
        elif kind == ("tool_use" if dialect == "claude" else "toolCall"):
            arguments = block.get("input" if dialect == "claude" else "arguments")
            if not isinstance(arguments, dict):
                raise PreservationError("Native tools require JSON object arguments")
            output.append(
                ToolCall(
                    string(block.get("id"), "tool ID"),
                    string(block.get("name"), "tool name"),
                    json.dumps(arguments, sort_keys=True, separators=(",", ":")),
                    "json",
                )
            )
        elif kind == "tool_result" and dialect == "claude":
            nested = blocks(block.get("content", []), assets, dialect)
            if any(not isinstance(item, Text | AttachmentRef) for item in nested):
                raise PreservationError("Unsupported nested tool result content")
            output.append(
                ToolResult(
                    string(block.get("tool_use_id"), "tool result ID"),
                    cast(tuple[Text | AttachmentRef, ...], nested),
                    block.get("is_error", False),
                )
            )
        else:
            raise PreservationError(f"Unsupported {dialect} conversational block: {kind}")
    return tuple(output)


def emit_blocks(
    content: tuple[Content, ...], assets: dict[str, Attachment], dialect: str
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, Text | Instruction | ToolOmission):
            output.append(
                {"type": "text", "text": item.text if isinstance(item, Text) else note(item)}
            )
        elif isinstance(item, AttachmentRef):
            asset = assets[item.attachment_id]
            if asset.media_type not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                raise PreservationError(
                    f"{dialect} cannot import attachment media type {asset.media_type}"
                )
            data = base64.b64encode(asset.data).decode()
            block: dict[str, Any] = {"type": "image", "ringleaderName": asset.name}
            if dialect == "claude":
                block["source"] = {"type": "base64", "media_type": asset.media_type, "data": data}
            else:
                block.update(data=data, mimeType=asset.media_type)
            output.append(block)
        elif isinstance(item, ToolCall):
            if item.argument_format != "json" or not isinstance(json.loads(item.arguments), dict):
                raise PreservationError(
                    f"{dialect} requires object JSON tool arguments; freeform calls unsupported"
                )
            output.append(
                {
                    "type": "tool_use" if dialect == "claude" else "toolCall",
                    "id": item.call_id,
                    "name": item.name,
                    "input" if dialect == "claude" else "arguments": json.loads(item.arguments),
                }
            )
        elif isinstance(item, ToolResult) and dialect == "claude":
            output.append(
                {
                    "type": "tool_result",
                    "tool_use_id": item.call_id,
                    "content": emit_blocks(item.output, assets, dialect),
                    "is_error": item.is_error,
                }
            )
        else:
            raise PreservationError(f"Unsupported {dialect} native content: {type(item).__name__}")
    return output


def source_path(ref: str, roots: NativeRoots) -> Path:
    path = Path(ref)
    return path if path.is_absolute() else roots.store / path


def probe_version(executable: str, agent: str, runner: Runner) -> RunResult:
    with TemporaryDirectory(prefix="ringleader-version-") as temporary:
        base = Path(temporary)
        roots = NativeRoots(
            base / "home", base / "store", base / "cwd", base / "config", base / "cache"
        )
        for path in (
            roots.home,
            roots.store,
            roots.cwd,
            roots.config,
            roots.cache / "tmp",
            roots.home / "data",
            roots.home / "state",
        ):
            path.mkdir(parents=True, exist_ok=True)
        return runner.run(run_request(executable, ("--version",), roots, agent))


def native_role(message: Message) -> str:
    if (
        message.role in ("system", "developer")
        and message.content
        and all(
            isinstance(item, Instruction) and item.authority == message.role
            for item in message.content
        )
    ):
        return "user"
    return message.role


def historical_time(message: Message, fallback: datetime) -> datetime:
    if message.timestamp is None:
        return fallback
    value = message.timestamp.value
    try:
        if message.timestamp.provenance.source.agent in ("pi", "opencode") and re.fullmatch(
            r"-?\d+(?:\.\d+)?", value
        ):
            return datetime.fromtimestamp(float(value) / 1000, UTC)
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError("timezone is missing")
        return result.astimezone(UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise PreservationError(f"Cannot preserve native historical timestamp: {value!r}") from exc


def reject_assistant_images(message: Message, agent: str) -> None:
    if message.role == "assistant" and any(
        isinstance(item, AttachmentRef) for item in message.content
    ):
        raise PreservationError(
            f"{agent} assistant images are unsupported by the verified native replay format"
        )
