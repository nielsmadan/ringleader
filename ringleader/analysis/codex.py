from __future__ import annotations

import json
from bisect import bisect_left
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from ..sources.codex import is_preamble
from ..usage.base import as_dict
from ..usage.codex import StartCache, codex_tokens, own_thread, rollout_session_id, turn_key
from . import build, taxonomy
from .facts import (
    COMMAND_CHARS,
    ERROR_HEAD_CHARS,
    ERROR_TAIL_CHARS,
    FINAL_TEXT_CHARS,
    MAIN_THREAD,
    CallStatus,
    CompactionFact,
    ErrorCategory,
    LastEvent,
    MessageOrigin,
    RequestFact,
    ToolCallFact,
    ToolKind,
    TranscriptFacts,
    TranscriptRef,
    UserMessageFact,
)

PREFILTER: tuple[str, ...] = (
    '"session_meta"',
    '"turn_context"',
    '"token_count"',
    '"item_completed"',
    '"task_complete"',
    '"turn_aborted"',
    '"compacted"',
    '"function_call"',
    '"custom_tool_call"',
    '"function_call_output"',
    '"custom_tool_call_output"',
    '"user"',
)
CALL_TYPES = frozenset({"function_call", "custom_tool_call"})
OUTPUT_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})
TOOL_ITEMS = frozenset(
    {
        "CommandExecution",
        "FileChange",
        "McpToolCall",
        "CollabAgentToolCall",
        "SubAgentActivity",
        "WebSearch",
        "Extension",
        "ImageView",
    }
)
NOTICE_ACTIVITIES = frozenset({"completed"})
ITEM_TOOLS: dict[str, str] = {
    "WebSearch": "web_search",
    "ImageView": "view_image",
    "web.search": "web_search",
    "clock.sleep": "sleep",
}
TEXT_PARTS = frozenset({"text", "input_text"})
INTERRUPT_REASON = "interrupt"
INTERRUPT_TEXT = taxonomy.INTERRUPT_MARKERS[0]
TAIL_CHARS = ERROR_HEAD_CHARS + ERROR_TAIL_CHARS
OUTPUT_KEYS = ("aggregated_output", "stdout", "stderr")
BULKY_KEYS = frozenset({*OUTPUT_KEYS, "formatted_output", "result", "results", "error"})

type Row = dict[str, Any]


@dataclass(frozen=True)
class _Identity:
    own_id: str
    session_id: str
    thread_id: str
    parent_thread_id: str | None
    subagent_type: str | None
    subagent_description: str | None
    cwd: str | None
    git_branch: str | None
    context_window: int | None


@dataclass(frozen=True)
class _Outcome:
    status: CallStatus
    is_error: bool | None = None
    category: ErrorCategory | None = None
    error_text: str | None = None


@dataclass(frozen=True)
class _Draft:
    line: int
    order: int
    call_id: str
    timestamp: str
    tool: str
    kind: ToolKind
    command: str | None
    file_path: str | None
    input_hash: str
    preview: str
    outcome: _Outcome


@dataclass(frozen=True)
class _Output:
    body: str
    code: int | None


@dataclass
class _FnCall:
    line: int
    timestamp: str
    name: str
    payload: Row
    children: int = 0
    output: _Output | None = None


@dataclass
class _Scan:
    own_id: str
    metas: list[Row] = field(default_factory=list)
    raw_model: object = None
    turn_cwd: str | None = None
    requests: list[RequestFact] = field(default_factory=list)
    previous_turn: tuple[Any, ...] | None = None
    items: list[tuple[int, str, Row]] = field(default_factory=list)
    calls: dict[str, _FnCall] = field(default_factory=dict)
    open_scripts: list[_FnCall] = field(default_factory=list)
    messages: list[tuple[int, str, str, str | None, bool]] = field(default_factory=list)
    compactions: list[tuple[int, str, int | None]] = field(default_factory=list)
    compaction_pending: Literal["record", "item"] | None = None
    fallback_messages: list[tuple[int, str, str, str | None, bool]] = field(default_factory=list)
    saw_user_item: bool = False
    final_text: str | None = None
    last_event: LastEvent = "none"
    first_ts: str | None = None
    last_ts: str | None = None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _lines(path: Path) -> Iterator[tuple[int, str]]:
    with path.open(encoding="utf-8", errors="replace") as fh:
        yield from enumerate(fh)


