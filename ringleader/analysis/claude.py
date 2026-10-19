from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from itertools import pairwise
from pathlib import Path
from typing import Any

from ..portability import claude_chain
from ..portability.model import PreservationError
from ..usage.base import as_dict
from ..usage.claude import claude_tokens, request_ids, session_of
from . import build, taxonomy
from .facts import (
    COMMAND_CHARS,
    FINAL_TEXT_CHARS,
    MAIN_THREAD,
    MESSAGE_CHARS,
    CallStatus,
    CompactionFact,
    CompactionTrigger,
    ErrorCategory,
    LastEvent,
    MessageOrigin,
    RequestFact,
    RewindFact,
    ToolCallFact,
    TranscriptFacts,
    TranscriptRef,
    UserMessageFact,
)

PREFILTER = '"uuid"'
GRAPH_TYPES = frozenset({"user", "assistant", "system", "progress", "attachment"})
ALLOWED_DECISIONS = frozenset({"accept", "allow"})
ORIGINS: frozenset[MessageOrigin] = frozenset({"human", "task-notification", "peer"})
TRIGGERS: frozenset[CompactionTrigger] = frozenset({"auto", "manual"})
PRESERVATION_KEYS = ("preservedMessages", "preservedSegment")
SUBAGENT_DEPTH = 4

type Row = dict[str, Any]


@dataclass(frozen=True)
class _Thread:
    session_id: str
    thread_id: str
    thread_key: str
    parent_thread_id: str | None = None
    parent_tool_use_id: str | None = None
    subagent_type: str | None = None
    subagent_description: str | None = None

    @property
    def main(self) -> bool:
        return self.thread_id == MAIN_THREAD


@dataclass(frozen=True)
class _Call:
    uuid: str | None
    line: int
    call_id: str
    timestamp: str
    tool: str
    command: str | None
    full_command: str | None
    file_path: str | None
    read_offset: int | None
    read_limit: int | None
    skill: str | None
    input_hash: str
    input_preview: str
    edit_old_hash: str | None
    edit_new_hash: str | None


@dataclass(frozen=True)
class _Result:
    is_error: bool | None
    text: str
    tool_use_result: str | None
    interrupted: bool
    denied: bool


@dataclass(frozen=True)
class _Outcome:
    status: CallStatus
    category: ErrorCategory | None = None
    error_text: str | None = None


@dataclass
class _Scan:
    main: bool
    key: str
    graph: list[Row] = field(default_factory=list)
    requests: list[tuple[str | None, RequestFact]] = field(default_factory=list)
    request_keys: set[str] = field(default_factory=set)
    calls: list[_Call] = field(default_factory=list)
    call_uuids: dict[str, str | None] = field(default_factory=dict)
    results: dict[str, _Result] = field(default_factory=dict)
    messages: list[tuple[str | None, UserMessageFact]] = field(default_factory=list)
    compactions: list[CompactionFact] = field(default_factory=list)
    texts: list[tuple[str | None, str]] = field(default_factory=list)
    events: list[tuple[str | None, LastEvent]] = field(default_factory=list)
    api_errors: int = 0
    cwd: str | None = None
    git_branch: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _meta(path: Path) -> dict[str, Any]:
    try:
        return as_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _thread(path: Path) -> _Thread:
    session_id = session_of(path, path.parents[min(SUBAGENT_DEPTH, len(path.parents) - 1)])
    if session_id == path.stem:
        return _Thread(session_id, MAIN_THREAD, session_id)
    meta = _meta(path.with_suffix(".meta.json"))
    parent_agent = _text(meta.get("parentAgentId"))
    depth = _int_or_none(meta.get("spawnDepth"))
    nested_orphan = parent_agent is None and depth is not None and depth > 1
    return _Thread(
        session_id,
        path.stem,
        f"{session_id}/{path.stem}",
        parent_thread_id=f"agent-{parent_agent}"
        if parent_agent
        else (None if nested_orphan else MAIN_THREAD),
        parent_tool_use_id=_text(meta.get("toolUseId")),
        subagent_type=_text(meta.get("agentType")),
        subagent_description=_text(meta.get("description")),
    )


