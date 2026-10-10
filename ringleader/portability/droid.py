from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from ..slug import droid_slug
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

SESSION_VERSION = 2
PINNED: tuple[str, ...] = ()
PROFILE = FormatProfile(
    "droid",
    "droid-session-v2",
    "2",
    (
        "droid 0.228.0 src/services/sessionJsonl.ts: session_start, message events in file order, latest compaction_state",
        "droid 0.228.0 offline /v1/messages captures of droid exec --fork",
    ),
)
LLM_SUMMARY = "llm_summary"
PROVIDER_SWITCH = "provider_switch_serialization"
LLM_PREFIX = (
    "A previous instance of Droid has summarized the conversation thus far as follows:\n\n"
    "<summary>\n"
)
LLM_SUFFIX = (
    "\n</summary>\n\nIMPORTANT: This summary was created by a previous instance of Droid. "
    "Files referenced in the summary may not be available until you explicitly view them again."
)
SWITCH_PREFIX = (
    "The following prefix of this conversation has been serialized to maintain compatibility "
    "across model providers.\n\n"
)
SWITCH_SUFFIX = (
    "\n\nWhen making tool calls, always use your native tool calling schema — do not "
    "replicate the text format shown in the transcript above."
)
CANCELLED = "Error: Tool execution was cancelled"
# id prefixes of droid's regenerated injections: session context, system reminders, todo nags
BOOTSTRAP_PREFIXES = ("context-", "__internal__system-reminder__", "todo-stale-", "todo-suggest-")
ANSWERED_LATER = "Droid tool call was answered after an intervening message; cannot import safely"
SKIPPED_RECORDS = frozenset({"todo_state", "agent_turn_outcome"})
ENVELOPE_KEYS = frozenset(
    {"type", "id", "parentId", "timestamp", "message", "compactionSummaryId", "ringleader"}
)
MESSAGE_KEYS = frozenset(
    {
        "role",
        "content",
        "visibility",
        "interactionMode",
        "modelId",
        "apiProvider",
        "reasoningEffort",
        "routerId",
        "userMessageSource",
        "openaiMessageId",
        "openaiPhase",
        "openaiEncryptedContent",
        "openaiReasoningId",
        "openaiReasoningSummary",
        "chatCompletionReasoningField",
        "chatCompletionReasoningContent",
    }
)
COMPACTION_KEYS = frozenset(
    {
        "type",
        "id",
        "timestamp",
        "summaryText",
        "summaryTokens",
        "summaryKind",
        "removedCount",
        "anchorMessage",
        "systemInfo",
        "systemInfoText",
        "uiRenderCutoffMessageId",
        "loadedDeferredToolNames",
        "ringleader",
    }
)
BLOCKS = frozenset({"text", "thinking", "redacted_thinking", "image", "tool_use", "tool_result"})
RESULT_BLOCKS = frozenset({"text", "image"})


def summary_text(kind: str, text: str) -> str:
    if kind == LLM_SUMMARY:
        return LLM_PREFIX + text + LLM_SUFFIX
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{SWITCH_PREFIX}{fence}text\n{text}\n{fence}{SWITCH_SUFFIX}"


def parse_summary(text: str) -> tuple[str, str]:
    if (
        text.startswith(LLM_PREFIX)
        and text.endswith(LLM_SUFFIX)
        and len(text) >= len(LLM_PREFIX) + len(LLM_SUFFIX)
    ):
        return LLM_SUMMARY, text[len(LLM_PREFIX) : -len(LLM_SUFFIX)]
    match = re.fullmatch(
        re.escape(SWITCH_PREFIX) + r"(`{3,})text\n(.*)\n\1" + re.escape(SWITCH_SUFFIX),
        text,
        re.DOTALL,
    )
    if match and summary_text(PROVIDER_SWITCH, match.group(2)) == text:
        return PROVIDER_SWITCH, match.group(2)
    raise PreservationError("Droid checkpoint summary must carry its native compaction wrapper")


def _unknown(keys: Any, allowed: frozenset[str], label: str) -> None:
    extra = sorted(set(keys) - allowed)
    if extra:
        raise PreservationError(f"Unsupported Droid {label}: {extra[0]}")