def _parse(raw: str) -> Row | None:
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _replay_rows(path: Path) -> Iterator[tuple[int, Row]]:
    for index, raw in _lines(path):
        record = _parse(raw)
        if record is not None:
            yield index, record


def start_cache() -> StartCache:
    return StartCache(head=_replay_rows)


def _start(path: Path, starts: StartCache | None) -> int:
    first = next(_replay_rows(path), None)
    if first is None or not as_dict(first[1].get("payload")).get("forked_from_id"):
        return 0
    return (starts or start_cache()).start(path)


def _tail(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    cut = text[-budget:]
    newline = cut.find("\n")
    return cut[newline + 1 :] if 0 <= newline < len(cut) - 1 else cut


def _error_text(code: int | None, output: str) -> str:
    prefix = f"Exit code {code}\n" if code is not None else ""
    return taxonomy.cap_error(prefix + _tail(output, TAIL_CHARS - len(prefix)))


def _rejected(status: object) -> _Outcome:
    return _Outcome("error", True, "user_rejected", str(status))


def _shell_outcome(command: str | None, status: object, code: int | None, output: str) -> _Outcome:
    if status in taxonomy.CODEX_REJECTED_STATUSES:
        return _rejected(status)
    failed = status not in taxonomy.CODEX_OK_STATUSES or (code is not None and code != 0)
    if not failed:
        return _Outcome("ok", is_error=False)
    if taxonomy.benign_exit(command, code, output):
        return _Outcome("ok", is_error=True)
    text = _error_text(code, output)
    category = taxonomy.categorize_error(text) or ("exit_code" if code is not None else None)
    return _Outcome("error", is_error=True, category=category, error_text=text or None)


def _flagged(text: str) -> _Outcome:
    if taxonomy.CODEX_FAILED_OUTPUT.match(text.lstrip()):
        error = taxonomy.cap_error(text)
        return _Outcome("error", True, taxonomy.categorize_error(error), error)
    return _Outcome("ok", is_error=False)


def _status_outcome(status: object, text: str) -> _Outcome:
    if status is None or status in taxonomy.CODEX_OK_STATUSES:
        return _Outcome("ok", is_error=False)
    if status in taxonomy.CODEX_REJECTED_STATUSES:
        return _rejected(status)
    error = taxonomy.cap_error(text or str(status))
    return _Outcome("error", True, taxonomy.categorize_error(error), error)


def _clip(text: str) -> str:
    if len(text) <= 2 * TAIL_CHARS:
        return text
    return f"{text[:TAIL_CHARS]}\n{text[-TAIL_CHARS:]}"


def _slim(item: Row) -> Row:
    kept = {k: v for k, v in item.items() if k not in BULKY_KEYS}
    outputs = {k: _clip(v) for k in OUTPUT_KEYS if (v := _text(item.get(k)))}
    failed = item.get("status") not in taxonomy.CODEX_OK_STATUSES
    reason = _content_text(item.get("error")) or _content_text(item.get("result")) if failed else ""
    return {**kept, **outputs, **({"error": _clip(reason)} if reason else {})}


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_content_text(v) for v in value if v)
    data = as_dict(value)
    if "content" in data:
        return _content_text(data["content"])
    return _text(data.get("text")) or _text(data.get("message")) or ""


def _draft(  # noqa: PLR0913 -- one constructor for every call shape
    anchor: tuple[int, str, int],
    call_id: str,
    tool: str,
    kind: ToolKind,
    shown: object,
    outcome: _Outcome,
    *,
    command: str | None = None,
    file_path: str | None = None,
) -> _Draft:
    line, timestamp, order = anchor
    raw = shown if isinstance(shown, str) else json.dumps(shown, sort_keys=True, default=str)
    return _Draft(
        line=line,
        order=order,
        call_id=call_id,
        timestamp=timestamp,
        tool=tool,
        kind=kind,
        command=command,
        file_path=file_path,
        input_hash=taxonomy.input_hash(shown),
        preview=taxonomy.preview(command or raw, taxonomy.INPUT_PREVIEW_CHARS),
        outcome=outcome,
    )


def _exec_item(anchor: tuple[int, str, int], item: Row) -> list[_Draft]:
    command = taxonomy.codex_command(item.get("command"))
    output = _text(item.get("aggregated_output")) or _text(item.get("stdout")) or ""
    outcome = _shell_outcome(
        command, item.get("status"), _int_or_none(item.get("exit_code")), output
    )
    return [
        _draft(
            anchor,
            str(item.get("id") or ""),
            "exec_command",
            taxonomy.codex_kind(command, item.get("parsed_cmd")),
            {"command": command, "cwd": item.get("cwd")},
            outcome,
            command=command,
        )
    ]


