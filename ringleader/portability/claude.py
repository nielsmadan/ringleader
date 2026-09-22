from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

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
    Checkpoint,
    Conversation,
    Identity,
    Message,
    PreservationError,
    Provenance,
    ToolCall,
    ToolResult,
    validate,
)
from .snapshot import SourceSnapshot, snapshot_files

MAX_SLUG_LENGTH = 200

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
IGNORED = {
    "file-history-snapshot",
    "queue-operation",
    "last-prompt",
    "custom-title",
    "summary",
    "agent-name",
    "agent-color",
    "pr-link",
    "tag",
    "mode",
}
SYSTEM_TELEMETRY = {"turn_duration", "api_error", "stop_hook_summary", "local_command"}


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
        native.blocks(message.get("content"), assets, "claude"),
        provenance,
        timestamp=native.timestamp(row.get("timestamp"), provenance),
    )
    native.reject_assistant_images(result, "Claude")
    return native.restore(result, row)


def _graph(rows: list[dict[str, Any]], identity: Identity) -> list[dict[str, Any]]:
    graph = []
    for row in rows:
        kind = row.get("type")
        if row.get("sessionId") not in (None, identity.native_id):
            raise PreservationError("Claude transcript session identity mismatch")
        if row.get("isSidechain") or row.get("teamName"):
            continue
        if row.get("isVisibleInTranscriptOnly"):
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
        if kind == "system" and row.get("subtype") == "compact_boundary":
            if row.get("parentUuid") is not None or not row.get("logicalParentUuid"):
                raise PreservationError("Unsupported Claude compact boundary parent graph")
            metadata = row.get("compactMetadata", {})
            if not isinstance(metadata, dict) or set(metadata) - {"trigger", "preTokens"}:
                raise PreservationError("Unsupported Claude retained-segment compaction metadata")
            graph.append({**row, "parentUuid": row["logicalParentUuid"]})
        else:
            graph.append(row)
    return graph


def _irrelevant(row: dict[str, Any]) -> bool:
    if row["type"] == "progress":
        return True
    if row["type"] == "system":
        if row.get("subtype") in SYSTEM_TELEMETRY - {"api_error", "local_command"}:
            return True
        raise PreservationError(f"Unsupported Claude system content: {row.get('subtype')}")
    if row["type"] == "attachment":
        raise PreservationError(
            "Claude attachment records are unsupported; embedded images are supported"
        )
    return False


def _history(
    path: list[dict[str, Any]], identity: Identity
) -> tuple[list[Message], list[Checkpoint], list[Attachment], list[Message]]:
    assets: list[Attachment] = []
    history: list[Message] = []
    uncompacted_context: list[Message] = []
    checkpoints: list[Checkpoint] = []
    boundary: dict[str, Any] | None = None
    summaries: list[Message] = []
    retained_ids: list[str] = []
    retained_context: list[Message] = []

    def finish_checkpoint() -> None:
        nonlocal boundary, summaries, retained_ids, retained_context
        if boundary is not None:
            if not summaries:
                raise PreservationError("Claude compact boundary is missing its summary")
            checkpoints.append(
                Checkpoint(
                    boundary["uuid"],
                    boundary["after"],
                    native.linked(summaries),
                    tuple(retained_ids),
                    native.linked(retained_context)
                    if boundary.get("ringleaderRetainedContext")
                    else None,
                )
            )
            boundary, summaries, retained_ids, retained_context = None, [], [], []

    for row in path:
        kind = row["type"]
        if kind == "system" and row.get("subtype") == "compact_boundary":
            finish_checkpoint()
            if not history:
                raise PreservationError("Claude compaction has no historical boundary")
            boundary = {**row, "after": history[-1].id}
            continue
        if _irrelevant(row):
            continue
        message = _message(row, identity, assets)
        if row.get("isCompactSummary"):
            if boundary is None:
                raise PreservationError("Claude summary lacks a compact boundary")
            summaries.append(message)
        elif "ringleaderRetained" in row:
            if boundary is None or not summaries:
                raise PreservationError("Invalid Claude retained-history annotation")
            original = next(
                (item for item in history if item.id == row["ringleaderRetained"]), None
            )
            if original is None or replace(message, id=original.id, parent_id=None) != replace(
                original, parent_id=None
            ):
                raise PreservationError(
                    "Claude retained-history copy disagrees with native historical content"
                )
            retained_ids.append(original.id)
        elif row.get("ringleaderRetainedContext"):
            if boundary is None or not summaries or not boundary.get("ringleaderRetainedContext"):
                raise PreservationError("Invalid Claude retained-context annotation")
            retained_context.append(message)
        else:
            finish_checkpoint()
            uncompacted_context.append(message)
            if not row.get("isMeta"):
                history.append(message)
    finish_checkpoint()
    if checkpoints and any(row.get("isMeta") for row in path):
        raise PreservationError(
            "Claude hidden context combined with compaction requires unsupported visibility semantics"
        )
    return history, checkpoints, assets, uncompacted_context