def _check_blocks(content: Any) -> None:
    if not isinstance(content, list):
        raise PreservationError("Droid message content must be a block list")
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in BLOCKS:
            kind = block.get("type") if isinstance(block, dict) else type(block).__name__
            raise PreservationError(f"Unsupported Droid content block: {kind}")
        nested = block.get("content") if block["type"] == "tool_result" else None
        if isinstance(nested, list) and any(
            not isinstance(item, dict) or item.get("type") not in RESULT_BLOCKS for item in nested
        ):
            raise PreservationError("Unsupported Droid tool result content")


def _header(row: dict[str, Any], identity: Identity) -> dict[str, Any]:
    if (
        row.get("type") != "session_start"
        or row.get("version") != SESSION_VERSION
        or row.get("id") != identity.native_id
    ):
        raise PreservationError("Droid requires a matching version 2 session_start header")
    if row.get("callingSessionId") is not None:
        raise PreservationError("Droid subagent sessions are not resumable top-level sessions")
    return row


def _segments(parsed: tuple[Content, ...], role: str) -> list[tuple[str, tuple[Content, ...]]]:
    if role == "assistant":
        return [("assistant", parsed)] if parsed else []
    segments: list[tuple[str, tuple[Content, ...]]] = []
    for item in parsed:
        if isinstance(item, ToolResult):
            segments.append(("tool", (item,)))
        elif segments and segments[-1][0] == "user":
            segments[-1] = ("user", (*segments[-1][1], item))
        else:
            segments.append(("user", (item,)))
    return segments


def _messages(row: dict[str, Any], identity: Identity, assets: list[Attachment]) -> list[Message]:
    key = native.string(row.get("id"), "message ID")
    data = row["message"]
    _unknown(data, MESSAGE_KEYS, "message field")
    role = data.get("role")
    if role not in ("user", "assistant"):
        raise PreservationError(f"Unsupported Droid message role: {role}")
    _check_blocks(data.get("content"))
    parsed = native.blocks(data["content"], assets, "claude")
    hidden = data.get("visibility") == "llm_only"
    provenance = (
        Provenance("harness", identity, key, "droid visibility=llm_only")
        if hidden
        else Provenance("authored", identity, key)
    )
    if hidden and (role != "user" or any(not isinstance(item, Text) for item in parsed)):
        raise PreservationError("Unsupported Droid llm_only content")
    segments = _segments(parsed, role)
    if len(segments) > 1 and "ringleader" in row:
        raise PreservationError("Droid annotated message splits into several native messages")
    result = [
        Message(
            key if len(segments) == 1 else f"{key}:{index}",
            cast(Any, kind),
            content,
            provenance,
            timestamp=native.timestamp(row.get("timestamp"), provenance),
            hidden=hidden,
        )
        for index, (kind, content) in enumerate(segments)
    ]
    for message in result:
        native.reject_assistant_images(message, "Droid")
    return [native.restore(result[0], row)] if len(result) == 1 else result


