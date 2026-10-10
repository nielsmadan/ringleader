from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from . import native
from .codex_items import (
    as_array,
    as_object,
    base_message,
    check_metadata,
    compaction_message,
    image,
    message,
    response,
)
from .model import (
    Attachment,
    Checkpoint,
    Content,
    Conversation,
    Identity,
    Message,
    PreservationError,
    Provenance,
    Text,
    ToolCall,
    ToolResult,
    validate,
)

IGNORED_ROWS = frozenset(
    {
        "turn_context",
        "world_state",
        "inter_agent_communication_metadata",
        "token_usage_record",
        "security_risk_score",
        "realtime_item",
    }
)
IGNORED_EVENTS = frozenset({"token_count", "thread_settings_applied", "thread_goal_updated"})
COMPACTED_FIELDS = frozenset(
    {
        "message",
        "replacement_history",
        "replacement_history_metadata",
        "window_number",
        "first_window_id",
        "previous_window_id",
        "window_id",
        "compaction_response_id",
        "guardian_history",
        "latest_token_usage_record",
        "retained_context",
        "resume_metadata",
    }
)
SUMMARY_LIMIT = 160


def _line(value: Any) -> str:
    text = " ".join(str(value).split("\n", 1)[0].split())
    return text if len(text) <= SUMMARY_LIMIT else text[: SUMMARY_LIMIT - 1] + "…"


def _command(item: dict[str, Any]) -> str:
    command = item.get("command")
    if isinstance(command, list):
        return shlex.join(native.string(part, "command part") for part in command)
    return native.string(command, "command")


def _query(item: dict[str, Any]) -> str:
    return f" {_line(item['query'])}" if isinstance(item.get("query"), str) else ""


DESCRIPTORS: dict[str, Callable[[dict[str, Any]], str]] = {
    "CommandExecution": lambda item: _line(_command(item)),
    "FileChange": lambda item: _line(", ".join(sorted(as_object(item.get("changes"))))),
    "McpToolCall": lambda item: (
        f"{native.string(item.get('server'), 'MCP server')}."
        f"{native.string(item.get('tool'), 'MCP tool')}"
    ),
    "CollabAgentToolCall": lambda item: native.string(item.get("tool"), "collab tool"),
    "SubAgentActivity": lambda item: (
        f"{native.string(item.get('kind'), 'activity')} "
        f"{native.string(item.get('agent_path'), 'agent path')}"
    ),
    "Extension": lambda item: native.string(item.get("kind"), "extension kind") + _query(item),
    "WebSearch": lambda item: _line(native.string(item.get("query"), "web search query")),
    "ImageView": lambda item: _line(native.string(item.get("path"), "image path")),
    "Plan": lambda item: _line(native.string(item.get("text"), "plan")),
    "ContextCompaction": lambda item: "",
}


ERROR_STATUSES: dict[str, frozenset[str]] = {
    "CommandExecution": frozenset({"failed", "declined"}),
    "FileChange": frozenset({"failed", "declined"}),
    "McpToolCall": frozenset({"failed"}),
}


def _item_error(item: dict[str, Any]) -> bool | None:
    statuses = ERROR_STATUSES.get(native.string(item.get("type"), "turn item type"))
    if statuses is None:
        return None
    status = item.get("status")
    return isinstance(status, str) and status in statuses


def visible_errors(rows: list[dict[str, Any]]) -> dict[str, bool]:
    errors: dict[str, bool] = {}
    for row in rows:
        payload = row.get("payload")
        if (
            row.get("type") != "event_msg"
            or not isinstance(payload, dict)
            or payload.get("type") != "item_completed"
        ):
            continue
        item = as_object(payload.get("item"))
        error = _item_error(item)
        if error is not None:
            errors[native.string(item.get("id"), "turn item ID")] = error
    return errors


def _summary(item: dict[str, Any]) -> str | None:
    kind = native.string(item.get("type"), "turn item type")
    if kind == "Reasoning":
        return None
    if kind not in DESCRIPTORS:
        raise PreservationError(f"Unsupported Codex turn item: {kind}")
    status = f" {item['status']}" if isinstance(item.get("status"), str) else ""
    return f"[{kind}{status}] {DESCRIPTORS[kind](item)}".rstrip()


