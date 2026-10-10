from __future__ import annotations

import base64
import io
import json
import os
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir
from typing import Any, cast

from .capabilities import (
    Compatibility,
    FormatProfile,
    NativeRoots,
    Runner,
    RunRequest,
    RunResult,
)
from .model import (
    Attachment,
    AttachmentRef,
    Content,
    Conversation,
    HistoricalTimestamp,
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
    canonical_json,
    unique_object,
)
from .serialization import _decode, _encode
from .versions import VersionVerdict

MAX_BYTES = 128 * 1024 * 1024
AUTH_MARKERS = ("authenticat", "device authorization", "browser opened", "device code")
DROID_LOGIN = "droid --version started Factory's login flow; refusing droid as a target"


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


def first_record(data: bytes) -> dict[str, Any]:
    line = next((line for line in io.BytesIO(data) if line.strip()), None)
    if line is None:
        raise PreservationError("Native JSONL requires object records")
    try:
        result = json.loads(line, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError) as exc:
        raise PreservationError(f"Invalid native JSONL: {exc}") from exc
    if not isinstance(result, dict):
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
    if agent == "droid":
        values["FACTORY_HOME_OVERRIDE"] = str(roots.store.parent)
        return tuple(values.items())
    values[
        {
            "claude-code": "CLAUDE_CONFIG_DIR",
            "pi": "PI_CODING_AGENT_DIR",
            "codex": "CODEX_HOME",
            "opencode": "OPENCODE_CONFIG_DIR",
        }[agent]
    ] = str(roots.store)
    return tuple(values.items())


def run_request(
    executable: str, args: tuple[str, ...], roots: NativeRoots, agent: str
) -> RunRequest:
    return RunRequest((executable, *args), roots.cwd, env(roots, agent))


def _same_path(left: str, right: str) -> bool:
    return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()


def launch_env(
    values: Mapping[str, str], environ: Mapping[str, str]
) -> tuple[tuple[str, str], ...]:
    home = values.get("HOME") or environ.get("HOME") or str(Path.home())
    data = values.get("XDG_DATA_HOME") or environ.get("XDG_DATA_HOME") or f"{home}/.local/share"
    builtin = {
        "HOME": str(Path.home()),
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_CACHE_HOME": f"{home}/.cache",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_STATE_HOME": f"{home}/.local/state",
        "TMPDIR": gettempdir(),
        "CLAUDE_CONFIG_DIR": f"{home}/.claude",
        "CODEX_HOME": f"{home}/.codex",
        "PI_CODING_AGENT_DIR": f"{home}/.pi/agent",
        "FACTORY_HOME_OVERRIDE": home,
        "OPENCODE_DB": f"{data}/opencode/opencode.db",
    }
    return tuple(
        (key, value)
        for key, value in values.items()
        if not (
            (native := environ.get(key) or builtin.get(key)) is not None
            and _same_path(value, native)
        )
    )


def launch_request(
    executable: str, args: tuple[str, ...], roots: NativeRoots, agent: str
) -> RunRequest:
    unresolved = {
        key
        for key, root in (
            ("XDG_DATA_HOME", roots.data),
            ("XDG_STATE_HOME", roots.state),
            ("TMPDIR", roots.temporary),
        )
        if root is None
    }
    values = {key: value for key, value in env(roots, agent) if key not in unresolved}
    return RunRequest((executable, *args), roots.cwd, launch_env(values, os.environ))


def safe_id(identity: Identity, agent: str) -> None:
    if (
        identity.agent != agent
        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", identity.native_id)
        or identity.native_id.endswith(".")
    ):
        raise PreservationError(f"Invalid {agent} target identity")


def streams(conversation: Conversation) -> tuple[tuple[Message, ...], ...]:
    return (
        conversation.history,
        conversation.context,
        *(
            stream
            for checkpoint in conversation.checkpoints
            for stream in (
                checkpoint.summary,
                checkpoint.retained_context,
                checkpoint.following_context,
                checkpoint.replaced_context,
            )
            if stream is not None
        ),
    )


def reject_opaque(
    conversation: Conversation, allowed: frozenset[tuple[str, str]] = frozenset()
) -> None:
    for stream in streams(conversation):
        for message in stream:
            for item in message.content:
                parts = item.output if isinstance(item, ToolResult) else (item,)
                opaque = next(
                    (
                        part
                        for part in parts
                        if isinstance(part, Opaque) and (part.agent, part.kind) not in allowed
                    ),
                    None,
                )
                if opaque is not None:
                    raise PreservationError(f"Unsupported opaque kind: {opaque.kind}")


def note(item: Instruction | ToolOmission) -> str:
    if isinstance(item, Instruction):
        return f"[Historical {item.authority} instruction; origin {item.provenance.source.agent}:{item.provenance.source.native_id} at {item.provenance.locator}]\n{item.text}"
    return f"[Omitted {item.calls} tool calls and {item.results} results: {', '.join(item.names)}]"


def object_arguments(call: ToolCall, dialect: str) -> dict[str, Any]:
    if call.argument_format == "freeform":
        return {"input": call.arguments}
    arguments = json.loads(call.arguments)
    if not isinstance(arguments, dict):
        raise PreservationError(f"{dialect} requires object JSON tool arguments")
    return arguments