def _patch_drafts(
    anchor: tuple[int, str, int], call_id: str, changes: Mapping[str, Any], outcome: _Outcome
) -> list[_Draft]:
    paths: list[str | None] = [*changes] or [None]
    line, timestamp, order = anchor
    return [
        _draft(
            (line, timestamp, order + i),
            call_id,
            "apply_patch",
            "write",
            {"path": path, "change": changes.get(path) if path else None},
            outcome,
            file_path=path,
        )
        for i, path in enumerate(paths)
    ]


def _file_change(anchor: tuple[int, str, int], item: Row) -> list[_Draft]:
    changes = {str(k): v for k, v in as_dict(item.get("changes")).items()}
    text = _text(item.get("stderr")) or _text(item.get("stdout")) or ""
    outcome = _status_outcome(item.get("status"), text)
    return _patch_drafts(anchor, str(item.get("id") or ""), changes, outcome)


def _mcp(anchor: tuple[int, str, int], item: Row) -> list[_Draft]:
    tool = f"mcp__{item.get('server') or ''}__{item.get('tool') or ''}"
    text = _content_text(item.get("error")) or _content_text(item.get("result"))
    return [
        _draft(
            anchor,
            str(item.get("id") or ""),
            tool,
            "mcp",
            item.get("arguments"),
            _status_outcome(item.get("status"), text),
        )
    ]


def _generic_item(anchor: tuple[int, str, int], item: Row, name: str | None) -> list[_Draft]:
    kind_name = str(item.get("type"))
    if kind_name == "SubAgentActivity" and item.get("kind") in NOTICE_ACTIVITIES:
        return []
    tool = (
        name
        or ITEM_TOOLS.get(kind_name)
        or ITEM_TOOLS.get(str(item.get("kind")))
        or str(item.get("tool") or item.get("kind") or kind_name)
    )
    agentic = kind_name in {"CollabAgentToolCall", "SubAgentActivity"}
    kind: ToolKind = "agent" if agentic else taxonomy.tool_kind(tool, None)
    shown = {k: v for k, v in item.items() if k not in {"id", "results", "agents_states"}}
    return [
        _draft(
            anchor,
            str(item.get("id") or ""),
            tool,
            kind,
            shown,
            _status_outcome(item.get("status"), ""),
            file_path=_text(item.get("path")) if kind == "read" else None,
        )
    ]


def _item_drafts(anchor: tuple[int, str, int], item: Row, name: str | None) -> list[_Draft]:
    kind = item.get("type")
    if kind == "CommandExecution":
        return _exec_item(anchor, item)
    if kind == "FileChange":
        return _file_change(anchor, item)
    if kind == "McpToolCall":
        return _mcp(anchor, item)
    return _generic_item(anchor, item, name)


def _arguments(call: _FnCall) -> Row:
    raw = call.payload.get("arguments")
    if isinstance(raw, dict):
        return raw
    parsed = _parse(raw) if isinstance(raw, str) else None
    return parsed or {}


def _output_parts(output: object) -> tuple[str, int | None]:
    text = _content_text(output)
    data = _parse(text) if text.lstrip().startswith("{") else None
    if data is not None and "output" in data:
        body = _content_text(data.get("output"))
        code = _int_or_none(as_dict(data.get("metadata")).get("exit_code"))
        return body, code
    return taxonomy.CODEX_OUTPUT_HEADER.sub("", text), taxonomy.output_exit_code(text)


def _slim_output(output: object) -> _Output:
    body, code = _output_parts(output)
    return _Output(_clip(body), code)


def _shell_call(anchor: tuple[int, str, int], call_id: str, call: _FnCall) -> list[_Draft]:
    args = _arguments(call)
    command = taxonomy.codex_command(args.get("command") or args.get("cmd"))
    if call.output is None:
        outcome = _Outcome("unknown")
    else:
        body, code = call.output.body, call.output.code
        outcome = (
            _shell_outcome(command, "completed", code, body) if code is not None else _flagged(body)
        )
    kind = taxonomy.codex_kind(command, None)
    return [
        _draft(
            anchor,
            call_id,
            "exec_command",
            kind,
            {"command": command, "cwd": args.get("workdir")},
            outcome,
            command=command,
        )
    ]