@dataclass
class _Reader:
    identity: Identity
    assets: list[Attachment] = field(default_factory=list)
    stream: list[Message] = field(default_factory=list)
    seen: dict[str, int] = field(default_factory=dict)
    pending: list[str] = field(default_factory=list)
    closed: set[str] = field(default_factory=set)
    compactions: list[tuple[Message, int, int]] = field(default_factory=list)
    last: dict[str, Any] | None = None

    def row(self, row: dict[str, Any]) -> None:
        kind = row.get("type")
        if kind in SKIPPED_RECORDS:
            return
        if kind == "compaction_state":
            self.compaction(row)
        elif kind == "message":
            self.message(row)
        else:
            raise PreservationError(f"Unsupported Droid record type: {kind}")
        self.last = row

    def message(self, row: dict[str, Any]) -> None:
        _unknown(row, ENVELOPE_KEYS, "message envelope field")
        key = native.string(row.get("id"), "message ID")
        data = row.get("message")
        if not isinstance(data, dict):
            raise PreservationError("Invalid Droid message")
        visibility = data.get("visibility")
        if visibility not in (None, "llm_only", "user_only"):
            raise PreservationError(f"Unsupported Droid message visibility: {visibility}")
        if visibility != "user_only" and not (
            visibility == "llm_only" and key.startswith(BOOTSTRAP_PREFIXES)
        ):
            for message in _messages(row, self.identity, self.assets):
                self.add(message, row)
        self.seen[key] = len(self.stream)

    def add(self, message: Message, row: dict[str, Any]) -> None:
        if native.results_only(message):
            calls = {cast(ToolResult, item).call_id for item in message.content}
            if calls & self.closed:
                raise PreservationError(ANSWERED_LATER)
            self.pending = [item for item in self.pending if item not in calls]
        else:
            self.close(row)
        self.stream.append(message)
        if message.role == "assistant":
            self.pending = [item.call_id for item in message.content if isinstance(item, ToolCall)]

    def close(self, row: dict[str, Any]) -> None:
        provenance = Provenance(
            "harness",
            self.identity,
            native.string(row.get("id"), "record ID"),
            "droid replay: tool call left without a result",
        )
        self.stream.extend(
            Message(
                f"cancelled-{call}",
                "tool",
                (ToolResult(call, (Text(CANCELLED),), False),),
                provenance,
                timestamp=native.timestamp(row.get("timestamp"), provenance),
            )
            for call in self.pending
        )
        self.closed.update(self.pending)
        self.pending = []

    def compaction(self, row: dict[str, Any]) -> None:
        _unknown(row, COMPACTION_KEYS, "compaction field")
        key = native.string(row.get("id"), "compaction ID")
        kind = row.get("summaryKind") or LLM_SUMMARY
        if kind not in (LLM_SUMMARY, PROVIDER_SWITCH):
            raise PreservationError(f"Unsupported Droid summaryKind: {kind}")
        provenance = Provenance("native", self.identity, key)
        summary = native.restore(
            Message(
                key,
                "user",
                (Text(summary_text(kind, native.string(row.get("summaryText"), "summaryText"))),),
                provenance,
                timestamp=native.timestamp(row.get("timestamp"), provenance),
            ),
            row,
        )
        anchor = row.get("anchorMessage")
        if anchor is None and self.stream:
            raise PreservationError(
                "Droid compaction without an anchor after messages is unsupported"
            )
        if anchor is None:
            start = 0
        elif isinstance(anchor, dict) and anchor.get("id") in self.seen:
            start = self.seen[anchor["id"]]
        else:
            raise PreservationError("Droid compaction anchor is missing from the transcript")
        if not any(not message.hidden for message in self.stream):
            if self.stream or start:
                raise PreservationError(
                    "Droid compaction before any visible message is unsupported"
                )
            self.stream.append(replace(summary, hidden=True))
            return
        self.compactions.append((summary, start, len(self.stream)))

    def checkpoint(self, summary: Message, start: int, end: int, latest: bool) -> Checkpoint:
        before = self.stream[:end]
        boundary = [message for message in before if not message.hidden][-1]
        retained = [message for message in before[start:] if latest or not message.hidden]
        while retained and native.results_only(retained[0]):
            retained = retained[1:]
        following = self.stream[end:]
        owned = any(message.hidden for message in retained)
        return Checkpoint(
            summary.id,
            boundary.id,
            (summary,),
            () if owned else tuple(message.id for message in retained),
            native.linked(retained) if owned else None,
            native.linked(following)
            if latest and any(message.hidden for message in following)
            else None,
        )

    def conversation(self, header: dict[str, Any], branch_id: str | None) -> Conversation:
        if self.pending and self.last is not None:
            self.close(self.last)
        history = native.linked([message for message in self.stream if not message.hidden])
        if not history:
            raise PreservationError("Droid session has no conversation messages")
        checkpoints = tuple(
            self.checkpoint(*item, latest=index == len(self.compactions) - 1)
            for index, item in enumerate(self.compactions)
        )
        context = native.linked(self.stream)
        if checkpoints:
            latest = checkpoints[-1]
            by_id = {message.id: message for message in history}
            retained = (
                latest.retained_context
                if latest.retained_context is not None
                else tuple(by_id[key] for key in latest.retained_message_ids)
            )
            tail = latest.following_context or tuple(
                message for message in self.stream[self.compactions[-1][2] :] if not message.hidden
            )
            context = native.linked((*latest.summary, *retained, *tail))
        branch = native.string((self.last or header).get("id"), "record ID")
        if branch_id is not None and branch_id != branch:
            raise PreservationError("Droid sessions are linear; unknown selected branch")
        provenance = Provenance("native", self.identity, "header")
        first = next((row for row in history if row.timestamp is not None), None)
        return Conversation(
            self.identity,
            branch,
            history,
            context,
            checkpoints,
            tuple(self.assets),
            header.get("title") if isinstance(header.get("title"), str) else None,
            source_created_at=first.timestamp if first else None,
            source_updated_at=native.timestamp(
                (self.last or {}).get("timestamp"), replace(provenance, locator=branch)
            ),
        )


