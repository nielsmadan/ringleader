from __future__ import annotations

import base64
import json
import re
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
from .codex_items import (
    COMPACTION,
    DEFAULT_DETAIL,
    OPAQUE_KINDS,
    base_message,
    compaction_message,
    compaction_payload,
    is_base,
    is_compaction,
    phase_value,
    response,
)
from .codex_items import as_array as _array
from .codex_items import as_object as _object
from .codex_items import blocks as _blocks
from .codex_items import calls_of as _tool_calls
from .codex_items import image as _image
from .codex_items import message as _message
from .codex_items import result_status as _result_status
from .codex_paginated import (
    COMPACTED_FIELDS,
    compaction_entries,
    complete_tools,
    read_paginated,
    rollback_turns,
    visible_item,
)
from .codex_paginated import IGNORED_ROWS as PAGINATED_IGNORED_ROWS
from .compare import CheckpointShape, Normalization, flattened, flattened_checkpoint
from .model import (
    Attachment,
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
from .snapshot import SourceSnapshot, snapshot_files
from .verification import VerificationRecord, local_schemas, local_versions
from .versions import check_version

PINNED = ("0.155.1", "0.157.0")

PROFILE = FormatProfile(
    "codex",
    "codex-0.155.1-legacy",
    "legacy",
    (
        "https://github.com/openai/codex/tree/rust-v0.155.1/codex-rs/protocol",
        "https://github.com/openai/codex/blob/rust-v0.155.1/codex-rs/app-server-protocol/src/protocol/thread_history.rs",
    ),
)
FLATTENED_VIEW = Normalization(
    "model-view-history",
    PROFILE.name,
    "A flattened checkpoint is written as one stream: the model context, hidden messages as ordinary ones",
)
IGNORED_EVENTS = {
    "token_count",
    "agent_reasoning",
    "agent_reasoning_raw_content",
    "agent_reasoning_section_break",
    "context_compacted",
    "thread_settings_applied",
    "thread_goal_updated",
}


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
    full: list[Message] = field(default_factory=list)
    full_groups: dict[str, tuple[int, int]] = field(default_factory=dict)
    errors: dict[str, bool] = field(default_factory=dict)
    calls: dict[str, ToolCall] = field(default_factory=dict)
    turn: str | None = None
    turns: list[int] = field(default_factory=list)
    tool_updates: dict[str, tuple[ToolCall, ToolResult]] = field(default_factory=dict)
    base: Message | None = None
    open_tools: set[str] = field(default_factory=set)
    output_ids: dict[str, str] = field(default_factory=dict)

    def rollback(self, payload: dict[str, Any]) -> None:
        count = payload.get("num_turns")
        if type(count) is not int or count < 0:
            raise PreservationError("Invalid Codex thread rollback")
        self.turn = None
        self.history, self.context, self.turns = rollback_turns(
            self.history, self.context, self.turns, count
        )
        self.full = rollback_turns([], self.full, [], count)[1]

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
        if item.get("type") != "FunctionCallOutput":
            visible = visible_item(
                item, row, f"visible:{index}", self.identity, self.assets, self.turn
            )
            if visible is not None:
                self.history.append(visible)
            return
        if set(item) - {"type", "id", "name", "namespace", "output"}:
            raise PreservationError("Unsupported Codex completed function output fields")
        if item.get("namespace"):
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
            self.turns.append(len(self.history))
        elif event in ("task_complete", "turn_complete", "turn_aborted"):
            if self.turn != payload.get("turn_id") or (
                event != "turn_aborted" and payload.get("error")
            ):
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
        elif event == "thread_rolled_back":
            self.rollback(payload)
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
        items = [_object(raw) for raw in _array(payload["replacement_history"])]
        sealed = any(item.get("type") == "compaction" for item in items)
        entries = (
            compaction_entries(payload, items, COMPACTED_FIELDS | {"ringleader_checkpoint"})
            if sealed
            else [None] * len(items)
        )
        for offset, (raw_item, entry) in enumerate(zip(items, entries, strict=True)):
            item_row = {
                **row,
                "timestamp": raw_item.get("ringleader_timestamp", row.get("timestamp")),
                "metadata": entry,
            }
            key = f"compact:{index}:{offset}"
            parsed = (
                (compaction_message(raw_item, item_row, key, self),)
                if raw_item.get("type") == "compaction"
                else response(raw_item, item_row, key, self)
            )
            for item in parsed:
                _append(rebuilt, item, raw_item, groups)
        if not rebuilt or any(remaining for _, remaining in groups.values()):
            raise PreservationError("Incomplete Codex replacement_history")
        lead = 1 if self.base else 0
        rebuilt[lead:] = [
            *(item for item in rebuilt[lead:] if is_compaction(item)),
            *(item for item in rebuilt[lead:] if not is_compaction(item)),
        ]
        if sealed and payload.get("ringleader_checkpoint") is None:
            checkpoint = Checkpoint(
                f"checkpoint:{index}",
                self.history[-1].id,
                native.linked(rebuilt[: lead + 1]),
                retained_context=native.linked(rebuilt[lead + 1 :]),
                following_context=(),
            )
        else:
            checkpoint = _restore_checkpoint(payload, self.history, rebuilt, index)
        if any(is_compaction(item) for item in checkpoint.summary):
            prefix = [self.base] if self.base else []
            checkpoint = replace(
                checkpoint, replaced_context=native.linked(prefix + complete_tools(self.full))
            )
        self.context, self.context_groups = rebuilt, groups
        self.checkpoints.append(checkpoint)

    def consume(self, row: dict[str, Any], index: int) -> None:
        kind, payload = row.get("type"), _object(row.get("payload"))
        if kind == "response_item":
            for item in response(payload, row, f"model:{index}", self):
                _append(self.context, item, row, self.context_groups)
                _append(self.full, item, row, self.full_groups)
        elif kind == "event_msg":
            self.event(payload, row, index)
        elif kind == "compacted":
            self.checkpoint(payload, row, index)
        elif kind not in PAGINATED_IGNORED_ROWS:
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
    mode = meta.get("history_mode", "legacy")
    if mode not in ("legacy", "paginated"):
        raise PreservationError(f"Unsupported Codex history mode: {mode}")
    if meta.get("history_base") or meta.get("subagent_history_start_ordinal") is not None:
        raise PreservationError("Codex inherited (forked) history requires another reader profile")
    if meta.get("session_id", identity.native_id) != identity.native_id:
        raise PreservationError(
            "Codex inherited root session identity requires another reader profile"
        )
    if branch_id not in (None, identity.native_id):
        raise PreservationError("Codex rollout has no selectable branch")
    if mode == "paginated":
        paginated = read_paginated(rows, identity, meta)
        return replace(paginated, title=meta.get("ringleader_title"))
    state = _Reader(identity)
    state.base = base_message(meta, identity)
    if state.base is not None:
        state.context.append(state.base)
    for row in rows[1:]:
        _scan_tool_metadata(row, state)
    for index, row in enumerate(rows[1:], 1):
        if row.get("type") == "response_item":
            for item in response(_object(row.get("payload")), row, f"model:{index}", state):
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
                    "detail": DEFAULT_DETAIL,
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
    phase: list[str] = []

    def flush() -> None:
        if prose:
            payload: dict[str, Any] = {
                "type": "message",
                "role": role,
                "content": _encoded_blocks(tuple(prose), assets, assistant=role == "assistant"),
            }
            if phase:
                payload["phase"] = phase.pop()
            result.append(payload)
            prose.clear()

    if any(isinstance(part, Opaque) and part.kind == COMPACTION for part in message.content):
        if len(message.content) != 1 or role != "assistant":
            raise PreservationError("Codex compaction item must stand alone")
        return [compaction_payload(cast(Opaque, message.content[0]))]
    for part in message.content:
        if isinstance(part, Opaque):
            if phase or prose or result or role != "assistant":
                raise PreservationError(f"Unsupported opaque kind: {part.kind}")
            phase.append(phase_value(part))
        elif isinstance(part, ToolCall):
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
            kind = (
                "function_call_output"
                if call.argument_format == "json"
                else "custom_tool_call_output"
            )
            if message.provenance.origin == "harness":
                result.append(
                    {
                        "type": kind,
                        "call_id": part.call_id,
                        "output": _encoded_blocks(part.output, assets),
                    }
                )
            else:
                result.append(
                    {
                        "type": kind,
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


def _split_base(messages: tuple[Message, ...]) -> tuple[Message | None, tuple[Message, ...]]:
    if any(is_base(item) for item in messages[1:]):
        raise PreservationError("Codex base instructions must lead the model context")
    if messages and is_base(messages[0]):
        if len(messages[0].content) != 1:
            raise PreservationError("Codex base instructions must stand alone")
        return messages[0], messages[1:]
    return None, messages


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
        elif isinstance(part, Opaque):
            raise PreservationError(f"Unsupported opaque kind: {part.kind}")
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
    summary = _split_base(checkpoint.summary)[1]
    calls = _tool_calls((*summary, *retained))
    replacement = []
    compactions = tuple(item for item in summary if is_compaction(item))
    if compactions and compactions != summary:
        raise PreservationError("Codex compaction cannot share a summary with other messages")
    ordered = (*retained, *summary) if compactions else (*summary, *retained)
    for message in ordered:
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


def cli_version(output: str) -> str:
    match = re.fullmatch(r"codex-cli (\S+)", output.strip())
    return match.group(1) if match else output.strip()


def _session_meta(
    conversation: Conversation,
    request: TargetRequest,
    now: datetime,
    version: str,
    base: Message | None,
) -> dict[str, Any]:
    meta = {
        "id": request.identity.native_id,
        "session_id": request.identity.native_id,
        "timestamp": now.isoformat(),
        "cwd": str(request.roots.cwd),
        "originator": "ringleader",
        "cli_version": version,
        "source": "cli",
        "history_mode": "legacy",
        "model_provider": "openai",
        "base_instructions": json.loads(cast(Opaque, base.content[0]).payload) if base else None,
        "ringleader": native.session_metadata(conversation),
        "ringleader_title": request.title,
    }
    if base:
        meta["ringleader_base_instructions"] = native.message_annotations(base)
    return {"timestamp": now.isoformat(), "type": "session_meta", "payload": meta}


def _task_started(turn: str, stamp: datetime) -> dict[str, Any]:
    return {
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


def _task_complete(turn: str, stamp: datetime) -> dict[str, Any]:
    return {
        "timestamp": stamp.isoformat(),
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": turn,
            "last_agent_message": None,
            "completed_at": int(stamp.timestamp()),
        },
    }


def _context_rows(
    context: tuple[Message, ...], assets: dict[str, Attachment], now: datetime
) -> list[dict[str, Any]]:
    calls = _tool_calls(context)
    return [
        row
        for message in context
        for row in _group_rows(
            _model_parts(message, assets, calls),
            message,
            "response_item",
            native.historical_time(message, now),
            str(uuid4()),
        )
    ]


def _checkpoint_rows(
    checkpoint: Checkpoint,
    by_id: dict[str, Message],
    assets: dict[str, Attachment],
    now: datetime,
    mapping: dict[str, str],
    stamp: datetime,
) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": stamp.isoformat(),
            "type": "compacted",
            "payload": _checkpoint_payload(checkpoint, by_id, assets, now, mapping),
        },
        *_context_rows(checkpoint.following_context or (), assets, now),
    ]


class CodexAdapter:
    profile = PROFILE

    def __init__(
        self, executable: str = "codex", verified: tuple[VerificationRecord, ...] = ()
    ) -> None:
        self.executable = executable
        self.versions = local_versions(verified, "codex")
        self.local_schemas = local_schemas(verified, "codex")
        self.schemas = codex_state.ACCEPTED | frozenset(self.local_schemas.values())

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
        index = request.roots.store / "session_index.jsonl"
        for _ in range(3):
            present = index.exists()
            paths = (("rollout", path),) + ((("index", index),) if present else ())
            files = snapshot_files(paths, max_bytes=native.MAX_BYTES)
            if index.exists() == present:
                break
        else:
            raise PreservationError("Codex name index changed during snapshot")
        conversation = read_rollout(files[0].data, request.identity, request.branch_id)
        if present and files[1].data.strip():
            for row in native.records(files[1].data):
                if row.get("id") == request.identity.native_id and isinstance(
                    row.get("thread_name"), str
                ):
                    conversation = replace(conversation, title=row["thread_name"].strip() or None)
        return SourceSnapshot(conversation, (files[0],), PROFILE.name)

    def inspect_target(self, roots: NativeRoots, runner: Runner) -> Compatibility:
        result = native.probe_version(self.executable, "codex", runner)
        output = result.stdout.decode(errors="replace").strip()
        verdict = check_version(
            cli_version(output) if result.returncode == 0 else "", PINNED, self.versions
        )
        if not verdict.accepted or verdict.basis is None:
            return native.version_compatibility("Codex writer", PROFILE, verdict, output)
        expected = {**self.local_schemas, **codex_state.SCHEMAS}.get(verdict.basis)
        try:
            schema = codex_state.inspect_database(
                roots, frozenset({expected}) if expected else frozenset()
            )
        except (PreservationError, OSError) as exc:
            return Compatibility(False, None, (str(exc),), output)
        return native.version_compatibility("Codex writer", PROFILE, verdict, output, schema)

    def stage(self, conversation: Conversation, request: TargetRequest) -> NativeStage:
        validate(conversation)
        native.reject_opaque(conversation, OPAQUE_KINDS)
        if any(is_base(item) for item in conversation.history):
            raise PreservationError("Codex base instructions must lead the model context")
        if request.profile != PROFILE or request.identity.agent != "codex":
            raise PreservationError("Unsupported Codex target profile")
        try:
            UUID(request.identity.native_id)
        except ValueError as exc:
            raise PreservationError("Codex target ID must be a UUID") from exc
        if request.identity == conversation.source:
            raise PreservationError("Codex target must have a fresh identity")
        schema = codex_state.inspect_database(request.roots, self.schemas)
        version = cli_version(request.runtime_version) if request.runtime_version else PINNED[0]
        now = datetime.now(UTC)
        target = request.identity.native_id
        ref = now.strftime("sessions/%Y/%m/%d/rollout-%Y-%m-%dT%H-%M-%S-") + target + ".jsonl"
        applied = self.normalizations(PROFILE)
        try:
            rows, preview = self._rows(conversation, request, now, version)
        except CheckpointShape:
            conversation = flattened(conversation)
            rows, preview = self._rows(conversation, request, now, version)
            applied = (*applied, flattened_checkpoint(PROFILE.name), FLATTENED_VIEW)
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
            "cli_version": version,
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
            applied,
        )

    def _rows(
        self,
        conversation: Conversation,
        request: TargetRequest,
        now: datetime,
        version: str,
    ) -> tuple[list[dict[str, Any]], str]:
        base, context = _split_base(conversation.context)
        rows = [_session_meta(conversation, request, now, version, base)]
        assets = {item.id: item for item in conversation.attachments}
        visible_calls = _tool_calls(conversation.history)
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
            raise CheckpointShape("Codex multiple checkpoints at one boundary are unsupported")
        turn: str | None = None
        last_stamp = now
        explicit_suffix = False
        for message in conversation.history:
            stamp = native.historical_time(message, now)
            if message.id in turn_starts:
                if turn is not None:
                    rows.append(_task_complete(turn, last_stamp))
                turn = str(uuid4())
                rows.append(_task_started(turn, stamp))
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
                checkpoint = boundaries[message.id]
                rows.extend(_checkpoint_rows(checkpoint, by_id, assets, now, mapping, stamp))
                explicit_suffix = checkpoint.following_context is not None
            last_stamp = stamp
        if turn:
            rows.append(_task_complete(turn, last_stamp))
        if not conversation.checkpoints:
            rows.extend(_context_rows(context, assets, now))
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
        return rows, preview

    def stage_metadata(self, stage: NativeStage) -> NativeMetadata:
        meta = native.first_record(
            next(item.data for item in stage.files if item.relative_path == stage.source_ref)
        )["payload"]
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

    def legacy_normalizations(self, profile: FormatProfile) -> tuple[Normalization, ...]:
        # pre-wave-1 Codex always declared model-view-history; a journal written
        # then recorded no normalizations of its own, so it still needs this to
        # compare a stage that was flattened at write time
        return (*self.normalizations(profile), FLATTENED_VIEW)

    def launch_request(self, identity: Identity, roots: NativeRoots) -> RunRequest:
        native.safe_id(identity, "codex")
        return native.launch_request(
            self.executable, ("resume", identity.native_id), roots, "codex"
        )

    def registration_status(
        self, stage: NativeStage
    ) -> Literal["absent", "owned", "archived", "conflict"]:
        return codex_state.registration_status(stage, self.schemas)

    def register_stage(self, stage: NativeStage, *, recover: bool = False) -> bool:
        return codex_state.registration(stage, recover=recover, accepted=self.schemas)

    def append_name(self, stage: NativeStage) -> bool:
        return codex_state.append_name(stage, self.schemas)

    def index_intent(self, stage: NativeStage) -> NativeIndexIntent:
        return NativeIndexIntent("session_index.jsonl", codex_state.name_intent(stage))

    def append_index(self, stage: NativeStage) -> bool:
        return self.append_name(stage)
