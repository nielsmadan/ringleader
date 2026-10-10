from __future__ import annotations

import base64
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from . import claude_attachments, claude_chain, native
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

MAX_SLUG_LENGTH = 200
PINNED = ("2.1.278", "2.1.283")

PROFILE = FormatProfile(
    "claude-code",
    "claude-code-2.1.278",
    "jsonl-parentUuid",
    (
        "Claude Code 2.1.278",
        "anthropics/claude-agent-sdk-python@v0.2.156/src/claude_agent_sdk/_internal/sessions.py",
        "anthropics/claude-agent-sdk-python@v0.2.156/src/claude_agent_sdk/_internal/session_mutations.py",
    ),
)
IGNORED = frozenset(
    {
        "permission-mode",
        "atis-latch",
        "cost-state",
        "file-history-snapshot",
        "file-history-delta",
        "last-prompt",
        "queue-operation",
        "custom-title",
        "ai-title",
        "summary",
        "agent-name",
        "agent-color",
        "agent-setting",
        "pr-link",
        "tag",
        "mode",
        "frame-link",
        "artifact-comment-monitor",
        "artifact-autoreact-ledger",
        "ended-by-model",
        "relocated",
        "bridge-session",
        "attribution-snapshot",
        "isolation-latch",
        "dev-mods",
        "memory-mode",
        "worktree-state",
        "observer-ref",
        "api-request-shape",
        "api-request-blob",
        "api-request",
    }
)
SYSTEM_DROPPED = frozenset(
    {
        "informational",
        "away_summary",
        "agents_killed",
        "api_error",
        "turn_duration",
        "stop_hook_summary",
        "model_refusal_fallback",
    }
)
RETRACTION_PREFIX = 24
COMPACT_METADATA = frozenset(
    {
        "trigger",
        "preTokens",
        "postTokens",
        "cumulativeDroppedTokens",
        "durationMs",
        "preCompactDiscoveredTools",
        "preservedSegment",
        "preservedMessages",
    }
)
INTERRUPTIONS = (
    "[Request interrupted by user]",
    "[Request interrupted by user for tool use]",
    (
        "[Tool call did not complete: the turn was ended to deliver the message that follows. "
        "Nothing refused it; re-run it if still needed.]"
    ),
    (
        "[Tool call interrupted: the session ended before this call's result was recorded, so its "
        "outcome is unknown. Check whether it took effect before relying on it or running it again.]"
    ),
    (
        "[Tool call result not in this copy: this session was copied from another session before "
        "that session recorded this call's result. The call may have finished there, may still be "
        "running there, or may never have run. Check whether it took effect before relying on it or "
        "running it again.]"
    ),
    (
        "The user doesn't want to take this action right now. STOP what you are doing and wait for "
        "the user to tell you how to proceed."
    ),
    (
        "[Tool call skipped: the turn was stopped before this call ran, by the check whose denial is "
        "on another call in this batch. Nothing refused this call and it had no effects; re-run it "
        "if still needed.]"
    ),
    (
        "[Tool call skipped: the turn ended to deliver the message that follows before this call "
        "ran. Nothing refused it; re-run it if still needed.]"
    ),
)
IMAGE_PLACEHOLDERS = ("[Image: ", "[Image source: ")
INTERRUPTED_RESULT = INTERRUPTIONS[3]
TOOL_REFERENCE = "claude.tool_reference"


def _images(blocks: Any) -> list[dict[str, Any]]:
    if not isinstance(blocks, list):
        return []
    images = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            images.append(block)
        elif block.get("type") == "tool_result":
            images.extend(_images(block.get("content")))
    return images


def _attachment_annotations(
    message: Message, assets: dict[str, Attachment]
) -> list[dict[str, Any]]:
    references = []
    for item in message.content:
        if isinstance(item, AttachmentRef):
            references.append(item)
        elif isinstance(item, ToolResult):
            references.extend(part for part in item.output if isinstance(part, AttachmentRef))
    return [
        {
            "sha256": sha256(assets[item.attachment_id].data).hexdigest(),
            "media_type": assets[item.attachment_id].media_type,
            "name": assets[item.attachment_id].name,
        }
        for item in references
    ]