def _records(path: Path) -> Iterator[tuple[int, Row]]:
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line, raw in enumerate(fh, 1):
            if PREFILTER not in raw:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield line, record


def _content(record: Row) -> object:
    return as_dict(record.get("message")).get("content")


def _blocks(record: Row, kind: str) -> list[Row]:
    content = _content(record)
    blocks = content if isinstance(content, list) else []
    return [b for b in blocks if isinstance(b, dict) and b.get("type") == kind]


def _user_text(record: Row) -> str:
    content = _content(record)
    if isinstance(content, str):
        return content
    return "\n".join(t for b in _blocks(record, "text") if (t := _text(b.get("text"))))


def _result_text(block: Row) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    items = content if isinstance(content, list) else []
    return "\n".join(
        t
        for b in items
        if isinstance(b, dict) and b.get("type") == "text" and (t := _text(b.get("text")))
    )


def _talks(record: Row) -> bool:
    kind = record.get("type")
    if kind == "assistant":
        return True
    return kind == "user" and not record.get("isMeta") and bool(_user_text(record).strip())


def _slim(record: Row, line: int) -> Row:
    metadata = as_dict(record.get("compactMetadata"))
    slim: Row = {
        key: record.get(key)
        for key in ("uuid", "parentUuid", "logicalParentUuid", "type", "subtype", "isMeta")
    }
    slim["compactMetadata"] = {k: metadata[k] for k in PRESERVATION_KEYS if k in metadata}
    slim["_line"] = line
    slim["_ts"] = str(record.get("timestamp") or "")
    slim["_talk"] = _talks(record)
    slim["_calls"] = len(_blocks(record, "tool_use")) if record.get("type") == "assistant" else 0
    slim["_mid"] = claude_chain.message_id(record) if record.get("type") == "assistant" else None
    slim["_cids"] = claude_chain.call_ids(record)
    slim["_rids"] = claude_chain.result_ids(record)
    return slim


def _hash_or_none(value: object) -> str | None:
    return taxonomy.short_hash(value) if isinstance(value, str) else None


def _file_path(tool: str, raw: Row) -> str | None:
    named = _text(raw.get("file_path")) or _text(raw.get("notebook_path"))
    if named or tool not in taxonomy.PATCH_TOOLS:
        return named
    return taxonomy.patch_path(_text(raw.get("input")))


def _call(block: Row, uuid: str | None, line: int, timestamp: str, key: str) -> _Call:
    tool = str(block.get("name") or "")
    raw = as_dict(block.get("input"))
    command = _text(raw.get("command"))
    capped = command[:COMMAND_CHARS] if command else None
    shown = capped or json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    reads = tool in taxonomy.READ_TOOLS
    return _Call(
        uuid=uuid,
        line=line,
        call_id=str(block.get("id") or f"{key}:line:{line}"),
        timestamp=timestamp,
        tool=tool,
        command=capped,
        full_command=command,
        file_path=_file_path(tool, raw),
        read_offset=_int_or_none(raw.get("offset")) if reads else None,
        read_limit=_int_or_none(raw.get("limit")) if reads else None,
        skill=_text(raw.get("skill")) if tool in taxonomy.SKILL_TOOLS else None,
        input_hash=taxonomy.input_hash(raw),
        input_preview=taxonomy.preview(shown, taxonomy.INPUT_PREVIEW_CHARS),
        edit_old_hash=_hash_or_none(raw.get("old_string")),
        edit_new_hash=_hash_or_none(raw.get("new_string")),
    )


def _request(
    record: Row, line: int, timestamp: str, previous: RequestFact | None, key: str
) -> RequestFact | None:
    message = as_dict(record.get("message"))
    usage = as_dict(message.get("usage"))
    if not usage:
        return None
    message_id, request_id = request_ids(record)
    return build.request_fact(
        line=line,
        dedupe_key=f"{message_id or ''}|{request_id or ''}"
        if message_id or request_id
        else f"{key}:line:{line}",
        timestamp=timestamp,
        model=_text(message.get("model")),
        tokens=claude_tokens(usage),
        context_window=None,
        previous=previous,
        skill=_text(record.get("attributionSkill")),
        plugin=_text(record.get("attributionPlugin")),
    )


