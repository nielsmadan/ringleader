from __future__ import annotations

import base64
import json
from typing import Any, Literal, Protocol, cast

from . import native
from .model import (
    Attachment,
    AttachmentRef,
    Content,
    Identity,
    Instruction,
    Message,
    Opaque,
    PreservationError,
    Provenance,
    Text,
    ToolCall,
    ToolResult,
    canonical_json,
)

PHASE = "codex.phase"
BASE_INSTRUCTIONS = "codex.base_instructions"
COMPACTION = "codex.compaction"
OPAQUE_KINDS = frozenset({("codex", PHASE), ("codex", BASE_INSTRUCTIONS), ("codex", COMPACTION)})
COMPACTION_FIELDS = frozenset(
    {"type", "id", "encrypted_content", "internal_chat_message_metadata_passthrough"}
)
PHASES = ("commentary", "final_answer")
DEFAULT_DETAIL = "high"
SCRIPT_FAILED = "Script failed\nWall time "
TOLERATED_METADATA = frozenset(
    {
        "client_authored",
        "fallback_token_limit_override",
        "user_input_order",
        "retained_source",
        "mcp_attribution",
        "compaction_model_hash",
    }
)
AUTHORED_KINDS = {"user.text": "user", "user.image": "user"}
BOOTSTRAP_KINDS = frozenset(
    {
        "agents_md.instructions",
        "apps.instructions",
        "collaboration_mode.instructions",
        "current_time.reminder",
        "current_time.unavailable",
        "environments.environment_context",
        "environments.instructions",
        "generic.developer_instructions",
        "host_skills.instructions",
        "managed_config.developer_instructions",
        "model.base_instructions",
        "model_switch.instructions",
        "multi_agent.mode_instructions",
        "multi_agent.model_catalog",
        "multi_agent.role_instructions",
        "multi_agent.usage_hint",
        "network_proxy.rule_saved",
        "permissions.approved_command_prefix_saved",
        "permissions.instructions",
        "persistent_mode.instructions",
        "plugins.instructions",
        "plugins.recommendations",
        "plugins.usage_instructions",
        "realtime_conversation.custom_start_instructions",
        "realtime_conversation.end_instructions",
        "realtime_conversation.start_instructions",
        "rollout_budget.remaining_tokens",
        "skills.catalog",
        "token_budget.context_window",
        "token_budget.context_window_guidance",
        "token_budget.remaining_tokens",
        "tools.deferred_namespaces",
    }
)
CARRIED_KINDS = frozenset(
    {
        "generic.turn_aborted",
        "hooks.additional_context",
        "multi_agent.subagent_notification",
        "shell.user_command",
        "skills.selected_skill_instructions",
    }
)
BOOTSTRAP_MARKERS = (
    ("# AGENTS.md instructions", "</INSTRUCTIONS>"),
    ("<user_instructions>", "</user_instructions>"),
    ("<environment_context>", "</environment_context>"),
    ("<recommended_plugins>", "</recommended_plugins>"),
)
CARRIED_MARKERS = (
    ("<turn_aborted>", "</turn_aborted>"),
    ("<subagent_notification>", "</subagent_notification>"),
    ("<skill>", "</skill>"),
    ("<user_shell_command>", "</user_shell_command>"),
    ("<agent_message_board_notification>", "</agent_message_board_notification>"),
    ("<codex_internal_context", "</codex_internal_context>"),
    ("<goal_context>", "</goal_context>"),
    ("<hook_prompt", "</hook_prompt>"),
)

BlockClass = Literal["authored", "drop", "carry"]


class ItemState(Protocol):
    identity: Identity
    assets: list[Attachment]
    errors: dict[str, bool]
    calls: dict[str, ToolCall]
    turn: str | None


def as_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PreservationError("Codex payload must be an object")
    return value


def as_array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise PreservationError("Codex content must be an array")
    return value