def _content(row: dict[str, Any], message: dict[str, Any]) -> Any:
    content = deepcopy(message.get("content"))
    annotations = row.get("ringleaderAttachments")
    if annotations is None:
        return content
    images = _images(content)
    if not isinstance(annotations, list) or len(annotations) != len(images):
        raise PreservationError("Invalid Claude attachment annotations")
    for image, annotation in zip(images, annotations, strict=True):
        if (
            not isinstance(annotation, dict)
            or set(annotation) != {"sha256", "media_type", "name"}
            or not isinstance(annotation["name"], str | type(None))
        ):
            raise PreservationError("Invalid Claude attachment annotation")
        source = image.get("source", {})
        try:
            digest = sha256(base64.b64decode(source["data"], validate=True)).hexdigest()
        except (KeyError, ValueError, TypeError) as exc:
            raise PreservationError("Invalid Claude annotated image") from exc
        if annotation["sha256"] != digest or annotation["media_type"] != source.get("media_type"):
            raise PreservationError(
                "Claude attachment annotation disagrees with native bytes or MIME"
            )
        image["ringleaderName"] = annotation["name"]
    return content


def _reject_failed_images(message: Message) -> None:
    if any(
        isinstance(item, ToolResult)
        and item.is_error
        and any(isinstance(part, AttachmentRef) for part in item.output)
        for item in message.content
    ):
        raise PreservationError(
            "Claude 2.1.278 strips failed tool-result images from model context"
        )


def _message(row: dict[str, Any], identity: Identity, assets: list[Attachment]) -> Message:
    message = row.get("message")
    if not isinstance(message, dict) or message.get("role") != row["type"]:
        raise PreservationError("Invalid Claude message role")
    provenance = Provenance("authored", identity, row["uuid"])
    if row.get("isCompactSummary"):
        provenance = replace(provenance, origin="native")
    if row.get("isMeta"):
        provenance = replace(
            provenance, origin="native", evidence="isMeta=true; historical context"
        )
    result = Message(
        row["uuid"],
        row["type"],
        native.blocks(_content(row, message), assets, "claude"),
        provenance,
        timestamp=native.timestamp(row.get("timestamp"), provenance),
        hidden=bool(row.get("isMeta")),
    )
    _reject_failed_images(result)
    native.reject_assistant_images(result, "Claude")
    return native.restore(result, row)