def _assistant(scan: _Scan, record: Row, line: int, timestamp: str) -> None:
    uuid = _text(record.get("uuid"))
    message = as_dict(record.get("message"))
    if record.get("isApiErrorMessage") is True:
        scan.api_errors += 1
        scan.events.append((uuid, "api_error"))
        return
    if message.get("model") == "<synthetic>":
        return
    previous = scan.requests[-1][1] if scan.requests else None
    request = _request(record, line, timestamp, previous, scan.key)
    if request is not None and request.dedupe_key not in scan.request_keys:
        scan.request_keys.add(request.dedupe_key)
        scan.requests.append((uuid, request))
    for block in _blocks(record, "text"):
        text = _text(block.get("text"))
        if text and text.strip():
            scan.texts.append((uuid, text[-FINAL_TEXT_CHARS:]))
            scan.events.append((uuid, "assistant_text"))
    for block in _blocks(record, "tool_use"):
        call = _call(block, uuid, line, timestamp, scan.key)
        scan.calls.append(call)
        scan.call_uuids.setdefault(call.call_id, uuid)
        scan.events.append((uuid, "tool_pending"))


def _result(record: Row, block: Row) -> _Result:
    used = record.get("toolUseResult")
    decision = as_dict(record.get("permissionDecision")).get("decision")
    is_error = block.get("is_error")
    return _Result(
        is_error=is_error if isinstance(is_error, bool) else None,
        text=taxonomy.cap_error(_result_text(block)),
        tool_use_result=taxonomy.cap_error(used) if isinstance(used, str) and used else None,
        interrupted=as_dict(used).get("interrupted") is True,
        denied=isinstance(decision, str) and decision not in ALLOWED_DECISIONS,
    )


def _origin(scan: _Scan, record: Row) -> MessageOrigin:
    if not scan.main:
        return "agent"
    origin = record.get("origin")
    if origin is None:
        return "human"
    kind = as_dict(origin).get("kind")
    return next((o for o in ORIGINS if o == kind), "unknown")


def _message(scan: _Scan, record: Row, line: int, timestamp: str, text: str) -> None:
    uuid = _text(record.get("uuid")) or f"{scan.key}:line:{line}"
    capped = text[:MESSAGE_CHARS]
    interrupt = taxonomy.starts_with_marker(capped, taxonomy.INTERRUPT_MARKERS)
    found = taxonomy.COMMAND_NAME_PATTERN.search(capped)
    fact = build.message_fact(
        before_call=len(scan.calls),
        line=line,
        message_id=uuid,
        dedupe_key=uuid,
        timestamp=timestamp,
        origin=_origin(scan, record),
        text=capped,
        is_interrupt=interrupt,
        slash_command=found.group(1) if found else None,
    )
    scan.messages.append((_text(record.get("uuid")), fact))
    scan.events.append((_text(record.get("uuid")), "interrupt" if interrupt else "user"))


def _user(scan: _Scan, record: Row, line: int, timestamp: str) -> None:
    if record.get("isCompactSummary") or record.get("isMeta"):
        return
    for block in _blocks(record, "tool_result"):
        call_id = _text(block.get("tool_use_id"))
        if call_id is None:
            continue
        scan.results.setdefault(call_id, _result(record, block))
        scan.events.append((scan.call_uuids.get(call_id, _text(record.get("uuid"))), "tool_result"))
    text = _user_text(record)
    if text.strip() and not text.lstrip().startswith(taxonomy.COMMAND_OUTPUT_MARKERS):
        _message(scan, record, line, timestamp, text)


def _system(scan: _Scan, record: Row, line: int, timestamp: str) -> None:
    subtype = record.get("subtype")
    uuid = _text(record.get("uuid"))
    if subtype == "compact_boundary":
        metadata = as_dict(record.get("compactMetadata"))
        trigger = metadata.get("trigger")
        scan.compactions.append(
            CompactionFact(
                before_call=len(scan.calls),
                line=line,
                timestamp=timestamp,
                trigger=next((t for t in TRIGGERS if t == trigger), "inferred"),
                pre_tokens=_int_or_none(metadata.get("preTokens")),
                post_tokens=_int_or_none(metadata.get("postTokens")),
            )
        )
    elif subtype == "api_error":
        scan.api_errors += 1
        scan.events.append((uuid, "api_error"))
    elif subtype == "turn_duration":
        scan.events.append((uuid, "turn_complete"))