def _user_content(item: dict[str, Any], assets: list[Attachment]) -> tuple[Content, ...]:
    content: list[Content] = []
    for raw in as_array(item.get("content")):
        block = as_object(raw)
        kind = block.get("type")
        if kind == "text":
            content.append(Text(native.string(block.get("text"), "user text")))
        elif kind == "image":
            content.append(image(block.get("image_url") or block.get("url"), assets))
        elif kind == "local_image":
            content.append(Text(f"[Image: {native.string(block.get('path'), 'image path')}]"))
        elif kind == "skill":
            content.append(Text(f"[Skill: {native.string(block.get('name'), 'skill name')}]"))
        else:
            raise PreservationError(f"Unsupported Codex user input: {kind}")
    return tuple(content)


def _agent_content(item: dict[str, Any]) -> tuple[Content, ...]:
    if set(item) - {"type", "id", "content", "phase", "delivery", "questions"}:
        raise PreservationError("Unsupported Codex agent message fields")
    content: list[Content] = []
    for raw in as_array(item.get("content")):
        block = as_object(raw)
        if block.get("type") != "Text":
            raise PreservationError(f"Unsupported Codex agent message content: {block.get('type')}")
        content.append(Text(native.string(block.get("text"), "agent text")))
    for raw in as_array(item.get("questions") or []):
        title = native.string(as_object(raw).get("title"), "question title")
        content.append(Text(f"[Question: {title}]"))
    return tuple(content)


def visible_item(
    item: dict[str, Any],
    row: dict[str, Any],
    key: str,
    identity: Identity,
    assets: list[Attachment],
    turn: str | None,
) -> Message | None:
    kind = item.get("type")
    if kind == "UserMessage":
        return message(identity, key, "user", _user_content(item, assets), row, turn)
    if kind == "AgentMessage":
        return message(identity, key, "assistant", _agent_content(item), row, turn)
    summary = _summary(item)
    if summary is None:
        return None
    evidence = f"codex-turn:{turn}\n" if turn else ""
    provenance = Provenance("harness", identity, key, evidence + f"codex turn item: {kind}")
    return Message(
        key,
        "assistant",
        (Text(summary),),
        provenance,
        timestamp=native.timestamp(row.get("timestamp"), provenance),
    )


def boundary(item: Message) -> bool:
    return item.role == "user" and (
        item.provenance.origin == "authored" or item.provenance.evidence == "codex agent_message"
    )


def rollback_turns(
    history: list[Message], context: list[Message], turns: list[int], count: int
) -> tuple[list[Message], list[Message], list[int]]:
    if not count:
        return history, context, turns
    history = history[: turns[max(len(turns) - count, 0)]] if turns else history
    turns = turns[: max(len(turns) - count, 0)]
    boundaries = [index for index, item in enumerate(context) if boundary(item)]
    if boundaries:
        cut = boundaries[max(len(boundaries) - count, 0)]
        floor = boundaries[0]
        while (
            cut > floor
            and context[cut - 1].hidden
            and context[cut - 1].provenance.origin == "harness"
        ):
            cut -= 1
        context = context[:cut]
    return history, context, turns


def compaction_entries(
    payload: dict[str, Any], items: list[dict[str, Any]], tolerated: frozenset[str]
) -> list[Any]:
    unknown = sorted(set(payload) - tolerated)
    if unknown:
        raise PreservationError(f"Unsupported Codex compaction field: {unknown[0]}")
    if payload.get("message"):
        raise PreservationError("Codex encrypted compaction with a plaintext summary")
    positions = [offset for offset, item in enumerate(items) if item.get("type") == "compaction"]
    if positions != [len(items) - 1]:
        raise PreservationError("Codex compaction item must close replacement_history")
    metadata = payload.get("replacement_history_metadata")
    entries = as_array(metadata) if metadata is not None else [None] * len(items)
    if len(entries) != len(items):
        raise PreservationError("Codex replacement_history_metadata disagrees with history")
    check_metadata(entries[-1])
    return entries