def image(value: Any, assets: list[Attachment], name: str | None = None) -> AttachmentRef:
    url = native.string(value, "image URL")
    if not url.startswith("data:image/") or ";base64," not in url:
        raise PreservationError(
            "Codex portable images require embedded data URLs; external/local image references unsupported"
        )
    mime, data = url[5:].split(";base64,", 1)
    try:
        decoded = base64.b64decode(data, validate=True)
    except ValueError as exc:
        raise PreservationError("Invalid Codex image bytes") from exc
    if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        raise PreservationError(f"Unsupported Codex image media type: {mime}")
    asset = next(
        (a for a in assets if (a.media_type, a.data, a.name) == (mime, decoded, name)), None
    )
    if asset is None:
        asset = Attachment(f"asset-{len(assets)}", mime, decoded, name)
        assets.append(asset)
    return AttachmentRef(asset.id)


def blocks(value: Any, assets: list[Attachment]) -> tuple[Text | AttachmentRef, ...]:
    if isinstance(value, str):
        return (Text(value),)
    output: list[Text | AttachmentRef] = []
    for raw in as_array(value):
        block = as_object(raw)
        if block.get("type") in ("input_text", "output_text"):
            output.append(Text(native.string(block.get("text"), "text")))
        elif block.get("type") == "input_image":
            if block.get("detail") not in (None, "auto", DEFAULT_DETAIL):
                raise PreservationError(
                    "Codex image detail cannot be preserved by the canonical attachment model"
                )
            output.append(image(block.get("image_url"), assets, block.get("ringleaderName")))
        elif block.get("type") == "encrypted_content":
            continue
        else:
            raise PreservationError(f"Unsupported Codex content block: {block.get('type')}")
    return tuple(output)


def message(
    identity: Identity,
    key: str,
    role: str,
    content: tuple[Content, ...],
    row: dict[str, Any],
    turn: str | None,
) -> Message:
    provenance = Provenance("authored", identity, key, f"codex-turn:{turn}" if turn else "")
    return Message(
        key,
        cast(Any, role),
        content,
        provenance,
        timestamp=native.timestamp(row.get("timestamp"), provenance),
    )


def phase_opaque(value: Any) -> Opaque:
    if value not in PHASES:
        raise PreservationError(f"Unsupported Codex assistant message phase: {value}")
    return Opaque("codex", PHASE, canonical_json({"phase": value}))


def phase_value(item: Opaque) -> str:
    value = as_object(json.loads(item.payload)).get("phase")
    if item.kind != PHASE or value not in PHASES:
        raise PreservationError(f"Unsupported opaque kind: {item.kind}")
    return cast(str, value)


def is_base(message: Message) -> bool:
    return any(
        isinstance(item, Opaque) and item.kind == BASE_INSTRUCTIONS for item in message.content
    )


def is_compaction(message: Message) -> bool:
    return any(isinstance(item, Opaque) and item.kind == COMPACTION for item in message.content)


def compaction_payload(item: Opaque) -> dict[str, Any]:
    if item.kind != COMPACTION:
        raise PreservationError(f"Unsupported opaque kind: {item.kind}")
    return as_object(json.loads(item.payload))


def compaction_message(
    payload: dict[str, Any], row: dict[str, Any], key: str, state: ItemState
) -> Message:
    item = {name: value for name, value in payload.items() if not name.startswith("ringleader")}
    if set(item) - COMPACTION_FIELDS:
        raise PreservationError("Unsupported Codex compaction item fields")
    if not native.string(item.get("encrypted_content"), "compaction content"):
        raise PreservationError("Codex compaction item has no encrypted content")
    content: tuple[Content, ...] = (Opaque("codex", COMPACTION, canonical_json(item)),)
    return message(state.identity, key, "assistant", content, row, state.turn)


def base_message(meta: dict[str, Any], identity: Identity) -> Message | None:
    base = meta.get("base_instructions")
    if base is None:
        return None
    base = as_object(base)
    if not native.string(base.get("text"), "base instructions"):
        return None
    provenance = Provenance(
        "harness",
        identity,
        "session_meta.base_instructions",
        "Codex system prompt from session_meta; only a Codex writer emits it",
    )
    result = Message(
        "base-instructions",
        "system",
        (Opaque("codex", BASE_INSTRUCTIONS, canonical_json(base)),),
        provenance,
        timestamp=native.timestamp(meta.get("timestamp"), provenance),
        hidden=True,
    )
    annotations = meta.get("ringleader_base_instructions")
    return native.restore(result, {"ringleader": annotations}) if annotations else result