def _annotated(item: Content, freeform_calls: bool = True) -> bool:
    return isinstance(item, Instruction | ToolOmission) or (
        freeform_calls and isinstance(item, ToolCall) and item.argument_format == "freeform"
    )


def _matches_native(item: Instruction | ToolOmission | ToolCall, actual: Content | None) -> bool:
    if isinstance(item, ToolCall):
        return (
            isinstance(actual, ToolCall)
            and (actual.call_id, actual.name, actual.argument_format)
            == (item.call_id, item.name, "json")
            and json.loads(actual.arguments) == {"input": item.arguments}
        )
    return actual == Text(note(item))


def message_annotations(message: Message, *, freeform_calls: bool = False) -> dict[str, Any]:
    return {
        "provenance": _encode(message.provenance),
        "timestamp": _encode(message.timestamp),
        "role": message.role,
        "content": {
            str(i): _encode(item)
            for i, item in enumerate(message.content)
            if _annotated(item, freeform_calls)
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
            item = _decode(raw, Instruction | ToolOmission | ToolCall)
            actual = content[index] if index >= 0 else None
        except (ValueError, IndexError, TypeError) as exc:
            raise PreservationError("Invalid historical annotation") from exc
        if not _annotated(item) or not _matches_native(item, actual):
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
        elif kind == "fallback" and dialect == "claude":
            continue
        elif kind == "tool_reference" and dialect == "claude":
            output.append(
                Opaque(
                    "claude-code",
                    "claude.tool_reference",
                    canonical_json(block),
                    (Text(f"[loaded tool schema: {string(block.get('tool_name'), 'tool name')}]"),),
                )
            )
        elif kind == "tool_result" and dialect == "claude":
            nested = blocks(block.get("content", []), assets, dialect)
            if any(not isinstance(item, Text | AttachmentRef | Opaque) for item in nested):
                raise PreservationError("Unsupported nested tool result content")
            output.append(
                ToolResult(
                    string(block.get("tool_use_id"), "tool result ID"),
                    cast(tuple[Text | AttachmentRef | Opaque, ...], nested),
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
            block: dict[str, Any] = {"type": "image"}
            if dialect == "claude":
                block["source"] = {"type": "base64", "media_type": asset.media_type, "data": data}
            else:
                block.update(data=data, mimeType=asset.media_type, ringleaderName=asset.name)
            output.append(block)
        elif isinstance(item, ToolCall):
            output.append(
                {
                    "type": "tool_use" if dialect == "claude" else "toolCall",
                    "id": item.call_id,
                    "name": item.name,
                    "input" if dialect == "claude" else "arguments": object_arguments(
                        item, dialect
                    ),
                }
            )
        elif (
            isinstance(item, Opaque)
            and dialect == "claude"
            and (item.agent, item.kind) == ("claude-code", "claude.tool_reference")
        ):
            output.append(json.loads(item.payload))
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


def started_auth(result: RunResult) -> bool:
    output = (result.stdout + result.stderr).decode(errors="replace").lower()
    return any(marker in output for marker in AUTH_MARKERS)


def probe_version(executable: str, agent: str, runner: Runner) -> RunResult:
    with TemporaryDirectory(prefix="ringleader-version-") as temporary:
        base = Path(temporary).resolve()
        store = base / "home" / ".factory" if agent == "droid" else base / "store"
        roots = NativeRoots(base / "home", store, base / "cwd", base / "config", base / "cache")
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
        request = run_request(executable, ("--version",), roots, agent)
        if agent == "droid":
            request = replace(request, env=(*request.env, ("FACTORY_DISABLE_KEYRING", "1")))
        result = runner.run(request)
    if agent == "droid" and started_auth(result):
        raise PreservationError(DROID_LOGIN)
    return result


def version_compatibility(
    label: str,
    profile: FormatProfile,
    verdict: VersionVerdict,
    output: str,
    schema_identity: str | None = None,
) -> Compatibility:
    if not verdict.accepted:
        hint = verification_hint(profile.agent) if verdict.needs_verification else ""
        return Compatibility(
            False,
            None,
            (f"{label} {verdict.message}{hint}",),
            output,
            needs_verification=verdict.needs_verification,
        )
    return Compatibility(
        True,
        profile,
        (),
        output,
        schema_identity,
        warnings=(f"{label} {verdict.message}",) if verdict.message else (),
    )


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


def unparented(messages: list[Message] | tuple[Message, ...]) -> tuple[Message, ...]:
    return tuple(replace(message, parent_id=None) for message in messages)


def results_only(message: Message) -> bool:
    return bool(message.content) and all(isinstance(item, ToolResult) for item in message.content)


def validate_replay(messages: tuple[Message, ...], label: str) -> None:
    pending: set[str] = set()
    for message in messages:
        if pending and not results_only(message):
            raise PreservationError(
                f"{label} tool results must follow their assistant calls contiguously; "
                "an intervening message changes native replay"
            )
        for item in message.content:
            if isinstance(item, ToolCall):
                pending.add(item.call_id)
            elif isinstance(item, ToolResult):
                pending.discard(item.call_id)


def verification_hint(agent: str) -> str:
    return f"; run rl doctor --verify-conversion {agent}"


def reject_assistant_images(message: Message, agent: str) -> None:
    if message.role == "assistant" and any(
        isinstance(item, AttachmentRef) for item in message.content
    ):
        raise PreservationError(
            f"{agent} assistant images are unsupported by the verified native replay format"
        )
