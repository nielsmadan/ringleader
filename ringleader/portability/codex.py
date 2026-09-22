from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from . import codex_state, native
from .capabilities import (
    Compatibility,
    FormatProfile,
    NativeFile,
    NativeIndexIntent,
    NativeMetadata,
    NativeRoots,
    NativeStage,
    Runner,
    RunRequest,
    SourceRequest,
    TargetRequest,
)
from .compare import Normalization
from .model import (
    Attachment,
    AttachmentRef,
    Checkpoint,
    Content,
    Conversation,
    Identity,
    Instruction,
    Message,
    PreservationError,
    Provenance,
    Text,
    ToolCall,
    ToolOmission,
    ToolResult,
    validate,
)
from .snapshot import SourceSnapshot, snapshot_files

PROFILE = FormatProfile(
    "codex",
    "codex-0.155.1-legacy",
    "legacy",
    (
        "https://github.com/openai/codex/tree/rust-v0.155.1/codex-rs/protocol",
        "https://github.com/openai/codex/blob/rust-v0.155.1/codex-rs/app-server-protocol/src/protocol/thread_history.rs",
    ),
)
IGNORED_EVENTS = {
    "token_count",
    "agent_reasoning",
    "agent_reasoning_raw_content",
    "agent_reasoning_section_break",
    "context_compacted",
}
IGNORED_ROWS = {"token_usage_record", "security_risk_score"}


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PreservationError("Codex payload must be an object")
    return value


def _array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise PreservationError("Codex content must be an array")
    return value


def _image(value: Any, assets: list[Attachment], name: str | None = None) -> AttachmentRef:
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


def _blocks(value: Any, assets: list[Attachment]) -> tuple[Text | AttachmentRef, ...]:
    if isinstance(value, str):
        return (Text(value),)
    output: list[Text | AttachmentRef] = []
    for raw in _array(value):
        block = _object(raw)
        if block.get("type") in ("input_text", "output_text"):
            output.append(Text(native.string(block.get("text"), "text")))
        elif block.get("type") == "input_image":
            if block.get("detail") not in (None, "auto"):
                raise PreservationError(
                    "Codex image detail cannot be preserved by the canonical attachment model"
                )
            output.append(_image(block.get("image_url"), assets, block.get("ringleaderName")))
        elif block.get("type") == "encrypted_content":
            continue
        else:
            raise PreservationError(f"Unsupported Codex content block: {block.get('type')}")
    return tuple(output)