def _patch_call(anchor: tuple[int, str, int], call_id: str, call: _FnCall) -> list[_Draft]:
    patch = _text(call.payload.get("input")) or _text(_arguments(call).get("input")) or ""
    paths = taxonomy.PATCH_FILE_LINE.findall(patch)
    if call.output is None:
        outcome = _Outcome("unknown")
    else:
        body, code = call.output.body, call.output.code
        outcome = (
            _shell_outcome(None, "completed", code, body) if code is not None else _flagged(body)
        )
    return _patch_drafts(anchor, call_id, dict.fromkeys(paths, patch), outcome)


def _response_drafts(anchor: tuple[int, str, int], call_id: str, call: _FnCall) -> list[_Draft]:
    if call.name in taxonomy.CODEX_SHELL_CALLS:
        return _shell_call(anchor, call_id, call)
    if call.name in taxonomy.PATCH_TOOLS:
        return _patch_call(anchor, call_id, call)
    body = call.output.body if call.output is not None else ""
    if call.name in taxonomy.CODEX_SCRIPT_CALLS:
        failed = call.children == 0 and taxonomy.CODEX_FAILED_OUTPUT.match(body.lstrip())
        return [_draft(anchor, call_id, call.name, "other", {}, _flagged(body))] if failed else []
    outcome = _flagged(body) if call.output is not None else _Outcome("unknown")
    shown = call.payload.get("arguments") or call.payload.get("input") or {}
    mcp = taxonomy.mcp_parts(call.name)
    kind: ToolKind = "mcp" if mcp else taxonomy.tool_kind(call.name, None)
    return [_draft(anchor, call_id, call.name, kind, shown, outcome)]


def _from_item(scan: _Scan, line: int, timestamp: str, item: Row) -> list[_Draft]:
    call = scan.calls.get(str(item.get("id")))
    anchor = (call.line, call.timestamp, 0) if call else (line, timestamp, 0)
    return _item_drafts(anchor, item, call.name if call else None)


def _drafts(scan: _Scan) -> list[_Draft]:
    matched = {str(item.get("id")) for _, _, item in scan.items}
    from_items = (d for line, ts, item in scan.items for d in _from_item(scan, line, ts, item))
    from_calls = (
        d
        for call_id, call in scan.calls.items()
        if call_id not in matched
        for d in _response_drafts((call.line, call.timestamp, 0), call_id, call)
    )
    return sorted((*from_items, *from_calls), key=lambda d: (d.line, d.order))


def _call_fact(path: str, seq: int, draft: _Draft) -> ToolCallFact:
    command = draft.command[:COMMAND_CHARS] if draft.command else None
    outcome = draft.outcome
    tail = build.call_tail(
        draft.tool, command, outcome.status, outcome.category, outcome.error_text
    )
    return ToolCallFact(
        seq=seq,
        line=draft.line,
        call_id=draft.call_id,
        dedupe_key=f"{path}|{seq}",
        timestamp=draft.timestamp,
        tool=draft.tool,
        kind=draft.kind,
        mcp_server=tail.mcp_server,
        mcp_tool=tail.mcp_tool,
        skill=None,
        command=command,
        command_class=tail.command_class,
        file_path=draft.file_path,
        input_hash=draft.input_hash,
        input_preview=draft.preview,
        edit_old_hash=None,
        edit_new_hash=None,
        is_error=outcome.is_error,
        status=outcome.status,
        error_category=outcome.category,
        error_text=outcome.error_text,
        signature=tail.signature,
        on_main_branch=True,
    )


def _identity(scan: _Scan) -> _Identity:
    own_id = scan.own_id
    metas = scan.metas
    meta = next((m for m in metas if m.get("id") == own_id), metas[0] if metas else {})
    source = meta.get("source")
    subagent = as_dict(as_dict(source).get("subagent"))
    spawn = as_dict(subagent.get("thread_spawn"))
    parent = _text(spawn.get("parent_thread_id")) or (
        _text(meta.get("parent_thread_id")) if subagent else None
    )
    root = _text(meta.get("session_id"))
    meta_cwd = _text(meta.get("cwd"))
    return _Identity(
        own_id=own_id,
        session_id=(root if root and root != own_id else parent) if parent else own_id,
        thread_id=own_id if parent else MAIN_THREAD,
        parent_thread_id=parent,
        subagent_type=_text(spawn.get("agent_role")) or _text(subagent.get("other")),
        subagent_description=_text(spawn.get("agent_path")) or _text(meta.get("agent_path")),
        cwd=_strip_file(scan.turn_cwd or meta_cwd),
        git_branch=_text(as_dict(meta.get("git")).get("branch")),
        context_window=_int_or_none(meta.get("context_window")),
    )