def _default_leaf(graph: list[dict[str, Any]]) -> str:
    by_id = {row["uuid"]: row for row in graph}
    parents = {row.get("parentUuid") for row in graph}
    candidates = []
    for terminal in graph:
        if terminal["uuid"] in parents:
            continue
        row = terminal
        while row["type"] not in ("user", "assistant") and row.get("parentUuid"):
            row = by_id[row["parentUuid"]]
        if row["type"] in ("user", "assistant"):
            candidates.append(row)
    preferred = [row for row in candidates if not row.get("isMeta")] or candidates
    if not preferred:
        raise PreservationError("Claude transcript has no conversational leaf")
    positions = {row["uuid"]: i for i, row in enumerate(graph)}
    return max(preferred, key=lambda row: positions[row["uuid"]])["uuid"]


def read(data: bytes, identity: Identity, branch_id: str | None = None) -> Conversation:
    if identity.agent != "claude-code":
        raise PreservationError("Source identity does not belong to claude-code")
    rows = native.records(data)
    graph = _graph(rows, identity)
    native.chain(graph, "uuid", "parentUuid", branch_id)
    if branch_id is None:
        branch_id = _default_leaf(graph)
    path = native.chain(graph, "uuid", "parentUuid", branch_id)
    history, checkpoints, assets, uncompacted_context = _history(path, identity)
    history_tuple = native.linked(history)
    context = native.linked(uncompacted_context)
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
    titles = [
        row["customTitle"]
        for row in rows
        if row.get("type") == "custom-title" and isinstance(row.get("customTitle"), str)
    ]
    result = Conversation(
        identity,
        path[-1]["uuid"],
        history_tuple,
        context,
        tuple(checkpoints),
        tuple(assets),
        titles[-1] if titles else None,
        source_created_at=native.timestamp(
            path[0].get("timestamp"), Provenance("native", identity, path[0]["uuid"])
        ),
        source_updated_at=native.timestamp(
            path[-1].get("timestamp"), Provenance("native", identity, path[-1]["uuid"])
        ),
    )
    metadata = [row["ringleaderSession"] for row in rows if "ringleaderSession" in row]
    if len(metadata) > 1:
        raise PreservationError("Duplicate Claude session annotations")
    result = native.restore_session(result, metadata[0] if metadata else None)
    validate(result)
    return result


class ClaudeAdapter:
    def __init__(self, executable: str = "claude") -> None:
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
        result = native.probe_version(self.executable, "claude-code", runner)
        version = result.stdout.decode(errors="replace").strip()
        supported = result.returncode == 0 and version == "2.1.278 (Claude Code)"
        return Compatibility(
            supported,
            PROFILE if supported else None,
            () if supported else (f"Claude writer requires verified 2.1.278; found {version!r}",),
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
        if any(checkpoint.following_context is not None for checkpoint in conversation.checkpoints):
            raise PreservationError(
                "This native profile cannot preserve an independent post-checkpoint model stream"
            )
        native.safe_id(request.identity, "claude-code")
        try:
            UUID(request.identity.native_id)
        except ValueError as exc:
            raise PreservationError("Claude session ID must be a UUID") from exc
        if request.profile != PROFILE:
            raise PreservationError("Unverified Claude writer profile")
        if not conversation.history:
            raise PreservationError("Claude requires a nonempty conversation")
        if not conversation.checkpoints and native.linked(conversation.context) != native.linked(
            conversation.history
        ):
            raise PreservationError(
                "Claude cannot represent distinct history/context without a checkpoint"
            )
        slug = re.sub(
            r"[^a-zA-Z0-9]", "-", unicodedata.normalize("NFC", str(request.roots.cwd.resolve()))
        )
        if len(slug) > MAX_SLUG_LENGTH:
            raise PreservationError("Claude cwd exceeds verified native project slug length")
        now_time = datetime.now(UTC)
        now = now_time.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        assets = {item.id: item for item in conversation.attachments}
        ids = {item.id: str(uuid4()) for item in conversation.history}
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
                    "ringleader": native.message_annotations(message),
                    **extra,
                }
            )
            parent = key

        for message in conversation.history:
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
        return native.run_request(
            self.executable, ("--resume", identity.native_id), roots, "claude-code"
        )