def _message(
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


def _response(
    payload: dict[str, Any], row: dict[str, Any], key: str, state: _Reader
) -> Message | None:
    identity, assets, turn = state.identity, state.assets, state.turn
    kind = payload.get("type")
    if kind == "reasoning":
        return None
    if kind == "message":
        if payload.get("phase"):
            raise PreservationError("Codex assistant message phases are unsupported")
        role = payload.get("role")
        if role not in ("user", "assistant", "system", "developer"):
            raise PreservationError(f"Unsupported Codex model role: {role}")
        content: tuple[Content, ...] = _blocks(payload.get("content"), assets)
        if role in ("system", "developer"):
            if any(not isinstance(item, Text) for item in content):
                raise PreservationError(
                    "Codex historical policy contains unsupported non-text content"
                )
            prov = Provenance("authored", identity, key)
            content = tuple(
                Instruction(cast(Text, item).text, cast(Any, role), prov) for item in content
            )
    elif kind in ("function_call", "custom_tool_call"):
        if payload.get("namespace"):
            raise PreservationError(
                "Codex namespaced model tools require a supported canonical namespace"
            )
        content = (
            ToolCall(
                native.string(payload.get("call_id"), "call ID"),
                native.string(payload.get("name"), "tool name"),
                native.string(
                    payload.get("arguments" if kind == "function_call" else "input"),
                    "tool arguments",
                ),
                "json" if kind == "function_call" else "freeform",
            ),
        )
        role = "assistant"
    elif kind in ("function_call_output", "custom_tool_call_output"):
        call_id = native.string(payload.get("call_id"), "result call ID")
        content = (_result(payload, call_id, state),)
        role = "tool"
    else:
        raise PreservationError(f"Unsupported Codex response_item: {kind}")
    return _message(identity, key, role, content, row, turn)


def _result_status(error: bool) -> str:
    return "[Historical tool result: failed]" if error else "[Historical tool result: succeeded]"


def _result(payload: dict[str, Any], call_id: str, state: _Reader) -> ToolResult:
    output = _blocks(payload.get("output"), state.assets)
    error = state.errors.get(call_id, False)
    marker = payload.get("ringleader_result_status")
    if marker is not None:
        if type(marker) is not bool or not output or output[0] != Text(_result_status(marker)):
            raise PreservationError("Codex tool status annotation disagrees with native output")
        error, output = marker, output[1:]
    call = state.calls.get(call_id)
    if call and (payload["type"] == "custom_tool_call_output") != (
        call.argument_format == "freeform"
    ):
        raise PreservationError("Codex function/custom tool pair mismatch")
    return ToolResult(call_id, output, error)


def _mcp_output(value: Any, assets: list[Attachment]) -> tuple[Text | AttachmentRef, ...]:
    result: list[Text | AttachmentRef] = []
    for raw in _array(value):
        block = _object(raw)
        if block.get("type") == "text":
            result.append(Text(native.string(block.get("text"), "MCP text")))
        elif block.get("type") == "image":
            result.append(
                _image(
                    f"data:{block.get('mimeType')};base64,{block.get('data')}",
                    assets,
                    block.get("ringleaderName"),
                )
            )
        else:
            raise PreservationError(f"Unsupported Codex MCP output: {block.get('type')}")
    return tuple(result)


def _mcp_parts(payload: dict[str, Any], state: _Reader) -> tuple[ToolCall, ToolResult]:
    invocation = _object(payload.get("invocation"))
    call_id = native.string(payload.get("call_id"), "MCP call ID")
    model = state.calls.get(call_id)
    argument = invocation.get("arguments")
    if model is not None and model.argument_format == "freeform":
        arguments, fmt = native.string(argument, "freeform MCP arguments"), "freeform"
    else:
        arguments, fmt = json.dumps(argument, separators=(",", ":")), "json"
    call = ToolCall(
        call_id, native.string(invocation.get("tool"), "MCP tool"), arguments, cast(Any, fmt)
    )
    result = _object(payload.get("result"))
    if "Err" in result:
        return call, ToolResult(call_id, (Text(native.string(result["Err"], "MCP error")),), True)
    body = _object(result.get("Ok"))
    if body.get("structuredContent") or body.get("_meta"):
        raise PreservationError("Codex MCP structured content requires a supported semantic")
    error = body.get("isError", False)
    if type(error) is not bool:
        raise PreservationError("Codex MCP error state must be boolean")
    return call, ToolResult(call_id, _mcp_output(body.get("content"), state.assets), error)


def _event(payload: dict[str, Any], row: dict[str, Any], key: str, state: _Reader) -> Message:
    kind = payload.get("type")
    if kind not in ("user_message", "agent_message"):
        raise PreservationError(f"Unsupported Codex visible event: {kind}")
    if any(payload.get(key) for key in ("audio", "local_audio", "local_images", "questions")):
        raise PreservationError("Unsupported Codex local image, audio or question event")
    if payload.get("phase"):
        raise PreservationError("Codex assistant message phases are unsupported")
    if any(value not in (None, "auto") for value in payload.get("image_details", [])):
        raise PreservationError("Unsupported Codex image detail")
    content: tuple[Content, ...] = (Text(native.string(payload.get("message"), "visible message")),)
    names = payload.get("ringleaderImageNames", [])
    for index, url in enumerate(payload.get("images") or []):
        content += (_image(url, state.assets, names[index] if index < len(names) else None),)
    return _message(
        state.identity,
        key,
        "user" if kind == "user_message" else "assistant",
        content,
        row,
        state.turn,
    )


def _append(
    messages: list[Message],
    message: Message,
    row: dict[str, Any],
    groups: dict[str, tuple[int, int]],
) -> None:
    group = row.get("ringleader_group")
    if group is None:
        messages.append(native.restore(message, row))
        return
    group = _object(group)
    key, index, total = group.get("id"), group.get("index"), group.get("count")
    if not isinstance(key, str) or type(index) is not int or type(total) is not int or total < 1:
        raise PreservationError("Invalid Codex native message grouping")
    if index == 0:
        if key in groups:
            raise PreservationError("Duplicate Codex native group")
        groups[key] = (len(messages), total)
        messages.append(replace(message, id=key))
    else:
        if key not in groups or groups[key] != (len(messages) - 1, total - index + 1):
            raise PreservationError("Noncontiguous Codex native group")
        old = messages[-1]
        messages[-1] = replace(old, content=(*old.content, *message.content))
        groups[key] = (len(messages) - 1, total - index)
    if index == total - 1:
        messages[-1] = native.restore(messages[-1], row)
        groups[key] = (len(messages) - 1, 0)


def _labeled_output(payload: dict[str, Any], fields: tuple[str, ...]) -> tuple[Text, ...]:
    return tuple(
        part
        for key in fields
        for part in (Text(f"[Native {key}]"), Text(native.string(payload.get(key), key)))
    )


def _patch_output(payload: dict[str, Any]) -> tuple[Text, ...]:
    allowed = {"type", "call_id", "turn_id", "stdout", "stderr", "success", "changes", "status"}
    if set(payload) - allowed:
        raise PreservationError("Unsupported Codex patch completion fields")
    changes = _object(payload.get("changes", {}))
    for path, raw in changes.items():
        native.string(path, "patch path")
        change = _object(raw)
        kind = change.get("type")
        if kind in ("add", "delete") and set(change) == {"type", "content"}:
            native.string(change["content"], "patch file content")
        elif kind == "update" and set(change) <= {"type", "unified_diff", "move_path"}:
            native.string(change.get("unified_diff"), "patch diff")
            if change.get("move_path") is not None:
                native.string(change["move_path"], "patch move destination")
        else:
            raise PreservationError("Unsupported Codex FileChange shape")
    return (
        *_labeled_output(payload, ("stdout", "stderr")),
        Text("[Native file changes JSON]"),
        Text(json.dumps(changes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
    )


def _execution_output(payload: dict[str, Any]) -> tuple[tuple[Text, ...], bool]:
    allowed = {
        "type",
        "call_id",
        "plugin_id",
        "script_path",
        "process_id",
        "turn_id",
        "completed_at_ms",
        "command",
        "cwd",
        "parsed_cmd",
        "source",
        "interaction_input",
        "stdout",
        "stderr",
        "aggregated_output",
        "exit_code",
        "duration",
        "formatted_output",
        "status",
    }
    if set(payload) - allowed:
        raise PreservationError("Unsupported Codex execution completion fields")
    if (
        payload.get("status") not in ("completed", "failed", "declined")
        or type(payload.get("exit_code")) is not int
    ):
        raise PreservationError("Incomplete Codex execution status")
    return _labeled_output(
        payload, ("stdout", "stderr", "aggregated_output", "formatted_output")
    ), payload["exit_code"] != 0 or payload["status"] != "completed"


def _tool_metadata(payload: dict[str, Any]) -> str:
    fields = (
        "type",
        "command",
        "cwd",
        "exit_code",
        "status",
        "source",
        "interaction_input",
        "process_id",
        "plugin_id",
        "script_path",
        "parsed_cmd",
    )
    return "native-tool:" + json.dumps(
        {key: payload[key] for key in fields if key in payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass
class _Reader:
    identity: Identity
    assets: list[Attachment] = field(default_factory=list)
    history: list[Message] = field(default_factory=list)
    context: list[Message] = field(default_factory=list)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    groups: dict[str, tuple[int, int]] = field(default_factory=dict)
    context_groups: dict[str, tuple[int, int]] = field(default_factory=dict)
    errors: dict[str, bool] = field(default_factory=dict)
    calls: dict[str, ToolCall] = field(default_factory=dict)
    turn: str | None = None
    tool_updates: dict[str, tuple[ToolCall, ToolResult]] = field(default_factory=dict)
    base: Message | None = None
    open_tools: set[str] = field(default_factory=set)
    output_ids: dict[str, str] = field(default_factory=dict)

    def mcp(self, payload: dict[str, Any], row: dict[str, Any], index: int) -> None:
        call, result = _mcp_parts(payload, self)
        parts: tuple[tuple[str, Content], ...]
        projection = payload.get("ringleader_projection")
        if projection == "call":
            if call.call_id in self.tool_updates:
                raise PreservationError("Duplicate Codex historical tool call")
            self.tool_updates[call.call_id] = (call, result)
            parts = (("assistant", call),)
        elif projection == "result":
            if self.tool_updates.pop(call.call_id, None) != (call, result):
                raise PreservationError(
                    "Codex historical tool update disagrees with native call/result"
                )
            parts = (("tool", result),)
        elif projection is None:
            parts = (("assistant", call), ("tool", result))
        else:
            raise PreservationError("Unknown Codex historical tool projection")
        for offset, (role, part) in enumerate(parts):
            item = _message(
                self.identity,
                f"visible:{index}:{offset}",
                role,
                (cast(Content, part),),
                row,
                self.turn,
            )
            server = native.string(_object(payload.get("invocation")).get("server"), "MCP server")
            item = replace(
                item,
                provenance=replace(
                    item.provenance,
                    evidence=item.provenance.evidence + "\nmcp-server:" + json.dumps(server),
                ),
            )
            _append(self.history, item, row, self.groups)

    def begin_tool(self, payload: dict[str, Any], row: dict[str, Any], index: int) -> str:
        call_id = native.string(payload.get("call_id"), "native tool call ID")
        if call_id not in self.calls:
            raise PreservationError("Codex visible tool has no unambiguous matched model call")
        if call_id in self.open_tools:
            raise PreservationError("Duplicate Codex native tool start")
        item = _message(
            self.identity,
            f"visible:{index}:call",
            "assistant",
            (self.calls[call_id],),
            row,
            self.turn,
        )
        item = replace(
            item,
            provenance=replace(
                item.provenance, evidence=item.provenance.evidence + "\n" + _tool_metadata(payload)
            ),
        )
        self.history.append(item)
        self.open_tools.add(call_id)
        return call_id

    def end_tool(self, payload: dict[str, Any], row: dict[str, Any], index: int) -> None:
        call_id = native.string(payload.get("call_id"), "native tool call ID")
        if call_id not in self.open_tools:
            self.begin_tool(payload, row, index)
        if payload["type"] == "exec_command_end":
            output, error = _execution_output(payload)
        else:
            if type(payload.get("success")) is not bool or payload.get("status") not in (
                "completed",
                "failed",
                "declined",
            ):
                raise PreservationError("Incomplete Codex patch status")
            output, error = _patch_output(payload), not payload["success"]
        item = _message(
            self.identity,
            f"visible:{index}:result",
            "tool",
            (ToolResult(call_id, output, error),),
            row,
            self.turn,
        )
        item = replace(
            item,
            provenance=replace(
                item.provenance, evidence=item.provenance.evidence + "\n" + _tool_metadata(payload)
            ),
        )
        self.history.append(item)
        self.open_tools.remove(call_id)

    def completed_item(self, payload: dict[str, Any], row: dict[str, Any], index: int) -> None:
        item = _object(payload.get("item"))
        if set(item) - {"type", "id", "name", "namespace", "output"}:
            raise PreservationError("Unsupported Codex completed function output fields")
        if item.get("type") != "FunctionCallOutput" or item.get("namespace"):
            raise PreservationError(f"Unsupported Codex completed item: {item.get('type')}")
        output_id = native.string(item.get("id"), "completed output ID")
        call_id = self.output_ids.get(output_id)
        if (
            call_id is None
            or call_id not in self.calls
            or item.get("name") != self.calls[call_id].name
        ):
            raise PreservationError(
                "Codex completed output has no unambiguous matched model output ID/name"
            )
        if call_id not in self.open_tools:
            self.begin_tool({"type": "item_completed", "call_id": call_id}, row, index)
        result = ToolResult(
            call_id, _blocks(item.get("output"), self.assets), self.errors.get(call_id, False)
        )
        self.history.append(
            _message(self.identity, f"visible:{index}:result", "tool", (result,), row, self.turn)
        )
        self.open_tools.remove(call_id)

    def event(self, payload: dict[str, Any], row: dict[str, Any], index: int) -> None:
        event = payload.get("type")
        if event in ("task_started", "turn_started"):
            if self.turn is not None:
                raise PreservationError("Incomplete or nested Codex turn")
            self.turn = native.string(payload.get("turn_id"), "turn ID")
        elif event in ("task_complete", "turn_complete"):
            if self.turn != payload.get("turn_id") or payload.get("error"):
                raise PreservationError("Incomplete or mismatched Codex turn completion")
            if self.open_tools:
                raise PreservationError("Incomplete Codex native tool at task completion")
            self.turn = None
        elif event in ("exec_command_begin", "patch_apply_begin"):
            self.begin_tool(payload, row, index)
        elif event in ("exec_command_end", "patch_apply_end"):
            self.end_tool(payload, row, index)
        elif event == "item_completed":
            self.completed_item(payload, row, index)
        elif event == "mcp_tool_call_end":
            self.mcp(payload, row, index)
        elif event not in IGNORED_EVENTS:
            item = _event(payload, row, f"visible:{index}", self)
            if item:
                _append(self.history, item, row, self.groups)

    def finish_checkpoint(self) -> None:
        if self.checkpoints and self.checkpoints[-1].following_context is not None:
            checkpoint = self.checkpoints[-1]
            prefix = len(checkpoint.summary) + len(
                checkpoint.retained_context
                if checkpoint.retained_context is not None
                else checkpoint.retained_message_ids
            )
            self.checkpoints[-1] = replace(
                checkpoint, following_context=native.linked(self.context[prefix:])
            )

    def checkpoint(self, payload: dict[str, Any], row: dict[str, Any], index: int) -> None:
        self.finish_checkpoint()
        if not self.history or not payload.get("replacement_history"):
            raise PreservationError(
                "Codex compaction requires historical boundary and replacement_history"
            )
        rebuilt: list[Message] = [self.base] if self.base else []
        groups: dict[str, tuple[int, int]] = {}
        for offset, raw in enumerate(_array(payload["replacement_history"])):
            response = _object(raw)
            item_row = {
                **row,
                "timestamp": response.get("ringleader_timestamp", row.get("timestamp")),
            }
            item = _response(response, item_row, f"compact:{index}:{offset}", self)
            if item:
                _append(rebuilt, item, response, groups)
        if not rebuilt or any(remaining for _, remaining in groups.values()):
            raise PreservationError("Incomplete Codex replacement_history")
        checkpoint = _restore_checkpoint(payload, self.history, rebuilt, index)
        self.context, self.context_groups = rebuilt, groups
        self.checkpoints.append(checkpoint)

    def consume(self, row: dict[str, Any], index: int) -> None:
        kind, payload = row.get("type"), _object(row.get("payload"))
        if kind == "response_item":
            item = _response(payload, row, f"model:{index}", self)
            if item:
                _append(self.context, item, row, self.context_groups)
        elif kind == "event_msg":
            self.event(payload, row, index)
        elif kind == "compacted":
            self.checkpoint(payload, row, index)
        elif kind == "turn_context":
            if payload.get("developer_instructions") or payload.get("user_instructions"):
                raise PreservationError(
                    "Codex turn_context authored instructions require explicit response_item history"
                )
        elif kind not in IGNORED_ROWS:
            raise PreservationError(f"Unsupported Codex rollout record: {kind}")


def _restore_checkpoint(
    payload: dict[str, Any], history: list[Message], rebuilt: list[Message], index: int
) -> Checkpoint:
    metadata = payload.get("ringleader_checkpoint")
    if metadata is None:
        return Checkpoint(
            f"checkpoint:{index}", history[-1].id, native.linked(rebuilt), following_context=()
        )
    metadata = _object(metadata)
    count, retained_ids = metadata.get("summary_count"), metadata.get("retained_ids")
    if (
        type(count) is not int
        or not 0 < count <= len(rebuilt)
        or not isinstance(retained_ids, list)
        or type(metadata.get("owned_retained")) is not bool
    ):
        raise PreservationError("Invalid Codex checkpoint annotation")
    summary, retained = native.linked(rebuilt[:count]), tuple(rebuilt[count:])
    if metadata["owned_retained"]:
        if retained_ids:
            raise PreservationError("Codex checkpoint mixes retained references and context")
        owned = native.linked(retained)
    else:
        by_id = {item.id: item for item in history}
        if any(key not in by_id for key in retained_ids):
            raise PreservationError("Unknown Codex checkpoint history reference")
        expected = tuple(by_id[key] for key in retained_ids)
        if tuple(replace(m, id="", parent_id=None) for m in retained) != tuple(
            replace(m, id="", parent_id=None) for m in expected
        ):
            raise PreservationError(
                "Codex checkpoint retained content disagrees with native history"
            )
        rebuilt[count:] = expected
        owned = None
    return Checkpoint(
        f"checkpoint:{index}",
        history[-1].id,
        summary,
        tuple(retained_ids),
        owned,
        () if metadata.get("following_explicit") else None,
    )


def _scan_tool_metadata(row: dict[str, Any], state: _Reader) -> None:
    payload = _object(row.get("payload"))
    kind = payload.get("type")
    if (
        row.get("type") == "response_item"
        and kind in ("function_call_output", "custom_tool_call_output")
        and payload.get("id")
    ):
        output_id = native.string(payload["id"], "model output ID")
        call_id = native.string(payload.get("call_id"), "model output call ID")
        if output_id in state.output_ids:
            raise PreservationError("Duplicate Codex model output ID")
        state.output_ids[output_id] = call_id
    if row.get("type") != "event_msg":
        return
    if kind == "mcp_tool_call_end":
        tool_result = _object(payload.get("result"))
        body = _object(tool_result["Ok"]) if "Ok" in tool_result else {}
        error = "Err" in tool_result or body.get("isError", False)
    elif kind == "exec_command_end":
        _, error = _execution_output(payload)
    elif kind == "patch_apply_end":
        error = not payload.get("success")
    else:
        return
    state.errors[native.string(payload.get("call_id"), "native tool call ID")] = error


def read_rollout(data: bytes, identity: Identity, branch_id: str | None = None) -> Conversation:
    rows = native.records(data)
    meta = _object(rows[0].get("payload"))
    if rows[0].get("type") != "session_meta" or meta.get("id") != identity.native_id:
        raise PreservationError("Codex root session identity mismatch")
    if (
        meta.get("history_mode", "legacy") != "legacy"
        or meta.get("history_base")
        or meta.get("subagent_history_start_ordinal") is not None
    ):
        raise PreservationError(
            "Codex paginated/inherited history requires an unsupported reader profile"
        )
    if meta.get("session_id", identity.native_id) != identity.native_id:
        raise PreservationError(
            "Codex inherited root session identity requires another reader profile"
        )
    if branch_id not in (None, identity.native_id):
        raise PreservationError("Codex legacy rollout has no selectable branch")
    state = _Reader(identity)
    base = meta.get("base_instructions")
    if base is not None:
        text = native.string(_object(base).get("text"), "base instructions")
        if text:
            provenance = Provenance(
                "native",
                identity,
                "session_meta.base_instructions",
                "SessionMeta lacks author/default provenance; preserved as historical context",
            )
            state.base = Message(
                "base-instructions",
                "system",
                (Instruction(text, "system", provenance),),
                provenance,
            )
            state.context.append(state.base)
    for row in rows[1:]:
        _scan_tool_metadata(row, state)
    for index, row in enumerate(rows[1:], 1):
        if row.get("type") == "response_item":
            item = _response(_object(row.get("payload")), row, f"model:{index}", state)
            if item:
                state.calls.update(
                    (part.call_id, part) for part in item.content if isinstance(part, ToolCall)
                )
    for index, row in enumerate(rows[1:], 1):
        state.consume(row, index)
    if (
        state.turn is not None
        or state.tool_updates
        or state.open_tools
        or any(
            remaining for _, remaining in (*state.groups.values(), *state.context_groups.values())
        )
    ):
        raise PreservationError("Incomplete Codex turn or grouped message")
    if not state.history and state.context:
        raise PreservationError(
            "Codex model-only rollout has no independently preserved visible history"
        )
    state.finish_checkpoint()
    provenance = Provenance("native", identity, "session_meta")
    result = Conversation(
        identity,
        identity.native_id,
        native.linked(state.history),
        native.linked(state.context),
        tuple(state.checkpoints),
        tuple(state.assets),
        meta.get("ringleader_title"),
        source_created_at=native.timestamp(meta.get("timestamp"), provenance),
        source_updated_at=native.timestamp(rows[-1].get("timestamp"), provenance),
    )
    result = native.restore_session(result, meta.get("ringleader"))
    validate(result)
    return result


def _encoded_blocks(
    content: tuple[Content, ...], assets: dict[str, Attachment], *, assistant: bool = False
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, Text | Instruction | ToolOmission):
            result.append(
                {
                    "type": "output_text" if assistant else "input_text",
                    "text": part.text if isinstance(part, Text) else native.note(part),
                }
            )
        elif isinstance(part, AttachmentRef):
            if assistant:
                raise PreservationError("Codex assistant images are unsupported")
            asset = assets[part.attachment_id]
            if asset.media_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                raise PreservationError(
                    f"Unsupported Codex attachment media type: {asset.media_type}"
                )
            result.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{asset.media_type};base64,{base64.b64encode(asset.data).decode()}",
                    "ringleaderName": asset.name,
                }
            )
        else:
            raise PreservationError("Unsupported nested Codex model content")
    return result


def _model_parts(
    message: Message, assets: dict[str, Attachment], calls: dict[str, ToolCall]
) -> list[dict[str, Any]]:
    role = native.native_role(message)
    result: list[dict[str, Any]] = []
    prose: list[Content] = []

    def flush() -> None:
        if prose:
            result.append(
                {
                    "type": "message",
                    "role": role,
                    "content": _encoded_blocks(tuple(prose), assets, assistant=role == "assistant"),
                }
            )
            prose.clear()

    for part in message.content:
        if isinstance(part, ToolCall):
            flush()
            result.append(
                {
                    "type": "function_call"
                    if part.argument_format == "json"
                    else "custom_tool_call",
                    "call_id": part.call_id,
                    "name": part.name,
                    "arguments" if part.argument_format == "json" else "input": part.arguments,
                }
            )
        elif isinstance(part, ToolResult):
            flush()
            call = calls[part.call_id]
            result.append(
                {
                    "type": "function_call_output"
                    if call.argument_format == "json"
                    else "custom_tool_call_output",
                    "call_id": part.call_id,
                    "output": [
                        {"type": "input_text", "text": _result_status(part.is_error)},
                        *_encoded_blocks(part.output, assets),
                    ],
                    "ringleader_result_status": part.is_error,
                }
            )
        else:
            prose.append(part)
    flush()
    if not result:
        raise PreservationError("Codex empty canonical messages are unsupported")
    return result


def _tool_calls(messages: tuple[Message, ...]) -> dict[str, ToolCall]:
    return {
        part.call_id: part
        for message in messages
        for part in message.content
        if isinstance(part, ToolCall)
    }


def _mcp_payload(
    call: ToolCall, result: ToolResult, assets: dict[str, Attachment], projection: str
) -> dict[str, Any]:
    output = []
    for block in _encoded_blocks(result.output, assets):
        if block["type"] == "input_text":
            output.append({"type": "text", "text": block["text"]})
        else:
            mime, data = block["image_url"][5:].split(";base64,", 1)
            output.append(
                {
                    "type": "image",
                    "mimeType": mime,
                    "data": data,
                    "ringleaderName": block["ringleaderName"],
                }
            )
    return {
        "type": "mcp_tool_call_end",
        "call_id": call.call_id,
        "invocation": {
            "server": "ringleader_history",
            "tool": call.name,
            "arguments": json.loads(call.arguments)
            if call.argument_format == "json"
            else call.arguments,
        },
        "duration": {"secs": 0, "nanos": 0},
        "result": {"Ok": {"content": output, "isError": result.is_error}},
        "ringleader_projection": projection,
    }


def _visible_parts(
    message: Message,
    assets: dict[str, Attachment],
    calls: dict[str, ToolCall],
    results: dict[str, ToolResult],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    role = native.native_role(message)
    for part in message.content:
        if isinstance(part, ToolCall):
            result.append(_mcp_payload(part, results[part.call_id], assets, "call"))
        elif isinstance(part, ToolResult):
            result.append(_mcp_payload(calls[part.call_id], part, assets, "result"))
        elif isinstance(part, AttachmentRef):
            if role != "user" or not result or result[-1]["type"] != "user_message":
                raise PreservationError("Codex visible images require a preceding user text block")
            block = _encoded_blocks((part,), assets)[0]
            result[-1]["images"].append(block["image_url"])
            result[-1]["ringleaderImageNames"].append(block["ringleaderName"])
        else:
            if role not in ("user", "assistant"):
                raise PreservationError(
                    "Codex visible prose requires a user/assistant or historical instruction role"
                )
            result.append(
                {
                    "type": "user_message" if role == "user" else "agent_message",
                    "message": part.text if isinstance(part, Text) else native.note(part),
                    "images": [],
                    "local_images": [],
                    "text_elements": [],
                    "ringleaderImageNames": [],
                }
            )
    if not result:
        raise PreservationError("Codex empty visible messages are unsupported")
    return result


def _group_rows(
    parts: list[dict[str, Any]], message: Message, kind: str, stamp: datetime, group_id: str
) -> list[dict[str, Any]]:
    rows = []
    for index, payload in enumerate(parts):
        row: dict[str, Any] = {
            "timestamp": stamp.isoformat(),
            "type": kind,
            "payload": payload,
            "ringleader_group": {"id": group_id, "index": index, "count": len(parts)},
        }
        if index == len(parts) - 1:
            row["ringleader"] = native.message_annotations(message)
        rows.append(row)
    return rows


def _checkpoint_payload(
    checkpoint: Checkpoint,
    by_id: dict[str, Message],
    assets: dict[str, Attachment],
    now: datetime,
    mapping: dict[str, str],
) -> dict[str, Any]:
    retained = (
        checkpoint.retained_context
        if checkpoint.retained_context is not None
        else tuple(by_id[key] for key in checkpoint.retained_message_ids)
    )
    calls = _tool_calls((*checkpoint.summary, *retained))
    replacement = []
    for message in (*checkpoint.summary, *retained):
        rows = _group_rows(
            _model_parts(message, assets, calls),
            message,
            "response_item",
            native.historical_time(message, now),
            str(uuid4()),
        )
        for row in rows:
            replacement.append(
                {
                    **row["payload"],
                    "ringleader_group": row["ringleader_group"],
                    **({"ringleader": row["ringleader"]} if "ringleader" in row else {}),
                    "ringleader_timestamp": row["timestamp"],
                }
            )
    return {
        "message": "",
        "replacement_history": replacement,
        "ringleader_checkpoint": {
            "summary_count": len(checkpoint.summary),
            "retained_ids": [mapping[key] for key in checkpoint.retained_message_ids],
            "owned_retained": checkpoint.retained_context is not None,
            "following_explicit": checkpoint.following_context is not None,
        },
    }


def _turn_starts(conversation: Conversation) -> set[str]:
    starts: set[str] = set()
    previous: str | None = None
    pending: set[str] = set()
    for message in conversation.history:
        evidence = message.provenance.evidence
        source_turn = (
            evidence.split("\n", 1)[0]
            if message.provenance.source.agent == "codex" and evidence.startswith("codex-turn:")
            else None
        )
        user_input = message.role == "user" and not all(
            isinstance(part, ToolResult) for part in message.content
        )
        if not starts or (source_turn != previous if source_turn else user_input):
            if pending:
                raise PreservationError(
                    "Codex historical tool exchange crosses a native turn boundary"
                )
            starts.add(message.id)
        for part in message.content:
            if isinstance(part, ToolCall):
                pending.add(part.call_id)
            elif isinstance(part, ToolResult):
                pending.discard(part.call_id)
        previous = source_turn
    if not starts:
        raise PreservationError("Codex target requires visible conversation history")
    return starts


class CodexAdapter:
    def __init__(self, executable: str = "codex") -> None:
        self.executable = executable

    def inspect_source(self, request: SourceRequest) -> Compatibility:
        try:
            self.snapshot(request)
            return Compatibility(True, PROFILE, ())
        except (PreservationError, OSError) as exc:
            return Compatibility(False, None, (str(exc),))

    def snapshot(self, request: SourceRequest) -> SourceSnapshot:
        if request.identity.agent != "codex":
            raise PreservationError("Codex source requires codex identity")
        path = native.source_path(request.source_ref, request.roots)
        files = snapshot_files((("rollout", path),), max_bytes=native.MAX_BYTES)
        conversation = read_rollout(files[0].data, request.identity, request.branch_id)
        return SourceSnapshot(conversation, files, PROFILE.name)

    def inspect_target(self, roots: NativeRoots, runner: Runner) -> Compatibility:
        result = native.probe_version(self.executable, "codex", runner)
        version = result.stdout.decode().strip()
        if result.returncode or version != "codex-cli 0.155.1":
            return Compatibility(
                False, None, (f"Unsupported Codex writer version: {version}",), version
            )
        try:
            schema = codex_state.inspect_database(roots)
        except (PreservationError, OSError) as exc:
            return Compatibility(False, None, (str(exc),), version)
        return Compatibility(True, PROFILE, (), version, schema)

    def stage(self, conversation: Conversation, request: TargetRequest) -> NativeStage:
        validate(conversation)
        if request.profile != PROFILE or request.identity.agent != "codex":
            raise PreservationError("Unsupported Codex target profile")
        try:
            UUID(request.identity.native_id)
        except ValueError as exc:
            raise PreservationError("Codex target ID must be a UUID") from exc
        if request.identity == conversation.source:
            raise PreservationError("Codex target must have a fresh identity")
        schema = codex_state.inspect_database(request.roots)
        now = datetime.now(UTC)
        target = request.identity.native_id
        ref = now.strftime("sessions/%Y/%m/%d/rollout-%Y-%m-%dT%H-%M-%S-") + target + ".jsonl"
        meta = {
            "id": target,
            "session_id": target,
            "timestamp": now.isoformat(),
            "cwd": str(request.roots.cwd),
            "originator": "ringleader",
            "cli_version": "0.155.1",
            "source": "cli",
            "history_mode": "legacy",
            "model_provider": "openai",
            "base_instructions": None,
            "ringleader": native.session_metadata(conversation),
            "ringleader_title": request.title,
        }
        rows: list[dict[str, Any]] = [
            {"timestamp": now.isoformat(), "type": "session_meta", "payload": meta}
        ]
        assets = {item.id: item for item in conversation.attachments}
        visible_calls = _tool_calls(conversation.history)
        model_calls = _tool_calls(conversation.context)
        results = {
            part.call_id: part
            for message in conversation.history
            for part in message.content
            if isinstance(part, ToolResult)
        }
        turn_starts = _turn_starts(conversation)
        mapping = {message.id: str(uuid4()) for message in conversation.history}
        by_id = {message.id: message for message in conversation.history}
        boundaries = {
            checkpoint.after_message_id: checkpoint for checkpoint in conversation.checkpoints
        }
        if len(boundaries) != len(conversation.checkpoints):
            raise PreservationError("Codex multiple checkpoints at one boundary are unsupported")
        turn: str | None = None
        last_stamp = now
        explicit_suffix = False
        for message in conversation.history:
            stamp = native.historical_time(message, now)
            if message.id in turn_starts:
                if turn is not None:
                    rows.append(
                        {
                            "timestamp": last_stamp.isoformat(),
                            "type": "event_msg",
                            "payload": {
                                "type": "task_complete",
                                "turn_id": turn,
                                "last_agent_message": None,
                                "completed_at": int(last_stamp.timestamp()),
                            },
                        }
                    )
                turn = str(uuid4())
                rows.append(
                    {
                        "timestamp": stamp.isoformat(),
                        "type": "event_msg",
                        "payload": {
                            "type": "task_started",
                            "turn_id": turn,
                            "model_context_window": None,
                            "started_at": int(stamp.timestamp()),
                            "collaboration_mode_kind": "default",
                        },
                    }
                )
            rows.extend(
                _group_rows(
                    _visible_parts(message, assets, visible_calls, results),
                    message,
                    "event_msg",
                    stamp,
                    mapping[message.id],
                )
            )
            if conversation.checkpoints and not explicit_suffix:
                rows.extend(
                    _group_rows(
                        _model_parts(message, assets, visible_calls),
                        message,
                        "response_item",
                        stamp,
                        mapping[message.id],
                    )
                )
            if message.id in boundaries:
                rows.append(
                    {
                        "timestamp": stamp.isoformat(),
                        "type": "compacted",
                        "payload": _checkpoint_payload(
                            boundaries[message.id], by_id, assets, now, mapping
                        ),
                    }
                )
                checkpoint = boundaries[message.id]
                explicit_suffix = checkpoint.following_context is not None
                following_calls = _tool_calls(checkpoint.following_context or ())
                for following in checkpoint.following_context or ():
                    rows.extend(
                        _group_rows(
                            _model_parts(following, assets, following_calls),
                            following,
                            "response_item",
                            native.historical_time(following, now),
                            str(uuid4()),
                        )
                    )
            last_stamp = stamp
        if turn:
            rows.append(
                {
                    "timestamp": last_stamp.isoformat(),
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": turn,
                        "last_agent_message": None,
                        "completed_at": int(last_stamp.timestamp()),
                    },
                }
            )
        if not conversation.checkpoints:
            for message in conversation.context:
                rows.extend(
                    _group_rows(
                        _model_parts(message, assets, model_calls),
                        message,
                        "response_item",
                        native.historical_time(message, now),
                        str(uuid4()),
                    )
                )
        preview = next(
            (
                part.text
                for message in conversation.history
                if message.role == "user"
                for part in message.content
                if isinstance(part, Text)
            ),
            request.title,
        )
        epoch = int(now.timestamp())
        payload = {
            "id": target,
            "rollout_path": str(request.roots.store / ref),
            "created_at": epoch,
            "updated_at": epoch,
            "created_at_ms": int(now.timestamp() * 1000),
            "updated_at_ms": int(now.timestamp() * 1000),
            "recency_at": epoch,
            "recency_at_ms": int(now.timestamp() * 1000),
            "source": "cli",
            "model_provider": "openai",
            "cwd": str(request.roots.cwd),
            "title": request.title,
            "name": request.title,
            "preview": preview,
            "first_user_message": preview,
            "sandbox_policy": '{"type":"read-only"}',
            "approval_mode": "on-request",
            "has_user_event": 1,
            "history_mode": "legacy",
            "cli_version": "0.155.1",
            "tokens_used": 0,
            "originator": "ringleader",
        }
        return NativeStage(
            request.identity,
            PROFILE,
            request.roots,
            (NativeFile(ref, native.jsonl(rows), visibility=True),),
            ref,
            json.dumps(payload, separators=(",", ":")).encode(),
            schema,
        )

    def stage_metadata(self, stage: NativeStage) -> NativeMetadata:
        rows = native.records(
            next(item.data for item in stage.files if item.relative_path == stage.source_ref)
        )
        meta = rows[0]["payload"]
        payload = json.loads(stage.database_payload or "null")
        if not isinstance(payload, dict) or (payload["id"], payload["cwd"], payload["name"]) != (
            meta["id"],
            meta["cwd"],
            meta["ringleader_title"],
        ):
            raise PreservationError("Codex staged picker metadata disagrees with rollout")
        return NativeMetadata(Identity("codex", meta["id"]), meta["cwd"], meta["ringleader_title"])

    def read_stage(self, stage: NativeStage) -> Conversation:
        item = next(item for item in stage.files if item.relative_path == stage.source_ref)
        return read_rollout(item.data, stage.identity)

    def normalizations(self, profile: FormatProfile) -> tuple[Normalization, ...]:
        if profile != PROFILE:
            raise PreservationError("Unsupported Codex normalization profile")
        return (
            Normalization(
                "json-argument-whitespace",
                PROFILE.name,
                "Native dynamic tool arguments use JSON values",
            ),
        )

    def launch_request(self, identity: Identity, roots: NativeRoots) -> RunRequest:
        native.safe_id(identity, "codex")
        return native.run_request(self.executable, ("resume", identity.native_id), roots, "codex")

    def registration_status(
        self, stage: NativeStage
    ) -> Literal["absent", "owned", "archived", "conflict"]:
        return codex_state.registration_status(stage)

    def register_stage(self, stage: NativeStage, *, recover: bool = False) -> bool:
        return codex_state.registration(stage, recover=recover)

    def append_name(self, stage: NativeStage) -> bool:
        return codex_state.append_name(stage)

    def index_intent(self, stage: NativeStage) -> NativeIndexIntent:
        return NativeIndexIntent("session_index.jsonl", codex_state.name_intent(stage))

    def append_index(self, stage: NativeStage) -> bool:
        return self.append_name(stage)