def _track(scan: _Scan, record: Row, line: int) -> str:
    timestamp = str(record.get("timestamp") or "")
    scan.cwd = _text(record.get("cwd")) or scan.cwd
    scan.git_branch = _text(record.get("gitBranch")) or scan.git_branch
    if timestamp:
        scan.first_ts = scan.first_ts or timestamp
        scan.last_ts = timestamp
    kind = record.get("type")
    in_graph = kind in GRAPH_TYPES and _text(record.get("uuid"))
    if in_graph and (scan.main is False or record.get("isSidechain") is not True):
        scan.graph.append(_slim(record, line))
    return timestamp


def _scan(path: Path, main: bool, key: str) -> _Scan:
    scan = _Scan(main=main, key=key)
    handlers = {"assistant": _assistant, "user": _user, "system": _system}
    for line, record in _records(path):
        timestamp = _track(scan, record, line)
        handler = handlers.get(str(record.get("type")))
        if handler is not None:
            handler(scan, record, line, timestamp)
    return scan


def _subtree(start: str, children: dict[str, list[str]]) -> list[str]:
    found: list[str] = []
    stack = [start]
    while stack:
        key = stack.pop()
        found.append(key)
        stack.extend(children.get(key, []))
    return found


@dataclass(frozen=True)
class _Branches:
    off: frozenset[str] = frozenset()
    rewinds: tuple[tuple[Row, Row, int], ...] = ()


def _live_path(graph: list[Row]) -> tuple[claude_chain.Entries, list[Row], dict[str, str | None]]:
    for row in graph:
        if claude_chain.is_boundary(row):
            claude_chain.validate_metadata(row["compactMetadata"])
    entries = claude_chain.index(graph)
    path = claude_chain.walk(entries, claude_chain.default_leaf(entries), logical=True)
    parents = {key: claude_chain.parent(entries, row) for key, row in entries.rows.items()}
    return entries, path, parents


def _parallel_live(entries: claude_chain.Entries, live: set[str]) -> set[str]:
    rows = entries.rows.values()
    messages = {row["_mid"] for row in rows if row["uuid"] in live and row["_mid"]}
    live = live | {row["uuid"] for row in rows if row["_mid"] in messages}
    calls = {call for row in rows if row["uuid"] in live for call in row["_cids"]}
    return live | {row["uuid"] for row in rows if calls.intersection(row["_rids"])}


def _before_boundary(entries: claude_chain.Entries, path: list[Row]) -> set[str]:
    first = path[0]
    if not claude_chain.is_boundary(first):
        return set()
    cutoff = entries.position(first)
    return {key for key, position in entries.positions.items() if position < cutoff}


def _branches(graph: list[Row]) -> _Branches:
    try:
        entries, path, parents = _live_path(graph)
    except (PreservationError, KeyError, TypeError):
        return _Branches()
    children: dict[str, list[str]] = {}
    for key, parent in parents.items():
        if parent is not None:
            children.setdefault(parent, []).append(key)
    chosen = {row["uuid"] for row in path}
    compacted = {
        key
        for point, child in pairwise(path)
        if claude_chain.is_boundary(child)
        for start in children.get(point["uuid"], [])
        if start not in chosen
        for key in _subtree(start, children)
    }
    live = _parallel_live(entries, chosen) | _before_boundary(entries, path) | compacted
    rewinds = []
    for point, child in pairwise(path):
        abandoned = [
            entries.rows[key]
            for start in children.get(point["uuid"], [])
            if start not in live
            for key in _subtree(start, children)
            if key not in live
        ]
        if any(row["_talk"] for row in abandoned):
            rewinds.append((point, child, sum(row["_calls"] for row in abandoned)))
    return _Branches(frozenset(entries.rows) - live, tuple(rewinds))