def _strip_file(cwd: str | None) -> str | None:
    return cwd.removeprefix("file://") if cwd else None


def _request(
    scan: _Scan, record: Row, payload: Row, line: int, key: str
) -> tuple[RequestFact | None, tuple[Any, ...] | None]:
    info = as_dict(payload.get("info"))
    last = as_dict(info.get("last_token_usage"))
    timestamp = record.get("timestamp")
    if not last or not timestamp:
        return None, scan.previous_turn
    turn = turn_key(timestamp, info)
    if turn == scan.previous_turn:
        return None, scan.previous_turn
    fact = build.request_fact(
        line=line,
        dedupe_key=f"{key}|{line}",
        timestamp=str(timestamp),
        model=scan.raw_model if isinstance(scan.raw_model, str) else None,
        tokens=codex_tokens(last),
        context_window=_int_or_none(info.get("model_context_window")),
        previous=scan.requests[-1] if scan.requests else None,
    )
    return fact, turn


def _user_message(
    scan: _Scan, item: Row, line: int, timestamp: str, *, fallback: bool = False
) -> None:
    parts = [p for p in item.get("content") or [] if isinstance(p, dict)]
    text = "\n".join(
        t for p in parts if p.get("type") in TEXT_PARTS and (t := _text(p.get("text")))
    )
    skill = next((_text(p.get("name")) for p in parts if p.get("type") == "skill"), None)
    slash = f"/{skill}" if skill else None
    lead = text.lstrip()
    if lead.startswith(taxonomy.CODEX_INJECTED_PREFIXES) or (text and is_preamble(text)):
        return
    shown = text or slash
    if not shown or (fallback and scan.saw_user_item):
        return
    scan.last_event = "user"
    if fallback:
        scan.fallback_messages.append((line, timestamp, shown, slash, False))
        return
    scan.messages.append((line, timestamp, shown, slash, False))
    scan.saw_user_item = True
    scan.compaction_pending = None


def _item(scan: _Scan, item: Row, line: int, timestamp: str) -> None:
    kind = item.get("type")
    if kind in TOOL_ITEMS:
        scan.items.append((line, timestamp, _slim(item)))
        for script in scan.open_scripts:
            script.children += 1
        scan.last_event = "tool_result"
    elif kind == "UserMessage":
        _user_message(scan, item, line, timestamp)
    elif kind == "AgentMessage":
        scan.last_event = "assistant_text"
    elif kind == "ContextCompaction":
        if scan.compaction_pending == "record":
            scan.compaction_pending = None
        else:
            scan.compactions.append((line, timestamp, _last_context(scan)))
            scan.compaction_pending = "item"


def _last_context(scan: _Scan) -> int | None:
    return scan.requests[-1].context_tokens if scan.requests else None


def _response(scan: _Scan, payload: Row, line: int, timestamp: str) -> None:
    kind = payload.get("type")
    call_id = str(payload.get("call_id") or f"line:{line}")
    if kind in CALL_TYPES:
        call = _FnCall(line, timestamp, str(payload.get("name") or ""), payload)
        scan.calls.setdefault(call_id, call)
        if call.name in taxonomy.CODEX_SCRIPT_CALLS:
            scan.open_scripts.append(call)
        scan.last_event = "tool_pending"
    elif kind == "message" and payload.get("role") == "user":
        _user_message(scan, payload, line, timestamp, fallback=True)
    elif kind in OUTPUT_TYPES and call_id in scan.calls:
        call = scan.calls[call_id]
        call.output = _slim_output(payload.get("output"))
        scan.open_scripts = [s for s in scan.open_scripts if s is not call]
        scan.last_event = "tool_result"


def _event(scan: _Scan, payload: Row, line: int, timestamp: str) -> None:
    kind = payload.get("type")
    if kind == "item_completed":
        _item(scan, as_dict(payload.get("item")), line, timestamp)
    elif kind == "task_complete":
        scan.last_event = "turn_complete"
        final = _text(payload.get("last_agent_message"))
        scan.final_text = final[-FINAL_TEXT_CHARS:] if final else scan.final_text
    elif kind == "turn_aborted":
        scan.last_event = "turn_aborted"
        if INTERRUPT_REASON in str(payload.get("reason") or ""):
            scan.messages.append((line, timestamp, INTERRUPT_TEXT, None, True))