def read(data: bytes, identity: Identity, branch_id: str | None = None) -> Conversation:
    if identity.agent != "droid":
        raise PreservationError("Source identity does not belong to droid")
    rows = native.records(data)
    header = _header(rows[0], identity)
    reader = _Reader(identity)
    for row in rows[1:]:
        reader.row(row)
    result = native.restore_session(
        reader.conversation(header, branch_id), header.get("ringleaderSession")
    )
    validate(result)
    _validate_replay(result.history)
    _validate_replay(result.context)
    return result


def _validate_replay(messages: tuple[Message, ...]) -> None:
    native.validate_replay(messages, "Droid")


def _first_record(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            line = handle.readline(1024 * 1024)
        value = json.loads(line)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def session_files(sessions: Path) -> list[Path]:
    try:
        return [*sessions.glob("*.jsonl"), *sessions.glob("*/*.jsonl")]
    except OSError:
        return []


def local_owner(sessions: Path) -> str | None:
    def modified(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    for path in sorted(session_files(sessions), key=modified, reverse=True):
        start = _first_record(path)
        if (
            start is not None
            and start.get("type") == "session_start"
            and start.get("callingSessionId") is None
            and isinstance(start.get("owner"), str)
            and start["owner"]
        ):
            return cast(str, start["owner"])
    return None


def _checkpoint_stream(conversation: Conversation) -> tuple[tuple[Message, ...], int]:
    *earlier, latest = conversation.checkpoints
    if any(
        checkpoint.following_context is not None or checkpoint.retained_context is not None
        for checkpoint in earlier
    ):
        raise CheckpointShape(
            "Droid cannot preserve an independent model stream behind an earlier checkpoint"
        )
    history = conversation.history
    cut = [message.id for message in history].index(latest.after_message_id) + 1
    retained = latest.retained_context or ()
    visible = tuple(message for message in retained if not message.hidden)
    if native.unparented(visible) != native.unparented(history[cut - len(visible) : cut]) or any(
        message.role != "user" for message in retained if message.hidden
    ):
        raise CheckpointShape(
            "Droid can only preserve retained checkpoint context that interleaves hidden messages with history"
        )
    tail = history[cut:]
    if latest.following_context is not None:
        shown = tuple(message for message in latest.following_context if not message.hidden)
        if native.unparented(shown) != native.unparented(tail):
            raise CheckpointShape(
                "Droid cannot preserve an independent post-checkpoint model stream that hides visible history"
            )
        tail = latest.following_context
    prefix = history[: cut - len(visible)]
    return (*prefix, *retained, *tail), len(prefix) + len(retained) - 1


def _anchor(
    checkpoint: Checkpoint, stream: tuple[Message, ...], written: dict[str, str], emit_at: int
) -> dict[str, Any]:
    if checkpoint.retained_context is not None:
        index = emit_at - len(checkpoint.retained_context)
    elif checkpoint.retained_message_ids:
        positions = {message.id: position for position, message in enumerate(stream)}
        index = positions.get(checkpoint.retained_message_ids[0], 0) - 1
        shown = tuple(item.id for item in stream[index + 1 : emit_at + 1] if not item.hidden)
        if checkpoint.retained_message_ids != shown:
            raise CheckpointShape("Droid requires a contiguous retained history suffix")
    else:
        index = emit_at
    if index < 0:
        raise CheckpointShape("Droid compaction must keep at least one message before its anchor")
    return {"id": written[stream[index].id], "index": index}


def _name(block: dict[str, Any], asset: Attachment) -> None:
    if asset.name is not None:
        block["ringleaderName"] = asset.name


class DroidAdapter:
    profile = PROFILE

    def __init__(
        self, executable: str = "droid", verified: tuple[VerificationRecord, ...] = ()
    ) -> None:
        self.executable = executable
        self.verified = local_versions(verified, "droid")

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
        try:
            result = native.probe_version(self.executable, "droid", runner)
        except PreservationError as exc:
            return Compatibility(False, None, (str(exc),))
        output = result.stdout.decode(errors="replace").strip()
        compatibility = native.version_compatibility(
            "Droid writer",
            PROFILE,
            check_version(output if result.returncode == 0 else "", PINNED, self.verified),
            output,
        )
        if not compatibility.supported:
            return compatibility
        owner = local_owner(roots.store / "sessions")
        if owner is None:
            return Compatibility(
                False,
                None,
                ("Droid target has no local top-level session to take its owner from",),
                output,
            )
        return replace(compatibility, target_configuration=(("owner", owner),))

    def normalizations(self, profile: FormatProfile) -> tuple[Normalization, ...]:
        return (
            Normalization(
                "json-argument-whitespace", profile.name, "Native tools store JSON objects"
            ),
            Normalization(
                "model-view-history",
                profile.name,
                "Droid keeps one stream: the model context, with llm_only messages for hidden ones",
                hides=True,
            ),
        )

    def _header(self, conversation: Conversation, request: TargetRequest) -> dict[str, Any]:
        owner = dict(request.target_configuration).get("owner")
        if not owner:
            raise PreservationError("Droid target owner is unknown; inspect the target first")
        return {
            "type": "session_start",
            "id": request.identity.native_id,
            "title": request.title,
            "owner": owner,
            "version": SESSION_VERSION,
            "cwd": str(request.roots.cwd),
            "isSessionTitleManuallySet": True,
            "ringleaderSession": native.session_metadata(conversation),
        }

    def _check_identity(self, request: TargetRequest) -> None:
        native.safe_id(request.identity, "droid")
        try:
            UUID(request.identity.native_id)
        except ValueError as exc:
            raise PreservationError("Droid session ID must be a UUID") from exc
        name = f"{request.identity.native_id}.jsonl"
        if any(path.name == name for path in session_files(request.roots.store / "sessions")):
            raise PreservationError("Droid session ID already exists in the native store")
        if request.profile != PROFILE:
            raise PreservationError("Unverified Droid writer profile")

    def stage(self, conversation: Conversation, request: TargetRequest) -> NativeStage:
        validate(conversation)
        native.reject_opaque(conversation)
        self._check_identity(request)
        applied = self.normalizations(PROFILE)
        try:
            rows = self._rows(conversation, request)
        except CheckpointShape:
            rows = self._rows(flattened(conversation), request)
            applied = (*applied, flattened_checkpoint(PROFILE.name))
        ref = f"sessions/{droid_slug(request.roots.cwd)}/{request.identity.native_id}.jsonl"
        return NativeStage(
            request.identity,
            PROFILE,
            request.roots,
            (NativeFile(ref, native.jsonl(rows), True),),
            ref,
            normalizations=applied,
        )

    def _rows(self, conversation: Conversation, request: TargetRequest) -> list[dict[str, Any]]:
        written = model_view(conversation, hides=True)
        if not written.history:
            raise PreservationError("Droid requires a nonempty conversation")
        stream, latest_at = (
            _checkpoint_stream(conversation)
            if conversation.checkpoints
            else (written.context, len(written.context) - 1)
        )
        _validate_replay(stream)
        now = datetime.now(UTC)
        assets = {item.id: item for item in conversation.attachments}
        ids = {message.id: str(uuid4()) for message in stream}
        positions = {message.id: index for index, message in enumerate(stream)}
        emit: dict[int, list[Checkpoint]] = {}
        for checkpoint in conversation.checkpoints:
            at = (
                latest_at
                if checkpoint is conversation.checkpoints[-1]
                else positions[checkpoint.after_message_id]
            )
            emit.setdefault(at, []).append(checkpoint)
        rows: list[dict[str, Any]] = [self._header(conversation, request)]
        parent: str | None = None
        for index, message in enumerate(stream):
            rows.append(self._row(message, ids[message.id], parent, assets, now))
            parent = ids[message.id]
            rows.extend(
                self._compaction(checkpoint, stream, ids, index, now)
                for checkpoint in emit.get(index, [])
            )
        return rows

    def _row(
        self,
        message: Message,
        key: str,
        parent: str | None,
        assets: dict[str, Attachment],
        now: datetime,
    ) -> dict[str, Any]:
        native.reject_assistant_images(message, "Droid")
        role = native.native_role(message)
        if role == "tool" and all(isinstance(item, ToolResult) for item in message.content):
            role = "user"
        if role not in ("user", "assistant"):
            raise PreservationError(
                "Droid requires user/assistant history; historical system policy cannot be activated"
            )
        if (
            any(isinstance(item, ToolResult) for item in message.content)
            and len(message.content) > 1
        ):
            raise PreservationError("Droid writes one tool result per message")
        if message.hidden and (
            role != "user"
            or any(
                isinstance(item, ToolCall | ToolResult | AttachmentRef) for item in message.content
            )
        ):
            raise PreservationError("Droid hidden messages carry user text only")
        data: dict[str, Any] = {"role": role, "content": self._blocks(message.content, assets)}
        if message.hidden:
            data["visibility"] = "llm_only"
        return {
            "type": "message",
            "id": key,
            **({"parentId": parent} if parent else {}),
            "timestamp": native.historical_time(message, now)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "message": data,
            "ringleader": native.message_annotations(message, freeform_calls=True),
        }

    def _blocks(
        self, content: tuple[Content, ...], assets: dict[str, Attachment]
    ) -> list[dict[str, Any]]:
        blocks = native.emit_blocks(content, assets, "claude")
        for item, block in zip(content, blocks, strict=True):
            if isinstance(item, AttachmentRef):
                _name(block, assets[item.attachment_id])
            elif isinstance(item, ToolResult):
                for part, nested in zip(item.output, block["content"], strict=True):
                    if isinstance(part, AttachmentRef):
                        _name(nested, assets[part.attachment_id])
                if len(item.output) == 1 and isinstance(item.output[0], Text):
                    block["content"] = item.output[0].text
        return blocks

    def _compaction(
        self,
        checkpoint: Checkpoint,
        stream: tuple[Message, ...],
        written: dict[str, str],
        emit_at: int,
        now: datetime,
    ) -> dict[str, Any]:
        if (
            len(checkpoint.summary) != 1
            or checkpoint.summary[0].role != "user"
            or len(checkpoint.summary[0].content) != 1
            or not isinstance(checkpoint.summary[0].content[0], Text)
        ):
            raise CheckpointShape("Droid requires one user text compaction summary")
        summary = checkpoint.summary[0]
        try:
            kind, text = parse_summary(cast(Text, summary.content[0]).text)
        except PreservationError as exc:
            raise CheckpointShape(str(exc)) from exc
        anchor = _anchor(checkpoint, stream, written, emit_at)
        return {
            "type": "compaction_state",
            "id": str(uuid4()),
            "timestamp": native.historical_time(summary, now)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "summaryText": text,
            "summaryTokens": 0,
            "summaryKind": kind,
            "anchorMessage": anchor,
            "removedCount": anchor["index"] + 1,
            "ringleader": native.message_annotations(summary, freeform_calls=True),
        }

    def stage_metadata(self, stage: NativeStage) -> NativeMetadata:
        header = native.first_record(
            next(item.data for item in stage.files if item.relative_path == stage.source_ref)
        )
        return NativeMetadata(
            Identity("droid", native.string(header.get("id"), "session ID")),
            native.string(header.get("cwd"), "cwd"),
            header.get("title"),
        )

    def read_stage(self, stage: NativeStage) -> Conversation:
        if stage.profile != PROFILE:
            raise PreservationError("Unverified native staged profile")
        return read(
            next(item.data for item in stage.files if item.relative_path == stage.source_ref),
            stage.identity,
        )

    def launch_request(self, identity: Identity, roots: NativeRoots) -> RunRequest:
        native.safe_id(identity, "droid")
        return native.launch_request(
            self.executable, ("resume", identity.native_id), roots, "droid"
        )