def check_metadata(value: Any) -> None:
    if value is None:
        return
    metadata = as_object(value)
    unknown = sorted(set(metadata) - TOLERATED_METADATA)
    if unknown:
        raise PreservationError(f"Unsupported Codex response metadata: {unknown[0]}")
    if metadata.get("client_authored") not in (None, False):
        raise PreservationError("Codex client-authored model messages are unsupported")


def _marked(text: str, markers: tuple[tuple[str, str], ...]) -> bool:
    trimmed = text.strip().lower()
    return any(
        trimmed.startswith(start.lower()) and trimmed.endswith(end.lower())
        for start, end in markers
    )


def _content_kinds(payload: dict[str, Any], count: int) -> list[str] | None:
    passthrough = payload.get("internal_chat_message_metadata_passthrough")
    kinds = as_object(passthrough).get("content_item_kinds") if passthrough is not None else None
    if kinds is None:
        return None
    if not isinstance(kinds, list) or len(kinds) != count:
        raise PreservationError("Codex content kinds disagree with message content")
    return [native.string(kind, "content kind") for kind in kinds]


def _classify(role: str, kind: str | None, block: dict[str, Any]) -> BlockClass:
    if kind is not None and kind not in ("", "unknown"):
        if AUTHORED_KINDS.get(kind) == role:
            return "authored"
        if kind in BOOTSTRAP_KINDS:
            return "drop"
        if kind in CARRIED_KINDS:
            return "carry"
        raise PreservationError(f"Unsupported Codex content kind: {kind}")
    text = block.get("text") if block.get("type") == "input_text" else None
    if role not in ("user", "developer") or not isinstance(text, str):
        return "authored"
    if _marked(text, CARRIED_MARKERS):
        return "carry"
    if role == "developer" or _marked(text, BOOTSTRAP_MARKERS):
        return "drop"
    return "authored"


def _harness(
    key: str, texts: tuple[Content, ...], row: dict[str, Any], state: ItemState, evidence: str
) -> Message:
    provenance = Provenance("harness", state.identity, key, evidence)
    return Message(
        key,
        "user",
        texts,
        provenance,
        timestamp=native.timestamp(row.get("timestamp"), provenance),
        hidden=True,
    )


def _model_message(
    payload: dict[str, Any], row: dict[str, Any], key: str, state: ItemState
) -> tuple[Message, ...]:
    role = payload.get("role")
    if role not in ("user", "assistant", "system", "developer"):
        raise PreservationError(f"Unsupported Codex model role: {role}")
    raw = payload.get("content")
    raw_blocks = [as_object(item) for item in as_array(raw)] if not isinstance(raw, str) else []
    ours = any(key in row for key in ("ringleader_group", "ringleader")) or (
        "ringleader_group" in payload
    )
    kinds = None if ours else _content_kinds(payload, len(raw_blocks))
    classes = [
        "authored" if ours else _classify(role, kinds[index] if kinds else None, block)
        for index, block in enumerate(raw_blocks)
    ]
    authored = (
        raw
        if isinstance(raw, str)
        else [block for block, kind in zip(raw_blocks, classes, strict=True) if kind == "authored"]
    )
    carried = [block for block, kind in zip(raw_blocks, classes, strict=True) if kind == "carry"]
    result: list[Message] = []
    if authored or not raw_blocks:
        content: tuple[Content, ...] = blocks(authored, state.assets)
        if role in ("system", "developer"):
            if any(not isinstance(item, Text) for item in content):
                raise PreservationError(
                    "Codex historical policy contains unsupported non-text content"
                )
            prov = Provenance("authored", state.identity, key)
            content = tuple(
                Instruction(cast(Text, item).text, cast(Any, role), prov) for item in content
            )
        if payload.get("phase") is not None:
            if role != "assistant":
                raise PreservationError("Codex message phase requires an assistant message")
            content = (phase_opaque(payload["phase"]), *content)
        result.append(message(state.identity, key, role, content, row, state.turn))
    if carried:
        texts = blocks(carried, state.assets)
        if any(not isinstance(item, Text) for item in texts):
            raise PreservationError("Codex harness context contains unsupported non-text content")
        labels = sorted(
            {kinds[index] for index, kind in enumerate(classes) if kind == "carry"}
            if kinds
            else {"wrapper-tag"}
        )
        result.append(
            _harness(
                f"{key}:harness" if result else key,
                texts,
                row,
                state,
                "codex harness context: " + ",".join(labels),
            )
        )
    return tuple(result)