def _own(scan: _Scan, record: Row, line: int, key: str) -> None:
    kind = record.get("type")
    payload = as_dict(record.get("payload"))
    timestamp = str(record.get("timestamp") or "")
    if kind == "turn_context":
        scan.raw_model = payload.get("model") or scan.raw_model
        scan.turn_cwd = _text(payload.get("cwd")) or scan.turn_cwd
    elif kind == "compacted":
        if scan.compaction_pending == "item":
            scan.compaction_pending = None
        else:
            scan.compactions.append((line, timestamp, _last_context(scan)))
            scan.compaction_pending = "record"
    elif kind == "response_item":
        _response(scan, payload, line, timestamp)
    elif payload.get("type") == "token_count" and own_thread(payload, scan.own_id):
        request, scan.previous_turn = _request(scan, record, payload, line, key)
        if request is not None:
            scan.requests.append(request)
    elif kind == "event_msg" and own_thread(payload, scan.own_id):
        _event(scan, payload, line, timestamp)


def _track(scan: _Scan, record: Row) -> None:
    timestamp = _text(record.get("timestamp"))
    if timestamp:
        scan.first_ts = scan.first_ts or timestamp
        scan.last_ts = timestamp


def _scan(path: Path, start: int) -> _Scan:
    scan = _Scan(own_id=rollout_session_id(path))
    key = str(path)
    for index, raw in _lines(path):
        own = index >= start
        if not own and '"session_meta"' not in raw:
            continue
        record = _parse(raw) if any(token in raw for token in PREFILTER) else None
        if record is None:
            continue
        is_meta = record.get("type") == "session_meta"
        if is_meta:
            scan.metas.append(as_dict(record.get("payload")))
        if own:
            _track(scan, record)
        if own and not is_meta:
            _own(scan, record, index + 1, key)
    return scan


def _origin(identity: _Identity) -> MessageOrigin:
    return "agent" if identity.parent_thread_id else "human"


def _message_fact(
    key: str, entry: tuple[int, str, str, str | None, bool], before: int, origin: MessageOrigin
) -> UserMessageFact:
    line, timestamp, text, slash, interrupt = entry
    return build.message_fact(
        before_call=before,
        line=line,
        message_id=f"{key}|{line}",
        dedupe_key=f"{key}|{line}",
        timestamp=timestamp,
        origin=origin,
        text=text,
        is_interrupt=interrupt,
        slash_command=slash,
    )


def _messages(scan: _Scan) -> list[tuple[int, str, str, str | None, bool]]:
    if scan.saw_user_item:
        return scan.messages
    return sorted((*scan.messages, *scan.fallback_messages), key=lambda m: m[0])


def extract_codex(ref: TranscriptRef, starts: StartCache | None = None) -> TranscriptFacts:
    scan = _scan(ref.path, _start(ref.path, starts))
    identity = _identity(scan)
    key = str(ref.path)
    requests = tuple(
        r if r.context_window else replace(r, context_window=identity.context_window)
        for r in scan.requests
    )
    calls = tuple(_call_fact(key, seq, d) for seq, d in enumerate(_drafts(scan)))
    lines = [c.line for c in calls]
    origin = _origin(identity)
    return TranscriptFacts(
        ref=ref,
        session_id=identity.session_id,
        thread_id=identity.thread_id,
        thread_key=identity.own_id,
        parent_thread_id=identity.parent_thread_id,
        parent_tool_use_id=None,
        subagent_type=identity.subagent_type,
        subagent_description=identity.subagent_description,
        cwd=identity.cwd,
        git_branch=identity.git_branch,
        first_ts=scan.first_ts,
        last_ts=scan.last_ts,
        final_text=scan.final_text,
        last_event=scan.last_event,
        api_errors=0,
        requests=requests,
        tool_calls=calls,
        user_messages=tuple(
            _message_fact(key, m, bisect_left(lines, m[0]), origin) for m in _messages(scan)
        ),
        compactions=tuple(
            CompactionFact(
                before_call=bisect_left(lines, line),
                line=line,
                timestamp=timestamp,
                trigger="auto",
                pre_tokens=pre,
                post_tokens=None,
            )
            for line, timestamp, pre in scan.compactions
        ),
        rewinds=(),
    )
