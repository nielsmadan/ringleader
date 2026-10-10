from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from . import native
from .capabilities import (
    Compatibility,
    FormatProfile,
    NativeFile,
    NativeMetadata,
    NativeRoots,
    NativeStage,
    Runner,
    RunRequest,
    SourceRequest,
    TargetRequest,
)
from .compare import CheckpointShape, Normalization, flattened, flattened_checkpoint, model_view
from .model import (
    Attachment,
    AttachmentRef,
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
from .snapshot import SourceSnapshot, snapshot_files
from .verification import VerificationRecord, local_versions
from .versions import check_version

SESSION_VERSION = 3
PINNED = ("0.85.1", "0.87.1")

PROFILE = FormatProfile(
    "pi",
    "pi-0.85.1-v3",
    "3",
    (
        "@earendil-works/pi-coding-agent@0.85.1/dist/core/session-manager.js",
        "@earendil-works/pi-coding-agent@0.85.1/dist/core/messages.js",
    ),
)
SUMMARY_PREFIX = "The conversation history before this point was compacted into the following summary:\n\n<summary>\n"
SUMMARY_SUFFIX = "\n</summary>"
BRANCH_PREFIX = (
    "The following is a summary of a branch that this conversation came back from:\n\n<summary>\n"
)
IGNORED = {"custom", "model_change", "thinking_level_change", "label", "session_info", "usage"}
NO_RESULT = "No result provided"
ZERO_USAGE = {
    "input": 0,
    "output": 0,
    "cacheRead": 0,
    "cacheWrite": 0,
    "totalTokens": 0,
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
}


def _validate_stop_reason(reason: Any) -> None:
    if reason in ("aborted", "error", "pending"):
        raise PreservationError(
            f"Incomplete Pi assistant stopReason={reason!r}; native replay excludes or cannot resume this turn"
        )
    if reason not in ("stop", "length", "toolUse"):
        raise PreservationError(f"Unsupported Pi assistant stopReason: {reason!r}")


def _validate_replay(messages: tuple[Message, ...]) -> None:
    for message in messages:
        native.reject_assistant_images(message, "Pi")
    native.validate_replay(messages, "Pi")


def _tool_call_ids(content: Any) -> set[str]:
    if not isinstance(content, list):
        return set()
    return {
        block["id"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "toolCall"
        and isinstance(block.get("id"), str)
    }


def _later_tool_result_ids(path: list[dict[str, Any]], start: int) -> set[str]:
    ids: set[str] = set()
    for row in path[start:]:
        if row.get("type") != "message":
            continue
        data = row.get("message")
        if isinstance(data, dict) and data.get("role") == "toolResult":
            call_id = data.get("toolCallId")
            if isinstance(call_id, str):
                ids.add(call_id)
    return ids


ANSWERED_LATER = (
    "Pi tool call was interrupted and answered later in the session; cannot import safely"
)


def _no_result(call_id: str, identity: Identity, row: dict[str, Any]) -> Message:
    provenance = Provenance(
        "harness",
        identity,
        native.string(row.get("id"), "entry ID"),
        "pi transformMessages: tool call orphaned by an interrupted or dropped turn",
    )
    return Message(
        f"no-result-{call_id}",
        "tool",
        (ToolResult(call_id, (Text(NO_RESULT),), True),),
        provenance,
        timestamp=native.timestamp(row.get("timestamp"), provenance),
    )


def _append_synthetic(
    synthetics: tuple[Message, ...],
    history: list[Message],
    context_by_id: dict[str, Message],
    context_order: list[str],
) -> tuple[list[Message], dict[str, Message], list[str]]:
    history = [*history, *synthetics]
    context_by_id = {**context_by_id, **{synthetic.id: synthetic for synthetic in synthetics}}
    context_order = [*context_order, *(synthetic.id for synthetic in synthetics)]
    return history, context_by_id, context_order


def _close_pending(
    path: list[dict[str, Any]],
    start: int,
    row: dict[str, Any],
    unresolved: set[str],
    identity: Identity,
) -> tuple[Message, ...]:
    if not unresolved:
        return ()
    if unresolved & _later_tool_result_ids(path, start):
        raise PreservationError(ANSWERED_LATER)
    return tuple(_no_result(call_id, identity, row) for call_id in sorted(unresolved))


def _context_entry(
    kind: str, row: dict[str, Any], identity: Identity, assets: list[Attachment]
) -> tuple[Message, bool]:
    message = _message(row, identity, assets)
    payload = row if kind == "custom_message" else row.get("message", {})
    hidden = (kind == "custom_message" or payload.get("role") == "custom") and payload.get(
        "display"
    ) is False
    return (replace(message, hidden=True), True) if hidden else (message, False)


def _pending_after(
    role: str | None,
    message: Message,
    message_data: dict[str, Any] | None,
    pending_calls: set[str],
    pending_resolved: set[str],
) -> tuple[set[str], set[str]]:
    if role == "assistant":
        return {item.call_id for item in message.content if isinstance(item, ToolCall)}, set()
    if role == "toolResult" and isinstance(message_data, dict):
        call_id = message_data.get("toolCallId")
        if isinstance(call_id, str):
            return pending_calls, pending_resolved | {call_id}
    return pending_calls, pending_resolved


def _context_edit_update(
    row: dict[str, Any], context_by_id: dict[str, Message], assets: list[Attachment]
) -> tuple[str, Message | None]:
    target = row.get("targetId")
    if not isinstance(target, str) or target not in context_by_id:
        raise PreservationError("Pi context_edit targets an unknown entry")
    if "replacement" not in row:
        raise PreservationError("Invalid Pi context_edit")
    replacement = row["replacement"]
    if replacement is None:
        return target, None
    if not isinstance(replacement, dict) or "content" not in replacement:
        raise PreservationError("Invalid Pi context_edit replacement")
    original = context_by_id[target]
    parsed = native.blocks(replacement["content"], assets, "pi")
    new_content: tuple[Content, ...]
    if original.role == "tool":
        if any(not isinstance(item, Text | AttachmentRef) for item in parsed):
            raise PreservationError("Unsupported Pi context_edit tool result content")
        existing = cast(ToolResult, original.content[0])
        new_content = (replace(existing, output=cast(tuple[Text | AttachmentRef, ...], parsed)),)
    else:
        new_content = parsed
    return target, replace(original, content=new_content)


def _message(row: dict[str, Any], identity: Identity, assets: list[Attachment]) -> Message:
    key = native.string(row.get("id"), "entry ID")
    provenance = Provenance("authored", identity, key)
    kind = row["type"]
    if kind in ("compaction", "branch_summary"):
        prefix, suffix = (
            (SUMMARY_PREFIX, SUMMARY_SUFFIX)
            if kind == "compaction"
            else (BRANCH_PREFIX, "</summary>")
        )
        content = (Text(prefix + native.string(row.get("summary"), "summary") + suffix),)
        message = Message(
            key,
            "user",
            content,
            replace(provenance, origin="native"),
            timestamp=native.timestamp(row.get("timestamp"), provenance),
        )
        return native.restore(message, row)
    message_data: Any
    if kind == "custom_message":
        message_data = {
            "role": "user",
            "content": row.get("content"),
            "timestamp": row.get("timestamp"),
        }
        provenance = replace(
            provenance,
            origin="native",
            evidence=f"custom_message:{row.get('customType')};display={row.get('display')}",
        )
    else:
        message_data = row.get("message")
    if not isinstance(message_data, dict):
        raise PreservationError("Invalid Pi message")
    role = message_data.get("role")
    custom = row if kind == "custom_message" else message_data
    if (kind == "custom_message" or role == "custom") and (
        type(custom.get("display")) is not bool or not isinstance(custom.get("customType"), str)
    ):
        raise PreservationError("Pi custom context requires explicit display and customType")
    if role == "custom":
        provenance = replace(
            provenance,
            origin="native",
            evidence=f"custom:{message_data.get('customType')};display={message_data.get('display')}",
        )
        role = "user"
    if role == "bashExecution":
        raise PreservationError(
            f"Unsupported Pi bashExecution with excludeFromContext={message_data.get('excludeFromContext', False)}; shell execution semantics require native support"
        )
    if role not in ("user", "assistant", "toolResult"):
        raise PreservationError(f"Unsupported Pi message role: {role}")
    if role == "assistant":
        _validate_stop_reason(message_data.get("stopReason"))
    parsed = native.blocks(message_data.get("content"), assets, "pi")
    if role == "toolResult":
        if any(not isinstance(item, Text | AttachmentRef) for item in parsed):
            raise PreservationError("Unsupported Pi tool result content")
        parsed = (
            ToolResult(
                native.string(message_data.get("toolCallId"), "tool result ID"),
                cast(tuple[Text | AttachmentRef, ...], parsed),
                message_data.get("isError", False),
            ),
        )
        role = "tool"
    if message_data.get("errorMessage"):
        raise PreservationError("Pi assistant errorMessage requires a supported error semantic")
    result = Message(
        key,
        cast(Any, role),
        parsed,
        provenance,
        timestamp=native.timestamp(message_data.get("timestamp", row.get("timestamp")), provenance),
    )
    native.reject_assistant_images(result, "Pi")
    return native.restore(result, row)


def _checkpoint(
    row: dict[str, Any],
    identity: Identity,
    assets: list[Attachment],
    path: list[dict[str, Any]],
    entry_messages: dict[str, str],
    after: str,
) -> Checkpoint:
    summary = _message(row, identity, assets)
    retained_context = None
    retained_ids: tuple[str, ...] = ()
    if "retainedTail" in row:
        if not isinstance(row["retainedTail"], list):
            raise PreservationError("Invalid Pi retainedTail")
        retained_context = native.linked(
            [
                _message(
                    {
                        "type": "message",
                        "id": f"{row['id']}:tail:{i}",
                        "timestamp": row.get("timestamp"),
                        "message": message,
                    },
                    identity,
                    assets,
                )
                for i, message in enumerate(row["retainedTail"])
            ]
        )
    else:
        first = row.get("firstKeptEntryId")
        earlier = [item["id"] for item in path]
        if first == row["id"]:
            retained_ids = ()
        elif first not in earlier:
            raise PreservationError("Pi compaction retained start is missing from selected branch")
        else:
            selected = path[earlier.index(first) :]
            if any(item["type"] == "compaction" for item in selected):
                raise PreservationError(
                    "Pi retained range containing an older checkpoint is unsupported"
                )
            retained_ids = tuple(
                entry_messages[item["id"]] for item in selected if item["id"] in entry_messages
            )
    return Checkpoint(row["id"], after, (summary,), retained_ids, retained_context)


def _entries(
    path: list[dict[str, Any]], identity: Identity, assets: list[Attachment]
) -> tuple[list[Message], list[Message], list[Checkpoint], bool, dict[str, Message], set[str]]:
    history: list[Message] = []
    context_by_id: dict[str, Message] = {}
    context_order: list[str] = []
    removed_from_context: set[str] = set()
    hidden_context = False
    checkpoints: list[Checkpoint] = []
    entry_messages: dict[str, str] = {}
    pending_calls: set[str] = set()
    pending_resolved: set[str] = set()
    for index, row in enumerate(path):
        kind = row.get("type")
        if kind in IGNORED:
            continue
        message_data = row.get("message") if kind == "message" else None
        role = message_data.get("role") if isinstance(message_data, dict) else None
        if kind == "message" and role == "system":
            continue
        if (kind == "message" and role in ("user", "assistant")) or kind in (
            "custom_message",
            "branch_summary",
            "compaction",
        ):
            history, context_by_id, context_order = _append_synthetic(
                _close_pending(path, index, row, pending_calls - pending_resolved, identity),
                history,
                context_by_id,
                context_order,
            )
            pending_calls = set()
            pending_resolved = set()
        if (
            kind == "message"
            and role == "assistant"
            and isinstance(message_data, dict)
            and message_data.get("stopReason") in ("aborted", "error")
        ):
            if _tool_call_ids(message_data.get("content")) & _later_tool_result_ids(
                path, index + 1
            ):
                raise PreservationError(ANSWERED_LATER)
            continue
        if kind in ("message", "custom_message", "branch_summary"):
            message, hidden = _context_entry(kind, row, identity, assets)
            context_by_id[row["id"]] = message
            context_order.append(row["id"])
            hidden_context = hidden_context or hidden
            if not hidden:
                history.append(message)
                entry_messages[row["id"]] = message.id
            pending_calls, pending_resolved = _pending_after(
                role, message, message_data, pending_calls, pending_resolved
            )
        elif kind == "context_edit":
            target, edited = _context_edit_update(row, context_by_id, assets)
            if edited is None:
                removed_from_context.add(target)
            else:
                context_by_id[target] = edited
                removed_from_context.discard(target)
        elif kind == "compaction":
            if not history:
                raise PreservationError("Pi compaction has no historical boundary")
            checkpoints.append(
                _checkpoint(row, identity, assets, path[:index], entry_messages, history[-1].id)
            )
        else:
            raise PreservationError(f"Unsupported Pi conversational entry: {kind}")
    if path:
        history, context_by_id, context_order = _append_synthetic(
            _close_pending(path, len(path), path[-1], pending_calls - pending_resolved, identity),
            history,
            context_by_id,
            context_order,
        )
    uncompacted_context = [
        context_by_id[row_id] for row_id in context_order if row_id not in removed_from_context
    ]
    return (
        history,
        uncompacted_context,
        checkpoints,
        hidden_context,
        context_by_id,
        removed_from_context,
    )


def read(data: bytes, identity: Identity, branch_id: str | None = None) -> Conversation:
    if identity.agent != "pi":
        raise PreservationError("Source identity does not belong to pi")
    rows = native.records(data)
    header = rows[0]
    if (
        header.get("type") != "session"
        or header.get("version") != SESSION_VERSION
        or header.get("id") != identity.native_id
    ):
        raise PreservationError("Pi requires a matching v3 session header")
    path = native.chain(rows[1:], "id", "parentId", branch_id)
    assets: list[Attachment] = []
    (
        history,
        uncompacted_context,
        checkpoints,
        hidden_context,
        context_by_id,
        removed_from_context,
    ) = _entries(path, identity, assets)
    history_tuple = native.linked(history)
    context = native.linked(uncompacted_context)
    if checkpoints and hidden_context:
        raise PreservationError(
            "Pi hidden custom context combined with compaction requires unsupported visibility semantics"
        )
    if checkpoints:
        latest = checkpoints[-1]
        positions = {item.id: i for i, item in enumerate(history_tuple)}

        if latest.retained_context is not None:
            retained = latest.retained_context
        else:
            plain_retained = tuple(
                history_tuple[positions[key]] for key in latest.retained_message_ids
            )
            retained = tuple(
                context_by_id[key]
                for key in latest.retained_message_ids
                if key not in removed_from_context
            )
            if native.unparented(retained) != native.unparented(plain_retained):
                latest = replace(latest, retained_message_ids=(), retained_context=retained)
        if latest.following_context is not None:
            tail = latest.following_context
        else:
            plain_tail = history_tuple[positions[latest.after_message_id] + 1 :]
            tail = tuple(
                context_by_id[item.id] for item in plain_tail if item.id not in removed_from_context
            )
            if native.unparented(tail) != native.unparented(plain_tail):
                latest = replace(latest, following_context=tail)
        checkpoints[-1] = latest
        context = native.linked((*latest.summary, *retained, *tail))
    names = [
        row.get("name")
        for row in rows
        if row.get("type") == "session_info" and isinstance(row.get("name"), str)
    ]
    provenance = Provenance("native", identity, "header")
    result = Conversation(
        identity,
        path[-1]["id"],
        history_tuple,
        context,
        tuple(checkpoints),
        tuple(assets),
        names[-1] if names else None,
        source_created_at=native.timestamp(header.get("timestamp"), provenance),
        source_updated_at=native.timestamp(
            path[-1].get("timestamp"), Provenance("native", identity, path[-1]["id"])
        ),
    )
    result = native.restore_session(result, header.get("ringleaderSession"))
    validate(result)
    _validate_replay(result.history)
    _validate_replay(result.context)
    return result


class PiAdapter:
    profile = PROFILE

    def __init__(
        self, executable: str = "pi", verified: tuple[VerificationRecord, ...] = ()
    ) -> None:
        self.executable = executable
        self.verified = local_versions(verified, "pi")

    def inspect_source(self, request: SourceRequest) -> Compatibility:
        try:
            self.snapshot(request)
        except PreservationError as exc:
            return Compatibility(False, None, (str(exc),))
        return Compatibility(True, PROFILE, ())

    def snapshot(self, request: SourceRequest) -> SourceSnapshot:
        files = snapshot_files(
            (("session", native.source_path(request.source_ref, request.roots)),),
            max_bytes=native.MAX_BYTES,
        )
        return SourceSnapshot(
            read(files[0].data, request.identity, request.branch_id), files, PROFILE.name
        )

    def inspect_target(self, roots: NativeRoots, runner: Runner) -> Compatibility:
        result = native.probe_version(self.executable, "pi", runner)
        output = result.stdout.decode(errors="replace").strip()
        return native.version_compatibility(
            "Pi writer",
            PROFILE,
            check_version(output if result.returncode == 0 else "", PINNED, self.verified),
            output,
        )

    def normalizations(self, profile: FormatProfile) -> tuple[Normalization, ...]:
        return (
            Normalization(
                "json-argument-whitespace", profile.name, "Native tools store JSON objects"
            ),
            Normalization(
                "model-view-history",
                profile.name,
                "Pi keeps one stream: the model context, with hidden messages as ordinary entries",
            ),
        )

    def stage(self, conversation: Conversation, request: TargetRequest) -> NativeStage:
        validate(conversation)
        native.reject_opaque(conversation)
        applied = self.normalizations(PROFILE)
        try:
            stage = self._stage(conversation, request)
        except CheckpointShape:
            stage = self._stage(flattened(conversation), request)
            applied = (*applied, flattened_checkpoint(PROFILE.name))
        return replace(stage, normalizations=applied)

    def _stage(self, conversation: Conversation, request: TargetRequest) -> NativeStage:
        conversation = model_view(conversation, hides=False)
        if any(checkpoint.following_context is not None for checkpoint in conversation.checkpoints):
            raise CheckpointShape(
                "This native profile cannot preserve an independent post-checkpoint model stream"
            )
        _validate_replay(conversation.history)
        _validate_replay(conversation.context)
        native.safe_id(request.identity, "pi")
        if request.profile != PROFILE:
            raise PreservationError("Unverified Pi writer profile")
        if not conversation.history:
            raise PreservationError("Pi requires a nonempty conversation")
        now = datetime.now(UTC)
        iso = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        rows: list[dict[str, Any]] = [
            {
                "type": "session",
                "version": 3,
                "id": request.identity.native_id,
                "timestamp": iso,
                "cwd": str(request.roots.cwd),
                "ringleaderSession": native.session_metadata(conversation),
            }
        ]
        ids = {item.id: uuid4().hex[:8] for item in conversation.history}
        assets = {item.id: item for item in conversation.attachments}
        calls = {
            item.call_id: item.name
            for message in conversation.history
            for item in message.content
            if isinstance(item, ToolCall)
        }
        parent = None
        for position, message in enumerate(conversation.history):
            historical = native.historical_time(message, now)
            millis = historical.timestamp() * 1000
            content = message.content
            role = native.native_role(message)
            result: dict[str, Any]
            if (
                len(content) == 1
                and isinstance(content[0], ToolResult)
                and role in ("user", "tool")
            ):
                tool = content[0]
                result = {
                    "role": "toolResult",
                    "toolCallId": tool.call_id,
                    "toolName": calls[tool.call_id],
                    "content": native.emit_blocks(tool.output, assets, "pi"),
                    "isError": tool.is_error,
                    "timestamp": millis,
                }
            elif role in ("user", "assistant"):
                result = {
                    "role": role,
                    "content": native.emit_blocks(content, assets, "pi"),
                    "timestamp": millis,
                }
                if role == "assistant":
                    result.update(
                        api="anthropic-messages",
                        provider="anthropic",
                        model="claude-sonnet-4-5",
                        usage=ZERO_USAGE,
                        stopReason="toolUse"
                        if any(isinstance(item, ToolCall) for item in content)
                        else "stop",
                    )
            else:
                raise PreservationError(
                    "Pi requires user/assistant messages or one tool result per message; historical system policy cannot be activated"
                )
            row = {
                "type": "message",
                "id": ids[message.id],
                "parentId": parent,
                "timestamp": historical.isoformat().replace("+00:00", "Z"),
                "message": result,
                "ringleader": native.message_annotations(message, freeform_calls=True),
            }
            rows.append(row)
            parent = row["id"]
            for checkpoint in conversation.checkpoints:
                if checkpoint.after_message_id != message.id:
                    continue
                if checkpoint.retained_context is not None:
                    raise CheckpointShape(
                        "Pi 0.85.1 runtime does not honor embedded retainedTail; cannot preserve this checkpoint"
                    )
                if (
                    len(checkpoint.summary) != 1
                    or checkpoint.summary[0].role != "user"
                    or len(checkpoint.summary[0].content) != 1
                    or not isinstance(checkpoint.summary[0].content[0], Text)
                ):
                    raise CheckpointShape("Pi requires one user text compaction summary")
                summary_message = checkpoint.summary[0]
                summary = cast(Text, summary_message.content[0]).text
                if not summary.startswith(SUMMARY_PREFIX) or not summary.endswith(SUMMARY_SUFFIX):
                    raise CheckpointShape(
                        "Pi checkpoint summary must preserve its native context wrapper"
                    )
                retained = checkpoint.retained_message_ids
                if retained and retained != tuple(
                    item.id
                    for item in conversation.history[position - len(retained) + 1 : position + 1]
                ):
                    raise CheckpointShape("Pi requires a contiguous retained history suffix")
                key = uuid4().hex[:8]
                rows.append(
                    {
                        "type": "compaction",
                        "id": key,
                        "parentId": parent,
                        "timestamp": native.historical_time(summary_message, now)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "summary": summary[len(SUMMARY_PREFIX) : -len(SUMMARY_SUFFIX)],
                        "firstKeptEntryId": ids[retained[0]] if retained else key,
                        "tokensBefore": 0,
                        "ringleader": native.message_annotations(
                            summary_message, freeform_calls=True
                        ),
                    }
                )
                parent = key
        rows.append(
            {
                "type": "session_info",
                "id": uuid4().hex[:8],
                "parentId": parent,
                "timestamp": iso,
                "name": request.title,
            }
        )
        slug = "--" + re.sub(r"[/\\:]", "-", str(request.roots.cwd).lstrip("/\\")) + "--"
        ref = f"sessions/{slug}/{iso.replace(':', '-')}_{request.identity.native_id}.jsonl"
        return NativeStage(
            request.identity,
            PROFILE,
            request.roots,
            (NativeFile(ref, native.jsonl(rows), True),),
            ref,
        )

    def stage_metadata(self, stage: NativeStage) -> NativeMetadata:
        rows = native.records(
            next(item.data for item in stage.files if item.relative_path == stage.source_ref)
        )
        titles = [row for row in rows if row.get("type") == "session_info"]
        if not titles:
            raise PreservationError("Pi staged picker title is missing")
        return NativeMetadata(Identity("pi", rows[0]["id"]), rows[0]["cwd"], titles[-1]["name"])

    def read_stage(self, stage: NativeStage) -> Conversation:
        if stage.profile != PROFILE:
            raise PreservationError("Unverified native staged profile")
        return read(
            next(item.data for item in stage.files if item.relative_path == stage.source_ref),
            stage.identity,
        )

    def launch_request(self, identity: Identity, roots: NativeRoots) -> RunRequest:
        native.safe_id(identity, "pi")
        return native.launch_request(
            self.executable, ("--session", identity.native_id), roots, "pi"
        )