@dataclass(frozen=True)
class _Compaction:
    key: str
    after_message_id: str
    summary: Message
    retained: tuple[Message, ...]
    replaced: tuple[Message, ...]


def _same(left: tuple[Message, ...], right: list[Message]) -> bool:
    return len(left) == len(right) and all(
        (a.id, a.role, a.content, a.hidden) == (b.id, b.role, b.content, b.hidden)
        for a, b in zip(left, right, strict=True)
    )


@dataclass
class _Paginated:
    identity: Identity
    base: Message | None = None
    assets: list[Attachment] = field(default_factory=list)
    errors: dict[str, bool] = field(default_factory=dict)
    calls: dict[str, ToolCall] = field(default_factory=dict)
    turn: str | None = None
    history: list[Message] = field(default_factory=list)
    context: list[Message] = field(default_factory=list)
    full: list[Message] = field(default_factory=list)
    item_keys: dict[str, str] = field(default_factory=dict)
    turns: list[int] = field(default_factory=list)
    compaction: _Compaction | None = None

    def rollback(self, payload: dict[str, Any]) -> None:
        count = payload.get("num_turns")
        if type(count) is not int or count < 0:
            raise PreservationError("Invalid Codex thread rollback")
        if count and self.compaction is not None:
            raise PreservationError("Codex thread rollback after a compaction is unsupported")
        self.turn = None
        self.history, self.context, self.turns = rollback_turns(
            self.history, self.context, self.turns, count
        )
        self.full = rollback_turns([], self.full, [], count)[1]

    def retained(
        self,
        raw: dict[str, Any],
        row: dict[str, Any],
        key: str,
        earlier_by_id: dict[str, list[Message]],
    ) -> list[Message]:
        original = self.item_keys.get(raw["id"]) if isinstance(raw.get("id"), str) else None
        if original is not None:
            parsed = response(raw, row, original, self)
            earlier = earlier_by_id.get(original, [])
            if _same(parsed, earlier):
                return earlier
        return list(response(raw, row, key, self))

    def compact(self, payload: dict[str, Any], row: dict[str, Any], index: int) -> None:
        items = [as_object(item) for item in as_array(payload.get("replacement_history") or [])]
        positions = [
            offset for offset, item in enumerate(items) if item.get("type") == "compaction"
        ]
        if not positions:
            raise PreservationError("Codex paginated compaction requires checkpoint support")
        entries = compaction_entries(payload, items, COMPACTED_FIELDS)
        if not self.history:
            raise PreservationError("Codex compaction requires a historical boundary")
        earlier_by_id: dict[str, list[Message]] = {}
        for item in self.full:
            earlier_by_id.setdefault(item.id.split(":harness")[0], []).append(item)
        retained: list[Message] = []
        for offset, (raw, entry) in enumerate(zip(items[:-1], entries[:-1], strict=True)):
            item_row = {"timestamp": row.get("timestamp"), "metadata": entry}
            parts = self.retained(raw, item_row, f"compact:{index}:{offset}", earlier_by_id)
            if {item.id for item in parts} & {item.id for item in retained}:
                parts = list(response(raw, item_row, f"compact:{index}:{offset}", self))
            retained.extend(parts)
        summary = compaction_message(items[-1], row, f"compact:{index}", self)
        prefix = [self.base] if self.base else []
        self.compaction = _Compaction(
            f"checkpoint:{index}",
            self.history[-1].id,
            summary,
            tuple(merge_calls(complete_tools(retained))),
            native.linked(prefix + merge_calls(complete_tools(self.full))),
        )
        self.context = []

    def event(self, payload: dict[str, Any], row: dict[str, Any], index: int) -> None:
        kind = payload.get("type")
        if kind in ("task_started", "turn_started"):
            self.turn = native.string(payload.get("turn_id"), "turn ID")
            self.turns.append(len(self.history))
        elif kind in ("task_complete", "turn_complete", "turn_aborted"):
            self.turn = None
        elif kind == "item_completed":
            turn = payload.get("turn_id", self.turn)
            item = visible_item(
                as_object(payload.get("item")),
                row,
                f"visible:{index}",
                self.identity,
                self.assets,
                turn if isinstance(turn, str) else None,
            )
            if item is not None:
                self.history.append(item)
        elif kind == "thread_rolled_back":
            self.rollback(payload)
        elif kind not in IGNORED_EVENTS:
            raise PreservationError(f"Unsupported Codex paginated event: {kind}")

    def consume(self, row: dict[str, Any], index: int) -> None:
        kind, payload = row.get("type"), as_object(row.get("payload"))
        if kind == "response_item":
            items = response(payload, row, f"model:{index}", self)
            for item in items:
                self.calls.update(
                    (part.call_id, part) for part in item.content if isinstance(part, ToolCall)
                )
            if isinstance(payload.get("id"), str):
                self.item_keys.setdefault(payload["id"], f"model:{index}")
            self.context.extend(items)
            self.full.extend(items)
        elif kind == "event_msg":
            self.event(payload, row, index)
        elif kind == "compacted":
            self.compact(payload, row, index)
        elif kind not in IGNORED_ROWS:
            raise PreservationError(f"Unsupported Codex rollout record: {kind}")


