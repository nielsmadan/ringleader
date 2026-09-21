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

SESSION_VERSION = 3

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
IGNORED = {"custom", "model_change", "thinking_level_change", "label", "session_info"}
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
    pending: set[str] = set()
    for message in messages:
        native.reject_assistant_images(message, "Pi")
        results_only = bool(message.content) and all(
            isinstance(item, ToolResult) for item in message.content
        )
        if pending and not results_only:
            raise PreservationError(
                "Pi tool results must follow their assistant calls contiguously; an intervening user/assistant message changes native replay"
            )
        for item in message.content:
            if isinstance(item, ToolCall):
                pending.add(item.call_id)
            elif isinstance(item, ToolResult):
                pending.discard(item.call_id)


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
    history: list[Message] = []
    uncompacted_context: list[Message] = []
    hidden_context = False
    checkpoints: list[Checkpoint] = []
    entry_messages: dict[str, str] = {}
    for index, row in enumerate(path):
        kind = row.get("type")
        if kind in IGNORED:
            continue
        if kind in ("message", "custom_message", "branch_summary"):
            message = _message(row, identity, assets)
            uncompacted_context.append(message)
            payload = row if kind == "custom_message" else row.get("message", {})
            hidden = (kind == "custom_message" or payload.get("role") == "custom") and payload.get(
                "display"
            ) is False
            hidden_context = hidden_context or hidden
            if not hidden:
                history.append(message)
                entry_messages[row["id"]] = message.id
        elif kind == "compaction":
            if not history:
                raise PreservationError("Pi compaction has no historical boundary")
            checkpoints.append(
                _checkpoint(row, identity, assets, path[:index], entry_messages, history[-1].id)
            )
        else:
            raise PreservationError(f"Unsupported Pi conversational entry: {kind}")
    history_tuple = native.linked(history)
    context = native.linked(uncompacted_context)
    if checkpoints and hidden_context:
        raise PreservationError(
            "Pi hidden custom context combined with compaction requires unsupported visibility semantics"
        )
    if checkpoints:
        latest = checkpoints[-1]
        positions = {item.id: i for i, item in enumerate(history_tuple)}
        retained = (
            latest.retained_context
            if latest.retained_context is not None
            else tuple(history_tuple[positions[key]] for key in latest.retained_message_ids)
        )
        context = native.linked(
            (*latest.summary, *retained, *history_tuple[positions[latest.after_message_id] + 1 :])
        )
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
    def __init__(self, executable: str = "pi") -> None:
        self.executable = executable

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
        version = result.stdout.decode(errors="replace").strip()
        supported = result.returncode == 0 and version == "0.85.1"
        return Compatibility(
            supported,
            PROFILE if supported else None,
            () if supported else (f"Pi writer requires verified 0.85.1; found {version!r}",),
            version,
        )

    def normalizations(self, profile: FormatProfile) -> tuple[Normalization, ...]:
        return (
            Normalization(
                "json-argument-whitespace", profile.name, "Native tools store JSON objects"
            ),
        )

    def stage(self, conversation: Conversation, request: TargetRequest) -> NativeStage:
        validate(conversation)
        _validate_replay(conversation.history)
        _validate_replay(conversation.context)
        native.safe_id(request.identity, "pi")
        if request.profile != PROFILE:
            raise PreservationError("Unverified Pi writer profile")
        if not conversation.history:
            raise PreservationError("Pi requires a nonempty conversation")
        if not conversation.checkpoints and native.linked(conversation.context) != native.linked(
            conversation.history
        ):
            raise PreservationError(
                "Pi cannot represent distinct history/context without a checkpoint"
            )
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
                "ringleader": native.message_annotations(message),
            }
            rows.append(row)
            parent = row["id"]
            for checkpoint in conversation.checkpoints:
                if checkpoint.after_message_id != message.id:
                    continue
                if checkpoint.retained_context is not None:
                    raise PreservationError(
                        "Pi 0.85.1 runtime does not honor embedded retainedTail; cannot preserve this checkpoint"
                    )
                if (
                    len(checkpoint.summary) != 1
                    or checkpoint.summary[0].role != "user"
                    or len(checkpoint.summary[0].content) != 1
                    or not isinstance(checkpoint.summary[0].content[0], Text)
                ):
                    raise PreservationError("Pi requires one user text compaction summary")
                summary_message = checkpoint.summary[0]
                summary = cast(Text, summary_message.content[0]).text
                if not summary.startswith(SUMMARY_PREFIX) or not summary.endswith(SUMMARY_SUFFIX):
                    raise PreservationError(
                        "Pi checkpoint summary must preserve its native context wrapper"
                    )
                retained = checkpoint.retained_message_ids
                if retained and retained != tuple(
                    item.id
                    for item in conversation.history[position - len(retained) + 1 : position + 1]
                ):
                    raise PreservationError("Pi requires a contiguous retained history suffix")
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
                        "ringleader": native.message_annotations(summary_message),
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

    def read_stage(self, stage: NativeStage) -> Conversation:
        if stage.profile != PROFILE:
            raise PreservationError("Unverified native staged profile")
        return read(
            next(item.data for item in stage.files if item.relative_path == stage.source_ref),
            stage.identity,
        )

    def launch_request(self, identity: Identity, roots: NativeRoots) -> RunRequest:
        native.safe_id(identity, "pi")
        return native.run_request(self.executable, ("--session", identity.native_id), roots, "pi")