def _agent_message(
    payload: dict[str, Any], row: dict[str, Any], key: str, state: ItemState
) -> Message:
    author = native.string(payload.get("author"), "agent message author")
    recipient = native.string(payload.get("recipient"), "agent message recipient")
    texts = blocks(payload.get("content"), state.assets)
    if not texts or any(not isinstance(item, Text) for item in texts):
        raise PreservationError("Codex agent message has no portable plaintext content")
    header = Text(f"[Agent message from {author} to {recipient}]")
    return _harness(key, (header, *texts), row, state, "codex agent_message")


def _script_failed(output: tuple[Text | AttachmentRef, ...]) -> bool:
    return bool(output) and isinstance(output[0], Text) and output[0].text.startswith(SCRIPT_FAILED)


def result(payload: dict[str, Any], call_id: str, state: ItemState) -> ToolResult:
    output = blocks(payload.get("output"), state.assets)
    error = state.errors.get(call_id)
    if error is None:
        error = _script_failed(output)
    marker = payload.get("ringleader_result_status")
    if marker is not None:
        if type(marker) is not bool or not output or output[0] != Text(result_status(marker)):
            raise PreservationError("Codex tool status annotation disagrees with native output")
        error, output = marker, output[1:]
    call = state.calls.get(call_id)
    if call and (payload["type"] == "custom_tool_call_output") != (
        call.argument_format == "freeform"
    ):
        raise PreservationError("Codex function/custom tool pair mismatch")
    return ToolResult(call_id, output, error)


def result_status(error: bool) -> str:
    return "[Historical tool result: failed]" if error else "[Historical tool result: succeeded]"


def _tool_call(payload: dict[str, Any], kind: str) -> ToolCall:
    name = native.string(payload.get("name"), "tool name")
    namespace = payload.get("namespace")
    if namespace is not None:
        name = f"{native.string(namespace, 'tool namespace')}.{name}"
    return ToolCall(
        native.string(payload.get("call_id"), "call ID"),
        name,
        native.string(
            payload.get("arguments" if kind == "function_call" else "input"), "tool arguments"
        ),
        "json" if kind == "function_call" else "freeform",
    )


def response(
    payload: dict[str, Any], row: dict[str, Any], key: str, state: ItemState
) -> tuple[Message, ...]:
    check_metadata(row.get("metadata"))
    kind = payload.get("type")
    if kind in ("reasoning", "tool_search_call", "tool_search_output"):
        return ()
    if kind == "message":
        return _model_message(payload, row, key, state)
    if kind == "agent_message":
        return (_agent_message(payload, row, key, state),)
    if kind in ("function_call", "custom_tool_call"):
        call = _tool_call(payload, kind)
        return (message(state.identity, key, "assistant", (call,), row, state.turn),)
    if kind in ("function_call_output", "custom_tool_call_output"):
        call_id = native.string(payload.get("call_id"), "result call ID")
        tool = result(payload, call_id, state)
        return (message(state.identity, key, "tool", (tool,), row, state.turn),)
    if kind == "compaction":
        raise PreservationError("Codex compaction item outside a compacted record is unsupported")
    raise PreservationError(f"Unsupported Codex response_item: {kind}")


def calls_of(messages: tuple[Message, ...]) -> dict[str, ToolCall]:
    return {
        part.call_id: part
        for item in messages
        for part in item.content
        if isinstance(part, ToolCall)
    }