def _boundary(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("parentUuid") is not None:
        raise PreservationError("Unsupported Claude compact boundary parent graph")
    metadata = row.get("compactMetadata", {})
    if not isinstance(metadata, dict) or set(metadata) - COMPACT_METADATA:
        raise PreservationError("Unsupported Claude retained-segment compaction metadata")
    claude_chain.validate_metadata(metadata)
    return row


def _graph(rows: list[dict[str, Any]], identity: Identity) -> list[dict[str, Any]]:
    graph = []
    for row in rows:
        kind = row.get("type")
        if row.get("sessionId") not in (None, identity.native_id):
            raise PreservationError("Claude transcript session identity mismatch")
        if row.get("isSidechain") or row.get("teamName"):
            continue
        if row.get("isVisibleInTranscriptOnly") and not row.get("isCompactSummary"):
            raise PreservationError(
                "Claude transcript-only messages require unsupported visibility semantics"
            )
        if kind in IGNORED:
            continue
        if kind == "content-replacement":
            if row.get("replacements") == []:
                continue
            raise PreservationError(
                "Claude content-replacement semantics are not verified; refusing required replacement content"
            )
        if kind not in ("user", "assistant", "system", "progress", "attachment"):
            raise PreservationError(f"Unsupported Claude transcript entry: {kind}")
        graph.append(_boundary(row) if claude_chain.is_boundary(row) else row)
    return graph


def _attachment(row: dict[str, Any], identity: Identity, human_turn: bool) -> Message | None:
    disposition = claude_attachments.disposition(row)
    if disposition == "drop":
        return None
    texts = claude_attachments.texts(row, human_turn)
    if not texts:
        return None
    evidence = f"attachment:{claude_attachments.kind_of(row)}"
    provenance = Provenance(
        "authored" if disposition == "visible" else "harness", identity, row["uuid"], evidence
    )
    return Message(
        row["uuid"],
        "user",
        tuple(Text(text) for text in texts),
        provenance,
        timestamp=native.timestamp(row.get("timestamp"), provenance),
        hidden=disposition != "visible",
    )


def _local_command(row: dict[str, Any], identity: Identity) -> Message:
    content = row.get("content")
    if not isinstance(content, str):
        raise PreservationError("Invalid Claude local command content")
    provenance = Provenance("native", identity, row["uuid"], "system:local_command")
    return Message(
        row["uuid"],
        "user",
        (Text(content),),
        provenance,
        timestamp=native.timestamp(row.get("timestamp"), provenance),
    )


def _row_message(
    row: dict[str, Any], identity: Identity, assets: list[Attachment], human_turn: bool
) -> Message | None:
    if row["type"] == "progress":
        return None
    if row["type"] == "system":
        subtype = row.get("subtype")
        if subtype == "local_command":
            return _local_command(row, identity)
        if subtype in SYSTEM_DROPPED:
            return None
        raise PreservationError(f"Unsupported Claude system content: {subtype}")
    if row["type"] == "attachment":
        return _attachment(row, identity, human_turn)
    if row.get("isVirtual") or claude_chain.synthetic_error(row):
        return None
    message = _message(row, identity, assets)
    return message if message.content else None


def _retract(path: list[dict[str, Any]]) -> list[dict[str, Any]]:
    retracted: set[str] = set()
    for row in path:
        if row["type"] == "system" and row.get("subtype") == "model_refusal_fallback":
            uuids = row.get("retractedMessageUuids", [])
            if not isinstance(uuids, list) or any(not isinstance(item, str) for item in uuids):
                raise PreservationError("Invalid Claude refusal retraction")
            retracted.update(item[:RETRACTION_PREFIX] for item in uuids)
    return [
        row
        for row in path
        if row["type"] == "system" or row["uuid"][:RETRACTION_PREFIX] not in retracted
    ]


def _interruption_text(block: Any) -> Any:
    if not isinstance(block, dict):
        return None
    if block.get("type") == "text":
        return block.get("text")
    if block.get("type") == "tool_result" and block.get("is_error") is True:
        return block.get("content")
    return None


def _interrupted(row: dict[str, Any]) -> bool:
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.startswith(INTERRUPTIONS)
    return (
        isinstance(content, list)
        and bool(content)
        and all(
            isinstance(text := _interruption_text(block), str) and text.startswith(INTERRUPTIONS)
            for block in content
        )
    )


def _image_placeholder(row: dict[str, Any]) -> bool:
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return (
        row.get("origin") is None
        and row.get("promptSource") is None
        and isinstance(content, list)
        and bool(content)
        and all(
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(text := block.get("text"), str)
            and text.startswith(IMAGE_PLACEHOLDERS)
            and text.endswith("]")
            and "\n" not in text
            for block in content
        )
    )


def _after_human_prompt(path: list[dict[str, Any]], index: int) -> bool:
    for row in reversed(path[:index]):
        if row["type"] == "assistant":
            return False
        if row["type"] != "user":
            continue
        if (
            row.get("toolUseResult") is not None
            or row.get("isCompactSummary") is True
            or _interrupted(row)
        ):
            return False
        if row.get("isVisibleInTranscriptOnly") is True:
            continue
        if row.get("isMeta") is True:
            if _image_placeholder(row):
                continue
            return False
        origin = row.get("origin")
        return (
            isinstance(origin, dict)
            and origin.get("kind") == "human"
            and row.get("verifiedSlackHumanTurn") is not True
        )
    return False


def _human_turn(path: list[dict[str, Any]]) -> set[str]:
    return {
        row["uuid"]
        for index, row in enumerate(path)
        if row["type"] == "attachment"
        and claude_attachments.kind_of(row) == "queued_command"
        and claude_attachments.task_notification(row["attachment"])
        and _after_human_prompt(path, index)
    }


def _anchor(row: dict[str, Any], compaction_stops: bool) -> bool:
    if row["type"] == "assistant":
        return True
    if compaction_stops and (row.get("isCompactSummary") or claude_chain.is_boundary(row)):
        return True
    message = row.get("message") if row["type"] == "user" else None
    content = message.get("content") if isinstance(message, dict) else None
    return row["type"] == "user" and (
        row.get("taskDelivery") is not None
        or (
            isinstance(content, list)
            and bool(content)
            and isinstance(content[0], dict)
            and content[0].get("type") == "tool_result"
        )
    )


def _hoist(path: list[dict[str, Any]], compaction_stops: bool) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for row in reversed(path):
        if row["type"] == "attachment":
            pending.append(row)
            continue
        if _anchor(row, compaction_stops):
            result.extend(pending)
            pending = []
        result.append(row)
    result.extend(pending)
    return list(reversed(result))


Converted = list[tuple[dict[str, Any], Message | None]]


def _converted(
    rows: list[dict[str, Any]],
    identity: Identity,
    assets: list[Attachment],
    compaction_stops: bool,
) -> Converted:
    rows, interrupted = claude_chain.resolved(_retract(rows))
    human_turn = _human_turn(rows)
    converted = _responses(
        [
            (
                row,
                None
                if claude_chain.is_boundary(row)
                else _row_message(row, identity, assets, row["uuid"] in human_turn),
            )
            for row in _hoist(rows, compaction_stops)
        ]
    )
    return converted + [
        (row, _interrupted_result(row, call, identity)) for row, call in interrupted
    ]


def _interrupted_result(row: dict[str, Any], call: str, identity: Identity) -> Message:
    provenance = Provenance("harness", identity, row["uuid"], "resume:interrupted-tool-result")
    return Message(
        f"interrupted-{call}",
        "user",
        (ToolResult(call, (Text(INTERRUPTED_RESULT),), True),),
        provenance,
        timestamp=native.timestamp(row.get("timestamp"), provenance),
    )


def _delivery(row: dict[str, Any]) -> bool:
    if row["type"] == "attachment":
        return True
    if row["type"] == "system":
        return not claude_chain.is_boundary(row)
    return row["type"] == "user" and (
        row.get("turnCompanion") is True or isinstance(row.get("sourceToolUseID"), str)
    )


def _responses(converted: Converted) -> Converted:
    result: Converted = []
    head: tuple[int, str, Message] | None = None
    pending_results: Converted = []
    pending_delivery: Converted = []
    for row, message in converted:
        response = claude_chain.message_id(row) if "ringleader" not in row else None
        if message is None:
            result.append((row, None))
        elif head is not None and message.role == "assistant" and response == head[1]:
            merged = replace(head[2], content=head[2].content + message.content)
            result[head[0]] = (result[head[0]][0], merged)
            head = (head[0], head[1], merged)
            result.append((row, None))
        elif (
            head is not None
            and message.role != "assistant"
            and all(isinstance(item, ToolResult) for item in message.content)
        ):
            pending_results.append((row, message))
        elif head is not None and _delivery(row):
            pending_delivery.append((row, message))
        else:
            result.extend(pending_results)
            result.extend(pending_delivery)
            pending_results = []
            pending_delivery = []
            head = (
                (len(result), response, message)
                if response and message.role == "assistant"
                else None
            )
            result.append((row, message))
    result.extend(pending_results)
    result.extend(pending_delivery)
    return result


@dataclass
class _Compaction:
    boundary: dict[str, Any]
    after: str
    summary: list[Message] = field(default_factory=list)
    retained_ids: list[str] = field(default_factory=list)
    retained_context: list[Message] = field(default_factory=list)

    def checkpoint(self, preserved: tuple[str, ...], history: list[Message]) -> Checkpoint:
        if not self.summary:
            raise PreservationError("Claude compact boundary is missing its summary")
        ids = self.retained_ids
        if "ringleaderRetainedContext" not in self.boundary:
            positions = {message.id: index for index, message in enumerate(history)}
            ids = sorted((key for key in preserved if key in positions), key=positions.__getitem__)
        return Checkpoint(
            self.boundary["uuid"],
            self.after,
            native.linked(self.summary),
            tuple(ids),
            native.linked(self.retained_context)
            if self.boundary.get("ringleaderRetainedContext")
            else None,
        )

    def retained_copy(self, row: dict[str, Any], message: Message, history: list[Message]) -> None:
        if not self.summary:
            raise PreservationError("Invalid Claude retained-history annotation")
        original = next((item for item in history if item.id == row["ringleaderRetained"]), None)
        if original is None or replace(message, id=original.id, parent_id=None) != replace(
            original, parent_id=None
        ):
            raise PreservationError(
                "Claude retained-history copy disagrees with native historical content"
            )
        self.retained_ids.append(original.id)

    def owned(self, message: Message) -> None:
        if not self.summary or not self.boundary.get("ringleaderRetainedContext"):
            raise PreservationError("Invalid Claude retained-context annotation")
        self.retained_context.append(message)


def _history(
    converted: Converted, preserved: dict[str, tuple[str, ...]]
) -> tuple[list[Message], list[Checkpoint], list[Message]]:
    history: list[Message] = []
    every: list[Message] = []
    checkpoints: list[Checkpoint] = []
    current: _Compaction | None = None
    for row, message in converted:
        if claude_chain.is_boundary(row):
            if current is not None:
                checkpoints.append(current.checkpoint(preserved[current.boundary["uuid"]], history))
            if not history:
                raise PreservationError("Claude compaction has no historical boundary")
            current = _Compaction(row, history[-1].id)
            continue
        if message is None:
            continue
        if row.get("isCompactSummary"):
            if current is None:
                raise PreservationError("Claude summary lacks a compact boundary")
            current.summary.append(message)
        elif "ringleaderRetained" in row:
            if current is None:
                raise PreservationError("Invalid Claude retained-history annotation")
            current.retained_copy(row, message, history)
        elif row.get("ringleaderRetainedContext"):
            if current is None:
                raise PreservationError("Invalid Claude retained-context annotation")
            current.owned(message)
        elif current is None or current.summary or not message.hidden:
            if current is not None:
                checkpoints.append(current.checkpoint(preserved[current.boundary["uuid"]], history))
                current = None
            every.append(message)
            if not message.hidden:
                history.append(message)
    if current is not None:
        checkpoints.append(current.checkpoint(preserved[current.boundary["uuid"]], history))
    return history, checkpoints, every


def _native_retained(
    retained: list[Message], history: list[Message], after: str
) -> tuple[tuple[str, ...], tuple[Message, ...] | None]:
    positions = {message.id: index for index, message in enumerate(history)}
    places = [positions.get(message.id, -1) for message in retained]
    if places == sorted(places) and all(
        0 <= place <= positions[after]
        and native.unparented([history[place]]) == native.unparented([message])
        for place, message in zip(places, retained, strict=True)
    ):
        return tuple(message.id for message in retained), None
    return (), native.linked(retained)


def _retained_row(row: dict[str, Any], preserved: set[str]) -> bool:
    return (
        row["uuid"] in preserved
        or "ringleaderRetained" in row
        or bool(row.get("ringleaderRetainedContext"))
    )


def _latest(
    converted: Converted,
    history: list[Message],
    previous: Checkpoint,
    preserved: tuple[str, ...],
) -> tuple[Checkpoint, tuple[Message, ...]]:
    boundary = next(row for row, _ in converted if claude_chain.is_boundary(row))
    items = [(row, message) for row, message in converted if message is not None]
    marks = [index for index, (row, _) in enumerate(items) if row.get("isCompactSummary")]
    if not marks:
        raise PreservationError("Claude compact boundary is missing its summary")
    kept = set(preserved)
    split = max(
        [
            marks[-1],
            *(index for index, (row, _) in enumerate(items) if _retained_row(row, kept)),
        ]
    )
    messages = [message for _, message in items]
    summary, retained, following = (
        messages[: marks[-1] + 1],
        messages[marks[-1] + 1 : split + 1],
        messages[split + 1 :],
    )
    if "ringleaderRetainedContext" in boundary:
        ids, owned = previous.retained_message_ids, previous.retained_context
    else:
        ids, owned = _native_retained(retained, history, previous.after_message_id)
    by_id = {message.id: message for message in history}
    cut = [message.id for message in history].index(previous.after_message_id) + 1
    explicit = boundary.get("ringleaderFollowingContext") is True or native.unparented(
        following
    ) != native.unparented(history[cut:])
    checkpoint = replace(
        previous,
        summary=native.linked(summary),
        retained_message_ids=ids,
        retained_context=owned,
        following_context=native.linked(following) if explicit else None,
    )
    context = native.linked(
        [*summary, *(owned if owned is not None else (by_id[key] for key in ids)), *following]
    )
    return checkpoint, context


def read(data: bytes, identity: Identity, branch_id: str | None = None) -> Conversation:
    if identity.agent != "claude-code":
        raise PreservationError("Source identity does not belong to claude-code")
    rows = native.records(data)
    entries = claude_chain.index(_graph(rows, identity))
    leaf = claude_chain.leaf(entries, branch_id)
    chain = claude_chain.recover(entries, claude_chain.walk(entries, leaf))
    boundaries = [row for row in chain if claude_chain.is_boundary(row)]
    preserved = {
        row["uuid"]: (claude_chain.preserved(row, entries.rows) or ("", ()))[1]
        for row in boundaries
    }
    assets: list[Attachment] = []
    history, checkpoints, every = _history(_converted(chain, identity, assets, True), preserved)
    context = native.linked(every)
    if boundaries:
        latest = boundaries[-1]
        checkpoints[-1], context = _latest(
            _converted(claude_chain.context(entries, leaf, latest, chain), identity, assets, False),
            history,
            checkpoints[-1],
            preserved[latest["uuid"]],
        )
    titles = [
        row["customTitle"]
        for row in rows
        if row.get("type") == "custom-title" and isinstance(row.get("customTitle"), str)
    ] or [
        row["aiTitle"]
        for row in rows
        if row.get("type") == "ai-title" and isinstance(row.get("aiTitle"), str)
    ]
    last = entries.rows[leaf]
    result = Conversation(
        identity,
        leaf,
        native.linked(history),
        context,
        tuple(checkpoints),
        tuple(assets),
        titles[-1] if titles else None,
        source_created_at=native.timestamp(
            chain[0].get("timestamp"), Provenance("native", identity, chain[0]["uuid"])
        ),
        source_updated_at=native.timestamp(
            last.get("timestamp"), Provenance("native", identity, leaf)
        ),
    )
    metadata = [row["ringleaderSession"] for row in rows if "ringleaderSession" in row]
    if len(metadata) > 1:
        raise PreservationError("Duplicate Claude session annotations")
    result = native.restore_session(result, metadata[0] if metadata else None)
    validate(result)
    return result


def _checkpoint_stream(conversation: Conversation) -> tuple[Message, ...]:
    *earlier, latest = conversation.checkpoints
    if any(checkpoint.following_context is not None for checkpoint in earlier):
        raise CheckpointShape(
            "This native profile cannot preserve an independent post-checkpoint model stream"
        )
    if any(
        item.role != "user"
        for checkpoint in conversation.checkpoints
        for item in checkpoint.summary
    ):
        raise CheckpointShape("Claude compact summaries require user messages")
    if latest.following_context is None:
        return conversation.history
    cut = [message.id for message in conversation.history].index(latest.after_message_id) + 1
    visible = [message for message in latest.following_context if not message.hidden]
    if native.unparented(visible) != native.unparented(conversation.history[cut:]):
        raise CheckpointShape(
            "Claude cannot preserve an independent post-checkpoint model stream that hides visible history"
        )
    return (*conversation.history[:cut], *latest.following_context)


def cli_version(output: str) -> str:
    match = re.fullmatch(r"(\S+) \(Claude Code\)", output.strip())
    return match.group(1) if match else output.strip()


class ClaudeAdapter:
    profile = PROFILE

    def __init__(
        self, executable: str = "claude", verified: tuple[VerificationRecord, ...] = ()
    ) -> None:
        self.executable = executable
        self.verified = local_versions(verified, "claude-code")

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
        result = native.probe_version(self.executable, "claude-code", runner)
        output = result.stdout.decode(errors="replace").strip()
        return native.version_compatibility(
            "Claude writer",
            PROFILE,
            check_version(
                cli_version(output) if result.returncode == 0 else "", PINNED, self.verified
            ),
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
                "Claude keeps one stream: the model context, with isMeta rows for hidden messages",
                hides=True,
            ),
        )

    def stage(self, conversation: Conversation, request: TargetRequest) -> NativeStage:
        validate(conversation)
        native.reject_opaque(conversation, frozenset({("claude-code", TOOL_REFERENCE)}))
        native.safe_id(request.identity, "claude-code")
        try:
            UUID(request.identity.native_id)
        except ValueError as exc:
            raise PreservationError("Claude session ID must be a UUID") from exc
        if request.profile != PROFILE:
            raise PreservationError("Unverified Claude writer profile")
        applied = self.normalizations(PROFILE)
        try:
            stream = _checkpoint_stream(conversation) if conversation.checkpoints else None
        except CheckpointShape:
            conversation = flattened(conversation)
            stream = None
            applied = (*applied, flattened_checkpoint(PROFILE.name))
        written = model_view(conversation, hides=True)
        if not written.history:
            raise PreservationError("Claude requires a nonempty conversation")
        slug = re.sub(
            r"[^a-zA-Z0-9]", "-", unicodedata.normalize("NFC", str(request.roots.cwd.resolve()))
        )
        if len(slug) > MAX_SLUG_LENGTH:
            raise PreservationError("Claude cwd exceeds verified native project slug length")
        now_time = datetime.now(UTC)
        now = now_time.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        assets = {item.id: item for item in conversation.attachments}
        stream = stream if stream is not None else written.context
        ids = {item.id: str(uuid4()) for item in stream}
        rows: list[dict[str, Any]] = [
            {
                "type": "custom-title",
                "sessionId": request.identity.native_id,
                "customTitle": request.title,
                "timestamp": now,
            }
        ]
        parent: str | None = None

        def emit(message: Message, key: str, **extra: Any) -> None:
            nonlocal parent
            _reject_failed_images(message)
            native.reject_assistant_images(message, "Claude")
            historical = (
                native.historical_time(message, now_time).isoformat().replace("+00:00", "Z")
            )
            role = native.native_role(message)
            if role == "tool" and all(isinstance(item, ToolResult) for item in message.content):
                role = "user"
            if role not in ("user", "assistant"):
                raise PreservationError(
                    "Claude requires user/assistant history; historical system policy cannot be activated"
                )
            message_data: dict[str, Any] = {
                "role": role,
                "content": native.emit_blocks(message.content, assets, "claude"),
            }
            if role == "assistant":
                message_data.update(
                    id=f"msg_{uuid4().hex}",
                    type="message",
                    model="claude-sonnet-4-5",
                    stop_reason="tool_use"
                    if any(isinstance(item, ToolCall) for item in message.content)
                    else "end_turn",
                    stop_sequence=None,
                    usage={"input_tokens": 0, "output_tokens": 0},
                )
            rows.append(
                {
                    "type": role,
                    "uuid": key,
                    "parentUuid": parent,
                    "sessionId": request.identity.native_id,
                    "cwd": str(request.roots.cwd),
                    "timestamp": historical,
                    "isSidechain": False,
                    "userType": "external",
                    "version": "2.1.278",
                    "message": message_data,
                    "ringleader": native.message_annotations(message, freeform_calls=True),
                    "ringleaderAttachments": _attachment_annotations(message, assets),
                    **({"isMeta": True} if message.hidden else {}),
                    **extra,
                }
            )
            parent = key

        for message in stream:
            emit(message, ids[message.id])
            for checkpoint in conversation.checkpoints:
                if checkpoint.after_message_id != message.id:
                    continue
                key = str(uuid4())
                rows.append(
                    {
                        "type": "system",
                        "subtype": "compact_boundary",
                        "uuid": key,
                        "parentUuid": None,
                        "logicalParentUuid": parent,
                        "sessionId": request.identity.native_id,
                        "cwd": str(request.roots.cwd),
                        "timestamp": now,
                        "isSidechain": False,
                        "content": "Conversation compacted",
                        "compactMetadata": {"trigger": "manual", "preTokens": 0},
                        "ringleaderRetainedContext": checkpoint.retained_context is not None,
                        **(
                            {"ringleaderFollowingContext": True}
                            if checkpoint.following_context is not None
                            else {}
                        ),
                    }
                )
                parent = key
                for summary in checkpoint.summary:
                    if summary.role != "user":
                        raise PreservationError("Claude compact summaries require user messages")
                    emit(summary, str(uuid4()), isCompactSummary=True)
                if checkpoint.retained_context is not None:
                    for retained in checkpoint.retained_context:
                        emit(retained, str(uuid4()), ringleaderRetainedContext=True)
                else:
                    by_id = {item.id: item for item in conversation.history}
                    for retained_id in checkpoint.retained_message_ids:
                        emit(by_id[retained_id], str(uuid4()), ringleaderRetained=ids[retained_id])
        rows.append(
            {
                "type": "custom-title",
                "sessionId": request.identity.native_id,
                "customTitle": request.title,
                "ringleaderSession": native.session_metadata(conversation),
            }
        )
        ref = f"projects/{slug}/{request.identity.native_id}.jsonl"
        return NativeStage(
            request.identity,
            PROFILE,
            request.roots,
            (NativeFile(ref, native.jsonl(rows), True),),
            ref,
            normalizations=applied,
        )

    def stage_metadata(self, stage: NativeStage) -> NativeMetadata:
        rows = native.records(
            next(item.data for item in stage.files if item.relative_path == stage.source_ref)
        )
        messages = [row for row in rows if "cwd" in row]
        titles = [row for row in rows if row.get("type") == "custom-title"]
        if not messages or not titles or any(row["cwd"] != messages[0]["cwd"] for row in messages):
            raise PreservationError("Claude staged picker metadata is inconsistent")
        return NativeMetadata(
            Identity("claude-code", titles[-1]["sessionId"]),
            messages[0]["cwd"],
            titles[-1]["customTitle"],
        )

    def read_stage(self, stage: NativeStage) -> Conversation:
        if stage.profile != PROFILE:
            raise PreservationError("Unverified native staged profile")
        return read(
            next(item.data for item in stage.files if item.relative_path == stage.source_ref),
            stage.identity,
        )

    def launch_request(self, identity: Identity, roots: NativeRoots) -> RunRequest:
        native.safe_id(identity, "claude-code")
        return native.launch_request(
            self.executable, ("--resume", identity.native_id), roots, "claude-code"
        )