def _outcome(command: str | None, result: _Result | None) -> _Outcome:
    if result is None:
        return _Outcome("unknown")
    forced: ErrorCategory | None = "interruption" if result.interrupted else None
    forced = forced or ("permission_denied" if result.denied else None)
    marked: tuple[tuple[tuple[str, ...], ErrorCategory], ...] = (
        (taxonomy.INTERRUPT_MARKERS, "interruption"),
        (taxonomy.REJECTION_MARKERS, "user_rejected"),
    )
    marker = next(
        (
            category
            for markers, category in marked
            if taxonomy.starts_with_marker(result.text, markers)
        ),
        None,
    )
    error_text = result.tool_use_result or result.text
    if result.is_error is True:
        code = taxonomy.exit_code(error_text)
        if forced is None and taxonomy.benign_exit(command, code, result.text):
            return _Outcome("ok")
        return _Outcome("error", forced or taxonomy.categorize_error(error_text), error_text)
    flagged = forced or marker
    if flagged is not None:
        return _Outcome("error", flagged, error_text)
    if result.is_error is None and result.text.lstrip().startswith(taxonomy.TOOL_USE_ERROR_MARKER):
        category = taxonomy.categorize_error(error_text, generic=False)
        return _Outcome("error", category, error_text)
    return _Outcome("ok")


def _call_fact(seq: int, call: _Call, result: _Result | None, live: bool) -> ToolCallFact:
    outcome = _outcome(call.full_command, result)
    tail = build.call_tail(
        call.tool, call.command, outcome.status, outcome.category, outcome.error_text
    )
    return ToolCallFact(
        seq=seq,
        line=call.line,
        call_id=call.call_id,
        dedupe_key=call.call_id,
        timestamp=call.timestamp,
        tool=call.tool,
        kind=taxonomy.tool_kind(call.tool, call.command),
        mcp_server=tail.mcp_server,
        mcp_tool=tail.mcp_tool,
        skill=call.skill,
        command=call.command,
        command_class=tail.command_class,
        file_path=call.file_path,
        input_hash=call.input_hash,
        input_preview=call.input_preview,
        edit_old_hash=call.edit_old_hash,
        edit_new_hash=call.edit_new_hash,
        is_error=result.is_error if result else None,
        status=outcome.status,
        error_category=outcome.category,
        error_text=outcome.error_text,
        signature=tail.signature,
        on_main_branch=live,
        read_offset=call.read_offset,
        read_limit=call.read_limit,
    )


def _rewind(point: Row, child: Row, abandoned: int, calls: tuple[ToolCallFact, ...]) -> RewindFact:
    line = int(child["_line"])
    return RewindFact(
        line=line,
        timestamp=str(child["_ts"]),
        branch_uuid=str(point["uuid"]),
        before_call=next((c.seq for c in calls if c.on_main_branch and c.line >= line), len(calls)),
        abandoned_calls=abandoned,
    )


def _last_live[T](items: list[tuple[str | None, T]], off: frozenset[str]) -> T | None:
    return next((value for uuid, value in reversed(items) if uuid not in off), None)


def extract_claude(ref: TranscriptRef) -> TranscriptFacts:
    thread = _thread(ref.path)
    scan = _scan(ref.path, thread.main, thread.thread_key)
    branches = _branches(scan.graph)
    off = branches.off
    calls = tuple(
        _call_fact(seq, call, scan.results.get(call.call_id), call.uuid not in off)
        for seq, call in enumerate(scan.calls)
    )
    return TranscriptFacts(
        ref=ref,
        session_id=thread.session_id,
        thread_id=thread.thread_id,
        thread_key=thread.thread_key,
        parent_thread_id=thread.parent_thread_id,
        parent_tool_use_id=thread.parent_tool_use_id,
        subagent_type=thread.subagent_type,
        subagent_description=thread.subagent_description,
        cwd=scan.cwd,
        git_branch=scan.git_branch,
        first_ts=scan.first_ts,
        last_ts=scan.last_ts,
        final_text=_last_live(scan.texts, off),
        last_event=_last_live(scan.events, off) or "none",
        api_errors=scan.api_errors,
        requests=tuple(replace(r, on_main_branch=u not in off) for u, r in scan.requests),
        tool_calls=calls,
        user_messages=tuple(replace(m, on_main_branch=u not in off) for u, m in scan.messages),
        compactions=tuple(scan.compactions),
        rewinds=tuple(_rewind(p, c, n, calls) for p, c, n in branches.rewinds)
        if thread.main
        else (),
    )
