from __future__ import annotations

import base64
import os
import re
import secrets
import sqlite3
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from . import native
from . import opencode_state as state
from .capabilities import (
    Compatibility,
    FormatProfile,
    NativeMetadata,
    NativeRoots,
    NativeStage,
    Runner,
    RunRequest,
    SourceRequest,
    TargetRequest,
)
from .compare import (
    CheckpointShape,
    Normalization,
    compare,
    flattened,
    flattened_checkpoint,
    joined_tool_output,
    model_view,
)
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
from .snapshot import SnapshotFile, SourceSnapshot, database_snapshot, snapshot_files
from .verification import VerificationRecord, local_schemas

PROFILE = FormatProfile(
    "opencode",
    "opencode-1.18.30-v1",
    "1.18.30",
    (
        "https://github.com/anomalyco/opencode/blob/v1.18.30/packages/schema/src/v1/session.ts",
        "https://github.com/anomalyco/opencode/blob/v1.18.30/packages/opencode/src/session/message-v2.ts",
        "https://github.com/anomalyco/opencode/blob/v1.18.30/packages/core/src/database/schema.gen.ts",
    ),
)
QUESTION = "What did we do so far?"
TOOL_TIME_EVIDENCE = "opencode-tool-times:"
SUMMARY_MESSAGES = 2
COMBINED_GROUP = 2
RESUMED_AGENT = "build"
MODEL_KEYS = frozenset({"model", "agent", "mode", "default_agent"})
ZERO_TOKENS = {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
MEDIA = {"image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf"}
INTERRUPTED = "[Tool execution was interrupted]"
DROPPED_TOOL_METADATA = frozenset(
    {
        "count",
        "description",
        "diagnostics",
        "diff",
        "dir",
        "display",
        "exists",
        "exit",
        "filediff",
        "filepath",
        "interrupted",
        "loaded",
        "matches",
        "model",
        "name",
        "output",
        "outputPath",
        "parentSessionId",
        "preview",
        "sessionId",
        "todos",
        "truncated",
    }
)


class IDs:
    def __init__(self) -> None:
        self.value = int(time.time() * 1000) * 4096

    def new(self, prefix: str) -> str:
        self.value += 1
        suffix = "".join(
            secrets.choice("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
            for _ in range(14)
        )
        return f"{prefix}_{self.value & ((1 << 48) - 1):012x}{suffix}"


def target_id(identity: Identity) -> None:
    if identity.agent != "opencode" or not re.fullmatch(
        r"ses_[0-9a-f]{12}[A-Za-z0-9]{14}", identity.native_id
    ):
        raise PreservationError("OpenCode requires a fresh sortable ses_ native identity")


def fields(value: dict[str, Any], allowed: str, label: str) -> None:
    unknown = set(value) - set(allowed.split())
    if unknown:
        raise PreservationError(
            f"Unsupported OpenCode {label} fields: {', '.join(sorted(unknown))}"
        )


def object_value(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PreservationError(f"Invalid OpenCode {label}")
    return value


def _time(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise PreservationError(f"Invalid OpenCode {label} timestamp")
    return value


def _file(part: dict[str, Any], assets: list[Attachment], files: dict[str, bytes]) -> AttachmentRef:
    fields(part, "id sessionID messageID type mime filename url source", "file")
    mime = native.string(part.get("mime"), "file MIME")
    if mime not in MEDIA:
        raise PreservationError(f"Unsupported OpenCode file transformation/MIME: {mime}")
    url = native.string(part.get("url"), "file URL")
    if url.startswith("data:"):
        match = re.fullmatch(r"data:([^;,]+);base64,(.*)", url, re.DOTALL)
        if not match or match[1] != mime:
            raise PreservationError("OpenCode attachment needs matching base64 MIME")
        try:
            data = base64.b64decode(match[2], validate=True)
        except ValueError as exc:
            raise PreservationError("Invalid OpenCode attachment base64") from exc
    elif url.startswith("file:"):
        parsed = urlsplit(url)
        if parsed.netloc not in ("", "localhost"):
            raise PreservationError("Remote OpenCode attachment is not portable")
        path = str(Path(unquote(parsed.path)).resolve())
        if path not in files:
            raise PreservationError(
                "OpenCode attachment was not included in stable source snapshot"
            )
        data = files[path]
    else:
        raise PreservationError("Remote OpenCode attachments require owned bytes")
    if len(data) > native.MAX_BYTES:
        raise PreservationError("OpenCode attachment exceeds snapshot bound")
    asset = Attachment(f"asset-{len(assets)}", mime, data, part.get("filename"))
    previous = next(
        (
            a
            for a in assets
            if (a.data, a.media_type, a.name) == (asset.data, asset.media_type, asset.name)
        ),
        None,
    )
    if previous is None:
        assets.append(asset)
    return AttachmentRef((previous or asset).id)


def _tool_metadata(part: dict[str, Any]) -> None:
    metadata = object_value(part.get("metadata") or {}, "tool part metadata")
    provider = object_value(metadata.get("openrouter", {}), "tool part provider metadata")
    unknown = sorted(set(metadata) - {"openrouter"}) + sorted(set(provider) - {"reasoning_details"})
    if unknown:
        raise PreservationError(
            "OpenCode provider tool metadata requires a proven context profile: "
            + ", ".join(unknown)
        )


def _tool_time(tool_state: dict[str, Any], created: int) -> dict[str, Any]:
    if tool_state.get("status") == "pending":
        result: dict[str, Any] = {"start": created, "end": created}
    else:
        times = object_value(tool_state.get("time"), "tool times")
        fields(times, "start end compacted", "tool times")
        start = _time(times.get("start"), "tool start")
        if tool_state.get("status") == "running":
            result = {"start": start, "end": start}
        elif times.get("end") is None:
            raise PreservationError("Incomplete OpenCode tool timing")
        else:
            result = {"start": start, "end": _time(times.get("end"), "tool end")}
    loaded = object_value(tool_state.get("metadata") or {}, "tool metadata").get("loaded")
    if loaded is not None:
        result["loaded"] = loaded
    return result


def _tool_output(tool_state: dict[str, Any], metadata: dict[str, Any]) -> tuple[str, bool]:
    status = tool_state.get("status")
    if status in ("pending", "running"):
        return INTERRUPTED, True
    if (status == "error" and "output" in tool_state) or (
        status == "completed" and "error" in tool_state
    ):
        raise PreservationError("OpenCode tool state has contradictory output/error fields")
    if status == "completed":
        return native.string(tool_state.get("output"), "tool output"), False
    if metadata.get("interrupted") is True and isinstance(metadata.get("output"), str):
        return metadata["output"], False
    return native.string(tool_state.get("error"), "tool output"), True


def _tool(
    part: dict[str, Any], assets: list[Attachment], files: dict[str, bytes]
) -> tuple[ToolCall, ToolResult]:
    fields(part, "id sessionID messageID type callID tool state metadata", "tool part")
    _tool_metadata(part)
    tool_state = object_value(part.get("state"), "tool state")
    fields(
        tool_state, "status input output title metadata time attachments error raw", "tool state"
    )
    status = tool_state.get("status")
    if status not in ("completed", "error", "running", "pending"):
        raise PreservationError(f"Unknown OpenCode tool state: {status}")
    if object_value(tool_state.get("time", {}), "tool times").get("compacted"):
        raise PreservationError("OpenCode tool time.compacted changes active model output")
    metadata = object_value(tool_state.get("metadata", {}), "tool metadata")
    unknown = set(metadata) - DROPPED_TOOL_METADATA
    if unknown:
        raise PreservationError(
            "Unsupported OpenCode tool-state metadata with unverified visible/context semantics: "
            + ", ".join(sorted(unknown))
        )
    args = object_value(tool_state.get("input"), "tool input")
    call = ToolCall(
        native.string(part.get("callID"), "callID"),
        native.string(part.get("tool"), "tool name"),
        state.encode(args).decode(),
        "json",
    )
    text, is_error = _tool_output(tool_state, metadata)
    output: list[Text | AttachmentRef] = [Text(text)]
    attachments = tool_state.get("attachments", []) if status == "completed" else []
    if status != "completed" and tool_state.get("attachments"):
        raise PreservationError("OpenCode error tool attachments are outside the native schema")
    for item in attachments:
        if not native.string(item.get("url"), "tool attachment URL").startswith("data:"):
            raise PreservationError(
                "OpenCode tool attachments must be embedded for native model context"
            )
        output.append(_file(item, assets, files))
    return call, ToolResult(call.call_id, tuple(output), is_error)


def _part_content(
    part: dict[str, Any], role: str, assets: list[Attachment], files: dict[str, bytes]
) -> Content | None:
    kind = part.get("type")
    if kind == "text":
        fields(
            part, "id sessionID messageID type text synthetic ignored time metadata", "text part"
        )
        if part.get("ignored"):
            raise PreservationError(
                "OpenCode ignored text differs between visible history and model context"
            )
        if part.get("metadata"):
            raise PreservationError(
                "OpenCode text provider metadata requires a proven context profile"
            )
        text = native.string(part.get("text"), "text")
        if not text:
            raise PreservationError("OpenCode empty text has native context filtering semantics")
        return Text(text)
    if kind == "file":
        if role != "user":
            raise PreservationError(
                "OpenCode assistant file parts are omitted from native model context"
            )
        return _file(part, assets, files)
    if kind == "compaction":
        fields(part, "id sessionID messageID type auto overflow tail_start_id", "compaction")
        if role != "user":
            raise PreservationError("OpenCode compaction part must belong to a user message")
        return Text(QUESTION)
    ignored = {
        "reasoning": "id sessionID messageID type text metadata time",
        "step-start": "id sessionID messageID type snapshot",
        "step-finish": "id sessionID messageID type reason snapshot cost tokens",
        "snapshot": "id sessionID messageID type snapshot",
        "patch": "id sessionID messageID type hash files",
    }
    if kind in ignored:
        fields(part, ignored[kind], kind)
        return None
    raise PreservationError(f"Unsupported OpenCode conversational part: {kind}")


def _skipped_assistant(info: dict[str, Any], parts: list[dict[str, Any]]) -> bool:
    error = info.get("error")
    if error is None:
        return False
    aborted = object_value(error, "message error").get("name") == "MessageAbortedError"
    return not (
        aborted and any(part.get("type") not in ("step-start", "reasoning") for part in parts)
    )


def _message(
    row: dict[str, Any], identity: Identity, assets: list[Attachment], files: dict[str, bytes]
) -> tuple[Message, ...]:
    info = object_value(row.get("info"), "message")
    fields(
        info,
        "id sessionID role time format summary agent model system tools parentID modelID providerID mode path cost tokens structured variant finish error",
        "message",
    )
    key = native.string(info.get("id"), "message ID")
    role = info.get("role")
    if role not in ("user", "assistant"):
        raise PreservationError(f"Unknown OpenCode role: {role}")
    if role == "user":
        if info.get("error") is not None:
            raise PreservationError("OpenCode user message error is unsupported")
    elif _skipped_assistant(info, row["parts"]):
        return ()
    if info.get("structured") is not None:
        raise PreservationError("OpenCode structured assistant output is unsupported")
    if info.get("system"):
        raise PreservationError(
            "OpenCode historical user.system context instructions need a separate context profile"
        )
    if info.get("format", {}).get("type", "text") != "text":
        raise PreservationError("OpenCode structured output format is unsupported")
    times = object_value(info.get("time"), "message time")
    fields(times, "created completed", "message time")
    created = _time(times.get("created"), "message creation")
    tool_times = {
        p["callID"]: _tool_time(object_value(p.get("state"), "tool state"), created)
        for p in row["parts"]
        if p.get("type") == "tool"
    }
    evidence = TOOL_TIME_EVIDENCE + state.encode(tool_times).decode() if tool_times else ""
    provenance = Provenance("authored", identity, key, evidence)
    content: list[Content] = []
    results: list[ToolResult] = []
    seen_tool = False
    for part in row["parts"]:
        if part.get("type") == "tool":
            if role != "assistant":
                raise PreservationError("OpenCode tool part must belong to an assistant")
            call, result = _tool(part, assets, files)
            content.append(call)
            results.append(result)
            seen_tool = True
        else:
            item = _part_content(part, role, assets, files)
            if item is not None:
                if seen_tool:
                    raise PreservationError(
                        "OpenCode prose after tools in one native message needs step-aware context grouping"
                    )
                content.append(item)
    if not content:
        return ()
    message = Message(
        key,
        role,
        tuple(content),
        provenance,
        timestamp=native.timestamp(times.get("created"), provenance),
    )
    return (
        message,
        *(
            Message(
                _result_id(key, index),
                "tool",
                (result,),
                provenance,
                timestamp=native.timestamp(tool_times[result.call_id]["end"], provenance),
            )
            for index, result in enumerate(results)
        ),
    )


def _result_id(key: str, index: int) -> str:
    return f"{key}:results" if index == 0 else f"{key}:results:{index}"


def _checkpoint(
    rows: list[dict[str, Any]], groups: dict[str, tuple[Message, ...]]
) -> tuple[Checkpoint, ...]:
    checkpoints = []
    by_id = {row["info"]["id"]: index for index, row in enumerate(rows)}
    for index, row in enumerate(rows):
        parts = [p for p in row["parts"] if p["type"] == "compaction"]
        if not parts:
            continue
        if len(parts) != 1 or len(row["parts"]) != 1:
            raise PreservationError("OpenCode compaction trigger with mixed parts is unsupported")
        parent = row["info"]["id"]
        summaries = [
            r
            for r in rows[index + 1 :]
            if r["info"].get("summary") and r["info"].get("parentID") == parent
        ]
        if len(summaries) != 1:
            raise PreservationError("Incomplete or ambiguous OpenCode compaction summary")
        summary = summaries[0]
        if summary["info"].get("error") is not None or not summary["info"].get("finish"):
            raise PreservationError("OpenCode compaction summary is incomplete or failed")
        if by_id[summary["info"]["id"]] != index + 1:
            raise PreservationError("OpenCode compaction summary is not adjacent to its trigger")
        tail = parts[0].get("tail_start_id")
        start = by_id.get(tail, -1) if tail else index
        if not 0 <= start <= index:
            raise PreservationError("OpenCode compaction retained tail is missing or forward")
        retained = tuple(m.id for r in rows[start:index] for m in groups[r["info"]["id"]])
        summaries_canonical = native.linked([*groups[parent], *groups[summary["info"]["id"]]])
        checkpoints.append(
            Checkpoint(parts[0]["id"], summaries_canonical[-1].id, summaries_canonical, retained)
        )
    return tuple(checkpoints)


def _restore_groups(
    groups: dict[str, tuple[Message, ...]], metadata: dict[str, Any]
) -> dict[str, tuple[Message, ...]]:
    annotations = metadata.get("messages")
    if annotations is None:
        return groups
    if not isinstance(annotations, dict) or not set(annotations) <= set(groups):
        raise PreservationError(
            "OpenCode structural message annotations disagree with native graph"
        )
    restored = {}
    floor: datetime | None = None
    for key, native_messages in groups.items():
        if key in annotations:
            descriptors = annotations[key]
            messages = _regrouped(native_messages, len(descriptors))
            if len(descriptors) != len(messages):
                raise PreservationError(
                    "OpenCode structural result grouping disagrees with native parts"
                )
            restored[key] = tuple(
                _restore_message(message, descriptor, floor if index == 0 else None)
                for index, (message, descriptor) in enumerate(
                    zip(messages, descriptors, strict=True)
                )
            )
        else:
            restored[key] = native_messages
        if native_messages:
            floor = native.historical_time(native_messages[0], datetime.now(UTC))
    return restored


def _regrouped(messages: tuple[Message, ...], groups: int) -> tuple[Message, ...]:
    if groups != COMBINED_GROUP or len(messages) <= COMBINED_GROUP:
        return messages
    results = messages[1:]
    return messages[0], replace(
        results[0],
        content=tuple(item for message in results for item in message.content),
        timestamp=max(
            (message.timestamp for message in results if message.timestamp is not None),
            key=lambda timestamp: int(timestamp.value),
            default=None,
        ),
    )


def _restore_message(message: Message, descriptor: Any, floor: datetime | None) -> Message:
    result = native.restore(message, {"ringleader": descriptor})
    if (
        result.provenance.source.agent == "opencode"
        and result.provenance.evidence.startswith(TOOL_TIME_EVIDENCE)
        and result.provenance.evidence != message.provenance.evidence
        and not (
            any(isinstance(item, ToolOmission) for item in result.content)
            and not any(isinstance(item, ToolCall | ToolResult) for item in result.content)
        )
    ):
        raise PreservationError("OpenCode original tool timestamps disagree with native state")
    if result.timestamp is not None:
        now = datetime.now(UTC)
        original = native.historical_time(result, now)
        actual = native.historical_time(message, now)
        if original != actual and not (floor is not None and original < actual == floor):
            raise PreservationError(
                "OpenCode historical timestamp annotation disagrees with native time"
            )
    return result


def parse(
    export: dict[str, Any], identity: Identity, files: dict[str, bytes] | None = None
) -> Conversation:
    info = object_value(export.get("info"), "session")
    if info.get("id") != identity.native_id:
        raise PreservationError("OpenCode session identity mismatch")
    if info.get("revert"):
        raise PreservationError(
            "OpenCode session revert requires explicit selected-history support"
        )
    if info.get("time", {}).get("compacting"):
        raise PreservationError("OpenCode session is currently compacting")
    rows = export.get("messages")
    if not isinstance(rows, list) or not rows:
        raise PreservationError("Empty or malformed OpenCode conversation")
    assets: list[Attachment] = []
    groups: dict[str, tuple[Message, ...]] = {}
    parts_seen: set[str] = set()
    user_ids: set[str] = set()
    for row in rows:
        message = object_value(row.get("info"), "message")
        key = native.string(message.get("id"), "message id")
        if key in groups or message.get("sessionID") != identity.native_id:
            raise PreservationError("OpenCode duplicate message or cross-session reference")
        if message.get("role") == "user":
            user_ids.add(key)
        elif message.get("parentID") not in user_ids:
            raise PreservationError("OpenCode assistant parentID must reference a preceding user")
        for part in row["parts"]:
            if (
                part.get("sessionID") != identity.native_id
                or part.get("messageID") != key
                or part["id"] in parts_seen
            ):
                raise PreservationError("OpenCode invalid part ownership or duplicate ID")
            parts_seen.add(part["id"])
        groups[key] = _message(row, identity, assets, files or {})
    metadata = object_value(info.get("metadata") or {}, "session metadata").get("ringleader", {})
    groups = _restore_groups(groups, metadata)
    history = native.linked([message for group in groups.values() for message in group])
    if not history:
        raise PreservationError("OpenCode conversation has no portable messages")
    checkpoints = _checkpoint(rows, groups)
    context = history
    if checkpoints:
        last = checkpoints[-1]
        positions = {m.id: i for i, m in enumerate(history)}
        context = native.linked(
            [
                *last.summary,
                *(history[positions[key]] for key in last.retained_message_ids),
                *history[positions[last.after_message_id] + 1 :],
            ]
        )
    provenance = Provenance("native", identity, "session")
    conversation = Conversation(
        identity,
        history[-1].id,
        history,
        context,
        checkpoints,
        tuple(assets),
        info.get("title"),
        source_created_at=native.timestamp(info["time"]["created"], provenance),
        source_updated_at=native.timestamp(info["time"]["updated"], provenance),
    )
    conversation = native.restore_session(conversation, metadata.get("session"))
    if metadata.get("messages") is not None and set(groups) - set(metadata["messages"]):
        conversation = replace(conversation, branch_id=history[-1].id)
    validate(conversation)
    return conversation


def _attachment_paths(export: dict[str, Any]) -> set[Path]:
    paths: set[Path] = set()
    for row in export["messages"]:
        for part in row["parts"]:
            if part.get("type") != "file" or not str(part.get("url", "")).startswith("file:"):
                continue
            parsed = urlsplit(part["url"])
            if parsed.netloc not in ("", "localhost"):
                raise PreservationError("Remote OpenCode attachment path")
            paths.add(Path(unquote(parsed.path)).resolve())
    return paths


def _read_graph(connection: Any, identity: Identity) -> tuple[dict[str, Any], dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    key = identity.native_id
    row = connection.execute("SELECT * FROM session WHERE id=?", (key,)).fetchone()
    if row is None:
        raise PreservationError("OpenCode session not found")
    session = dict(row)
    for table in ("session_message", "session_input", "session_context_epoch"):
        if connection.execute(
            f"SELECT 1 FROM {table} WHERE session_id=? LIMIT 1", (key,)
        ).fetchone():
            raise PreservationError(
                f"OpenCode active V2 {table} data cannot be read as legacy history"
            )
    project = connection.execute(
        "SELECT * FROM project WHERE id=?", (session["project_id"],)
    ).fetchone()
    if project is None:
        raise PreservationError("OpenCode session project reference is missing")
    messages = [
        dict(r)
        for r in connection.execute(
            "SELECT * FROM message WHERE session_id=? ORDER BY time_created,id", (key,)
        )
    ]
    parts = [
        dict(r)
        for r in connection.execute(
            "SELECT * FROM part WHERE session_id=? OR message_id IN (SELECT id FROM message WHERE session_id=?) ORDER BY id",
            (key, key),
        )
    ]
    by_message: dict[str, list[dict[str, Any]]] = {r["id"]: [] for r in messages}
    for part in parts:
        if part["session_id"] != key or part["message_id"] not in by_message:
            raise PreservationError("OpenCode native graph has conflicting part ownership")
        by_message[part["message_id"]].append(
            {
                **state.decode(part["data"]),
                "id": part["id"],
                "sessionID": key,
                "messageID": part["message_id"],
            }
        )
    info = {
        "id": key,
        "projectID": session["project_id"],
        "directory": session["directory"],
        "title": session["title"],
        "version": session["version"],
        "metadata": state.decode(session["metadata"] or "{}"),
        "revert": state.decode(session["revert"] or "null"),
        "time": {
            "created": session["time_created"],
            "updated": session["time_updated"],
            "compacting": session["time_compacting"],
        },
    }
    export = {
        "info": info,
        "messages": [
            {
                "info": {**state.decode(r["data"]), "id": r["id"], "sessionID": key},
                "parts": by_message[r["id"]],
            }
            for r in messages
        ],
    }
    return export, {
        "session": session,
        "project": dict(project),
        "messages": messages,
        "parts": parts,
    }


class OpenCodeAdapter:
    profile = PROFILE

    def __init__(
        self, executable: str = "opencode", verified: tuple[VerificationRecord, ...] = ()
    ) -> None:
        self.executable = executable
        self.local_schemas = local_schemas(verified, "opencode")
        self.schemas = state.ACCEPTED | frozenset(self.local_schemas.values())

    def target_model(self, roots: NativeRoots) -> tuple[str, str]:
        if any(
            os.environ.get(key)
            for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG_DIR")
        ):
            raise PreservationError(
                "OpenCode custom config overrides are unsupported by the explicit-model profile"
            )
        project = tuple(
            (str(path), path)
            for directory in (roots.cwd, *roots.cwd.parents)
            for path in (
                directory / name
                for name in (
                    "opencode.json",
                    "opencode.jsonc",
                    ".opencode/opencode.json",
                    ".opencode/opencode.jsonc",
                )
            )
            if path.exists()
        )
        for item in snapshot_files(project, max_bytes=1024 * 1024):
            config = object_value(state.decode(item.data), "project JSON config")
            if MODEL_KEYS & set(config):
                raise PreservationError(
                    "OpenCode project config model precedence is unverified by this target profile"
                )
        home_config = roots.home / ".opencode"
        if any((home_config / name).exists() for name in ("opencode.json", "opencode.jsonc")):
            raise PreservationError("OpenCode home .opencode config precedence is unverified")
        directories = {
            roots.config / "opencode",
            home_config,
            *(directory / ".opencode" for directory in (roots.cwd, *roots.cwd.parents)),
        }
        if any(
            (directory / name).exists()
            for directory in directories
            for name in ("agent", "agents", "mode", "modes")
        ):
            raise PreservationError(
                "OpenCode discovered agent/mode definitions can override the target model"
            )
        model = None
        paths = tuple(
            (name, roots.config / "opencode" / name)
            for name in ("config.json", "opencode.json", "opencode.jsonc")
            if (roots.config / "opencode" / name).exists()
        )
        for item in snapshot_files(paths, max_bytes=1024 * 1024):
            config = object_value(state.decode(item.data), "target JSON config")
            if "default_agent" in config or any(
                RESUMED_AGENT in object_value(config.get(key) or {}, f"target {key} config")
                for key in ("agent", "mode")
            ):
                raise PreservationError(
                    "OpenCode build agent/mode or default_agent configuration can override the resumed model"
                )
            model = config.get("model", model)
        if not isinstance(model, str) or "/" not in model or "{" in model:
            raise PreservationError(
                "OpenCode target needs an explicit provider/model in native JSON config; automatic default-model discovery is unverified"
            )
        provider, model_id = model.split("/", 1)
        if not provider or not model_id:
            raise PreservationError("OpenCode target model requires provider/model")
        return provider, model_id

    def allocate_identity(self) -> Identity:
        return Identity("opencode", IDs().new("ses"))

    def inspect_source(self, request: SourceRequest) -> Compatibility:
        try:
            if request.identity.agent != "opencode":
                raise PreservationError("Not an OpenCode source")
            path = native.source_path(request.source_ref, request.roots)
            with state.database_copy(path) as isolated, database_snapshot(isolated) as connection:
                schema = state.schema_identity(connection, self.schemas)
            return Compatibility(True, PROFILE, (), schema_identity=schema)
        except (OSError, PreservationError) as exc:
            return Compatibility(False, None, (str(exc),))

    def snapshot(self, request: SourceRequest) -> SourceSnapshot:
        if request.identity.agent != "opencode" or request.branch_id is not None:
            raise PreservationError("OpenCode source requires its complete selected native session")
        path = native.source_path(request.source_ref, request.roots)
        with state.database_copy(path) as isolated, database_snapshot(isolated) as connection:
            schema = state.schema_identity(connection, self.schemas)
            export, graph = _read_graph(connection, request.identity)
            attachments = snapshot_files(
                tuple((str(p), p) for p in sorted(_attachment_paths(export))),
                max_bytes=native.MAX_BYTES,
            )
            data = {item.name: item.data for item in attachments}
            conversation = parse(export, request.identity, data)
            graph_bytes = state.encode(graph)
            if len(graph_bytes) + sum(len(item.data) for item in attachments) > native.MAX_BYTES:
                raise PreservationError("OpenCode source graph exceeds snapshot bound")
        return SourceSnapshot(
            conversation,
            (SnapshotFile("graph.json", graph_bytes), *attachments),
            PROFILE.name,
            (("schema", schema),),
        )

    def inspect_target(self, roots: NativeRoots, runner: Runner) -> Compatibility:
        try:
            schema = state.inspect_database(roots, None)
        except (OSError, PreservationError) as exc:
            return Compatibility(False, None, (str(exc),))
        if schema not in self.schemas:
            if self.local_schemas:
                probed = native.probe_version(self.executable, "opencode", runner)
                version = (
                    probed.stdout.decode(errors="replace").strip() if probed.returncode == 0 else ""
                )
                if version in self.local_schemas:
                    return Compatibility(
                        False, None, (f"OpenCode writer schema {schema} is not supported",)
                    )
            return Compatibility(
                False,
                None,
                (
                    (
                        f"OpenCode writer schema {schema} is not verified; it needs "
                        f"self-verification{native.verification_hint('opencode')}"
                    ),
                ),
                needs_verification=True,
            )
        try:
            state.resolve_project(roots.cwd)
            provider, model = self.target_model(roots)
        except (OSError, PreservationError) as exc:
            return Compatibility(False, None, (str(exc),))
        verified_locally = schema in frozenset(self.local_schemas.values()) - state.ACCEPTED
        return Compatibility(
            True,
            PROFILE,
            ()
            if verified_locally
            else ("Source/schema-backed profile; native runtime smoke unverified",),
            schema_identity=schema,
            target_configuration=(("providerID", provider), ("modelID", model)),
            warnings=(
                (f"OpenCode writer schema {schema} accepted: self-verified on this machine",)
                if verified_locally
                else ()
            ),
        )

    def normalizations(self, profile: FormatProfile) -> tuple[Normalization, ...]:
        return (
            Normalization(
                "json-argument-whitespace",
                profile.name,
                "OpenCode native tool input is a JSON object",
            ),
            Normalization(
                "model-view-history",
                profile.name,
                "OpenCode keeps one stream: the model context, with hidden messages as ordinary parts",
            ),
            Normalization(
                "call-ordered-results",
                profile.name,
                "OpenCode stores each result inside its call's part, so one step's results "
                "replay in call order",
            ),
            Normalization(
                "joined-tool-output",
                profile.name,
                "OpenCode tool output is one string: text blocks joined by a blank line with "
                "media after it, as its own MCP tool results are stored",
            ),
        )

    def stage(self, conversation: Conversation, request: TargetRequest) -> NativeStage:
        return _stage(conversation, request, self)

    def stage_metadata(self, stage: NativeStage) -> NativeMetadata:
        session = state.decode(stage.database_payload or b"null")["session"]
        return NativeMetadata(
            Identity("opencode", session["id"]), session["directory"], session["title"]
        )

    def read_stage(self, stage: NativeStage) -> Conversation:
        if (
            stage.database_payload is None
            or stage.schema_identity not in self.schemas | state.LEGACY
        ):
            raise PreservationError("Missing OpenCode staged graph")
        return parse(_payload_export(state.decode(stage.database_payload)), stage.identity)

    def registration_status(
        self, stage: NativeStage
    ) -> Literal["absent", "owned", "archived", "conflict"]:
        return state.registration_status(stage, self.schemas)

    def register_stage(self, stage: NativeStage, *, recover: bool = False) -> bool:
        self.read_stage(stage)
        return state.registration(stage, recover=recover, accepted=self.schemas)

    def launch_request(self, identity: Identity, roots: NativeRoots) -> RunRequest:
        target_id(identity)
        if roots.store.name != "opencode":
            raise PreservationError(
                "OpenCode native store must be named opencode under XDG_DATA_HOME"
            )
        environment = {
            "HOME": str(roots.home),
            "XDG_DATA_HOME": str(roots.store.parent),
            "XDG_CONFIG_HOME": str(roots.config),
            "XDG_CACHE_HOME": str(roots.cache),
            "XDG_STATE_HOME": str(roots.state or roots.home / "state"),
            "TMPDIR": str(roots.temporary or roots.cache / "tmp"),
            "OPENCODE_DB": str(state.database_path(roots)),
        }
        return RunRequest(
            (self.executable, "--session", identity.native_id),
            roots.cwd,
            native.launch_env(environment, os.environ),
        )


def _emit_file(asset: Attachment, ids: IDs, session_id: str, message_id: str) -> dict[str, Any]:
    if asset.media_type not in MEDIA:
        raise PreservationError(f"OpenCode cannot preserve attachment MIME {asset.media_type}")
    part: dict[str, Any] = {
        "id": ids.new("prt"),
        "sessionID": session_id,
        "messageID": message_id,
        "type": "file",
        "mime": asset.media_type,
        "url": f"data:{asset.media_type};base64," + base64.b64encode(asset.data).decode(),
    }
    if asset.name is not None:
        part["filename"] = asset.name
    return part


def _compactions(conversation: Conversation) -> dict[str, Checkpoint]:
    by_id = {m.id: i for i, m in enumerate(conversation.history)}
    result = {}
    for checkpoint in conversation.checkpoints:
        if checkpoint.following_context is not None or checkpoint.retained_context is not None:
            raise CheckpointShape(
                "OpenCode cannot preserve independent checkpoint retained/following context"
            )
        summary = checkpoint.summary
        if (
            len(summary) != SUMMARY_MESSAGES
            or summary[0].role != "user"
            or summary[0].content != (Text(QUESTION),)
            or summary[1].role != "assistant"
            or summary[0].id not in by_id
            or summary[1].id not in by_id
        ):
            raise CheckpointShape(
                "OpenCode compaction requires the native question and assistant summary in visible history"
            )
        start = by_id[summary[0].id]
        end = by_id[summary[1].id]
        if end != start + 1 or checkpoint.after_message_id != summary[1].id:
            raise CheckpointShape("OpenCode compaction summary must be adjacent to native trigger")
        if checkpoint.retained_message_ids:
            ids = checkpoint.retained_message_ids
            expected = tuple(m.id for m in conversation.history[by_id[ids[0]] : start])
            if ids != expected:
                raise CheckpointShape(
                    "OpenCode retained compaction tail must be a contiguous preceding suffix"
                )
        result[summary[0].id] = checkpoint
    return result


def _emit_tool(
    call: ToolCall,
    result: ToolResult,
    assets: dict[str, Attachment],
    ids: IDs,
    owner: dict[str, Any],
) -> dict[str, Any]:
    arguments = native.object_arguments(call, "OpenCode")
    joined = joined_tool_output(result)
    images = [item for item in joined[1:] if isinstance(item, AttachmentRef)]
    if len(images) != len(joined) - 1 or not isinstance(joined[0], Text):
        raise PreservationError("OpenCode tool result output must be text and attachments")
    text = [joined[0].text]
    if result.is_error and images:
        raise PreservationError("OpenCode error tool state cannot preserve attachments")
    timing = owner["time"]["created"]
    tool_state: dict[str, Any] = {
        "status": "error" if result.is_error else "completed",
        "input": arguments,
        "metadata": {},
        "time": {"start": timing, "end": timing},
    }
    if result.is_error:
        tool_state["error"] = text[0]
    else:
        tool_state.update(
            output=text[0],
            title=call.name,
            attachments=[
                _emit_file(assets[ref.attachment_id], ids, owner["sessionID"], owner["id"])
                for ref in images
            ],
        )
    return {
        "id": ids.new("prt"),
        "sessionID": owner["sessionID"],
        "messageID": owner["id"],
        "type": "tool",
        "callID": call.call_id,
        "tool": call.name,
        "state": tool_state,
    }


def _emit_messages(
    conversation: Conversation, request: TargetRequest, now: datetime, target_model: tuple[str, str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ids = IDs()
    native_ids = {m.id: ids.new("msg") for m in conversation.history}
    assets = {a.id: a for a in conversation.attachments}
    checkpoints = _compactions(conversation)
    summaries = {cp.summary[1].id for cp in checkpoints.values()}
    rows: list[dict[str, Any]] = []
    annotations: dict[str, Any] = {}
    last_user: str | None = None
    index = 0
    previous_time = -1
    while index < len(conversation.history):
        message = conversation.history[index]
        role = native.native_role(message)
        if role not in ("user", "assistant"):
            raise PreservationError(
                "OpenCode tool results must immediately follow their assistant calls"
            )
        gap_fallback = (
            datetime.fromtimestamp(previous_time / 1000, UTC) if previous_time >= 0 else now
        )
        created = max(
            int(native.historical_time(message, gap_fallback).timestamp() * 1000), previous_time
        )
        previous_time = created
        info: dict[str, Any] = {
            "id": native_ids[message.id],
            "sessionID": request.identity.native_id,
            "role": role,
            "time": {"created": created},
            "agent": RESUMED_AGENT,
        }
        if role == "user":
            last_user = info["id"]
            info["model"] = {
                "providerID": target_model[0],
                "modelID": target_model[1],
            }
        else:
            if last_user is None:
                raise PreservationError(
                    "OpenCode assistant history requires a preceding user parent"
                )
            info.update(
                parentID=last_user,
                modelID=conversation.source.agent,
                providerID="ringleader-history",
                mode=RESUMED_AGENT,
                path={"cwd": str(request.roots.cwd), "root": str(request.roots.cwd)},
                cost=0,
                tokens=ZERO_TOKENS,
                finish="stop",
            )
            info["time"]["completed"] = created
            if message.id in summaries:
                info["summary"] = True
        parts, descriptors, consumed = _emit_content(
            message, info, conversation.history[index + 1 :], assets, ids
        )
        if message.id in checkpoints:
            cp = checkpoints[message.id]
            parts = [
                {
                    "id": ids.new("prt"),
                    "messageID": info["id"],
                    "sessionID": info["sessionID"],
                    "type": "compaction",
                    "auto": False,
                }
            ]
            if cp.retained_message_ids:
                parts[0]["tail_start_id"] = native_ids[cp.retained_message_ids[0]]
        annotations[info["id"]] = descriptors
        rows.append({"info": info, "parts": parts})
        index += consumed + 1
    return rows, annotations


def _emit_content(
    message: Message,
    info: dict[str, Any],
    following: tuple[Message, ...],
    assets: dict[str, Attachment],
    ids: IDs,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    calls = [item for item in message.content if isinstance(item, ToolCall)]
    replies = _result_messages(following, len(calls)) if calls else ()
    order = {call.call_id: index for index, call in enumerate(calls)}
    ordered = sorted(
        replies,
        key=lambda reply: min(
            order.get(item.call_id, len(order))
            for item in reply.content
            if isinstance(item, ToolResult)
        ),
    )
    descriptors = [
        native.message_annotations(item, freeform_calls=True) for item in (message, *ordered)
    ]
    result_by_id = {
        item.call_id: (item, reply)
        for reply in ordered
        for item in reply.content
        if isinstance(item, ToolResult)
    }
    if list(result_by_id) != [call.call_id for call in calls]:
        raise PreservationError("OpenCode tool results must answer every call of one native step")
    parts = []
    seen_tool = False
    for item in message.content:
        if isinstance(item, ToolCall):
            result, reply = result_by_id[item.call_id]
            part = _emit_tool(item, result, assets, ids, info)
            part["state"]["time"]["end"] = int(
                native.historical_time(
                    reply, datetime.fromtimestamp(info["time"]["created"] / 1000, UTC)
                ).timestamp()
                * 1000
            )
            if (
                message.provenance.source.agent == "opencode"
                and message.provenance.evidence.startswith(TOOL_TIME_EVIDENCE)
            ):
                times = state.decode(message.provenance.evidence.removeprefix(TOOL_TIME_EVIDENCE))
                if item.call_id not in times:
                    raise PreservationError("Missing original OpenCode tool timestamps")
                original = dict(times[item.call_id])
                loaded = original.pop("loaded", None)
                part["state"]["time"] = original
                if loaded is not None:
                    part["state"]["metadata"]["loaded"] = loaded
            parts.append(part)
            seen_tool = True
            continue
        if seen_tool:
            raise PreservationError(
                "OpenCode prose after a tool in one assistant message requires a separate native step"
            )
        if isinstance(item, AttachmentRef):
            if info["role"] != "user":
                raise PreservationError(
                    "OpenCode assistant attachments are omitted from native model context"
                )
            parts.append(_emit_file(assets[item.attachment_id], ids, info["sessionID"], info["id"]))
        elif isinstance(item, Text | Instruction | ToolOmission):
            text = item.text if isinstance(item, Text) else native.note(item)
            if not text:
                raise PreservationError("OpenCode native context omits empty text")
            parts.append(
                {
                    "id": ids.new("prt"),
                    "messageID": info["id"],
                    "sessionID": info["sessionID"],
                    "type": "text",
                    "text": text,
                }
            )
        else:
            raise PreservationError("Unsupported OpenCode content placement")
    if not parts:
        raise PreservationError("OpenCode native context omits empty messages")
    return parts, descriptors, len(replies)


def _result_messages(following: tuple[Message, ...], calls: int) -> tuple[Message, ...]:
    replies: list[Message] = []
    answered = 0
    for reply in following:
        if answered >= calls or not reply.content:
            break
        if not all(isinstance(item, ToolResult) for item in reply.content):
            break
        replies.append(reply)
        answered += len(reply.content)
    if not replies:
        raise PreservationError(
            "OpenCode needs contiguous result messages after assistant tool calls"
        )
    if len(replies) not in (1, calls):
        raise PreservationError("OpenCode mixed result grouping is unsupported")
    return tuple(replies)


def _payload_export(payload: dict[str, Any]) -> dict[str, Any]:
    session = payload["session"]
    key = session["id"]
    info = {
        "id": key,
        "slug": session["slug"],
        "projectID": session["project_id"],
        "directory": session["directory"],
        "path": session["path"],
        "title": session["title"],
        "version": session["version"],
        "metadata": state.decode(session["metadata"]),
        "time": {"created": session["time_created"], "updated": session["time_updated"]},
    }
    by_message: dict[str, list[dict[str, Any]]] = {r["id"]: [] for r in payload["message"]}
    for part in sorted(payload["part"], key=lambda p: p["id"]):
        if part["session_id"] != key or part["message_id"] not in by_message:
            raise PreservationError("Staged OpenCode part ownership mismatch")
        by_message[part["message_id"]].append(
            {
                **state.decode(part["data"]),
                "id": part["id"],
                "messageID": part["message_id"],
                "sessionID": key,
            }
        )
    messages = []
    for row in sorted(payload["message"], key=lambda r: (r["time_created"], r["id"])):
        if row["session_id"] != key:
            raise PreservationError("Staged OpenCode message ownership mismatch")
        messages.append(
            {
                "info": {**state.decode(row["data"]), "id": row["id"], "sessionID": key},
                "parts": by_message[row["id"]],
            }
        )
    return {"info": info, "messages": messages}


def _stage(
    conversation: Conversation, request: TargetRequest, adapter: OpenCodeAdapter
) -> NativeStage:
    validate(conversation)
    native.reject_opaque(conversation)
    applied = adapter.normalizations(PROFILE)
    try:
        _compactions(conversation)
    except CheckpointShape:
        conversation = flattened(conversation)
        applied = (*applied, flattened_checkpoint(PROFILE.name))
    conversation = model_view(conversation, hides=False)
    target_id(request.identity)
    if request.profile != PROFILE:
        raise PreservationError("Unsupported OpenCode writer profile")
    schema = state.inspect_database(request.roots, adapter.schemas)
    now = datetime.now(UTC)
    created = int(now.timestamp() * 1000)
    project_id, worktree, vcs = state.resolve_project(request.roots.cwd)
    target_model = adapter.target_model(request.roots)
    configuration = (("providerID", target_model[0]), ("modelID", target_model[1]))
    if request.target_configuration and request.target_configuration != configuration:
        raise PreservationError("OpenCode target model configuration changed since inspection")
    messages, annotations = _emit_messages(conversation, request, now, target_model)
    metadata = {
        "ringleader": {
            "session": native.session_metadata(conversation),
            "messages": annotations,
            "registration": secrets.token_hex(32),
        }
    }
    session = {
        "id": request.identity.native_id,
        "project_id": project_id,
        "slug": "ringleader-" + request.identity.native_id,
        "directory": str(request.roots.cwd.resolve()),
        "path": str(request.roots.cwd.resolve().relative_to(Path(worktree)))
        if request.roots.cwd.resolve().is_relative_to(Path(worktree))
        else os.path.relpath(request.roots.cwd, worktree),
        "title": request.title,
        "version": PROFILE.format_version,
        "metadata": state.encode(metadata).decode(),
        "model": state.encode({"providerID": target_model[0], "id": target_model[1]}).decode(),
        "time_created": created,
        "time_updated": created,
    }
    payload: dict[str, Any] = {
        "session": session,
        "project_identity": [project_id, worktree, vcs],
        "project": {
            "id": project_id,
            "worktree": worktree,
            "vcs": vcs,
            "time_created": created,
            "time_updated": created,
            "sandboxes": "[]",
        },
        "message": [],
        "part": [],
    }
    for row in messages:
        info = row["info"]
        data = {k: v for k, v in info.items() if k not in ("id", "sessionID")}
        payload["message"].append(
            {
                "id": info["id"],
                "session_id": session["id"],
                "time_created": info["time"]["created"],
                "time_updated": created,
                "data": state.encode(data).decode(),
            }
        )
        for part in row["parts"]:
            data = {k: v for k, v in part.items() if k not in ("id", "messageID", "sessionID")}
            payload["part"].append(
                {
                    "id": part["id"],
                    "message_id": info["id"],
                    "session_id": session["id"],
                    "time_created": created,
                    "time_updated": created,
                    "data": state.encode(data).decode(),
                }
            )
    stage = NativeStage(
        request.identity,
        PROFILE,
        request.roots,
        (),
        "opencode.db",
        state.encode(payload),
        schema,
        applied,
    )
    actual = adapter.read_stage(stage)
    compare(replace(conversation, title=request.title), actual, applied)
    return stage