def _aborted(call: Message, part: ToolCall) -> Message:
    provenance = Provenance(
        "harness",
        call.provenance.source,
        call.id + ":aborted",
        "Codex resume inserts an aborted output for a call without one",
    )
    return Message(
        call.id + ":aborted",
        "tool",
        (ToolResult(part.call_id, (Text("aborted"),), False),),
        provenance,
        timestamp=call.timestamp,
    )


def complete_tools(messages: list[Message]) -> list[Message]:
    calls = {
        part.call_id for item in messages for part in item.content if isinstance(part, ToolCall)
    }
    results = {
        part.call_id for item in messages for part in item.content if isinstance(part, ToolResult)
    }
    output: list[Message] = []
    for item in messages:
        kept = tuple(
            part
            for part in item.content
            if not isinstance(part, ToolResult) or part.call_id in calls
        )
        if kept or not item.content:
            output.append(replace(item, content=kept))
        output.extend(
            _aborted(item, part)
            for part in item.content
            if isinstance(part, ToolCall) and part.call_id not in results
        )
    return output


def _joins(previous: Message, item: Message) -> bool:
    return (
        previous.role == item.role == "assistant"
        and bool(previous.content)
        and isinstance(previous.content[-1], ToolCall)
        and bool(item.content)
        and all(isinstance(part, ToolCall) for part in item.content)
    )


def merge_calls(messages: list[Message]) -> list[Message]:
    output: list[Message] = []
    for item in messages:
        if output and _joins(output[-1], item):
            output[-1] = replace(output[-1], content=(*output[-1].content, *item.content))
        else:
            output.append(item)
    return output


def read_paginated(
    rows: list[dict[str, Any]], identity: Identity, meta: dict[str, Any]
) -> Conversation:
    base = base_message(meta, identity)
    state = _Paginated(identity, base, errors=visible_errors(rows[1:]))
    for index, row in enumerate(rows[1:], 1):
        state.consume(row, index)
    if not state.history and not state.full:
        raise PreservationError("Codex rollout has no conversation turns")
    if not state.history:
        raise PreservationError(
            "Codex model-only rollout has no independently preserved visible history"
        )
    prefix = [base] if base else []
    following = merge_calls(complete_tools(state.context))
    checkpoints: tuple[Checkpoint, ...] = ()
    if state.compaction is None:
        context = prefix + following
    else:
        found = state.compaction
        summary = [*prefix, found.summary]
        context = [*summary, *found.retained, *following]
        checkpoints = (
            Checkpoint(
                found.key,
                found.after_message_id,
                native.linked(summary),
                retained_context=native.linked(found.retained),
                following_context=native.linked(following),
                replaced_context=found.replaced,
            ),
        )
    provenance = Provenance("native", identity, "session_meta")
    result = Conversation(
        identity,
        identity.native_id,
        native.linked(state.history),
        native.linked(context),
        checkpoints,
        tuple(state.assets),
        None,
        source_created_at=native.timestamp(meta.get("timestamp"), provenance),
        source_updated_at=native.timestamp(rows[-1].get("timestamp"), provenance),
    )
    validate(result)
    return result
