from __future__ import annotations

from dataclasses import dataclass

from ..usage.base import TokenCounts, parse_iso
from . import taxonomy
from .facts import (
    MESSAGE_CHARS,
    REBUILD_READ_RATIO,
    CallStatus,
    ErrorCategory,
    MessageOrigin,
    RequestFact,
    UserMessageFact,
)


def is_rebuild(previous: RequestFact | None, tokens: TokenCounts) -> bool:
    return (
        previous is not None
        and tokens.cache_write > 0
        and tokens.cache_read < REBUILD_READ_RATIO * previous.context_tokens
    )


def _gap(timestamp: str, previous: RequestFact | None) -> float | None:
    now, before = parse_iso(timestamp), parse_iso(previous.timestamp if previous else None)
    return (now - before).total_seconds() if now and before else None


def request_fact(  # noqa: PLR0913 -- one fact from its token counts and its neighbours
    *,
    line: int,
    dedupe_key: str,
    timestamp: str,
    model: str | None,
    tokens: TokenCounts,
    context_window: int | None,
    previous: RequestFact | None,
    skill: str | None = None,
    plugin: str | None = None,
) -> RequestFact:
    return RequestFact(
        line=line,
        dedupe_key=dedupe_key,
        timestamp=timestamp,
        model=model,
        input=tokens.input,
        output=tokens.output,
        cache_read=tokens.cache_read,
        cache_write=tokens.cache_write,
        cache_write_5m=tokens.cache_write_5m,
        cache_write_1h=tokens.cache_write_1h,
        reasoning=tokens.reasoning,
        context_tokens=tokens.context,
        context_window=context_window,
        gap_seconds=_gap(timestamp, previous),
        rebuild=is_rebuild(previous, tokens),
        on_main_branch=True,
        skill=skill,
        plugin=plugin,
    )


@dataclass(frozen=True)
class CallTail:
    mcp_server: str | None
    mcp_tool: str | None
    command_class: str
    signature: str | None


def call_tail(
    tool: str,
    command: str | None,
    status: CallStatus,
    category: ErrorCategory | None,
    error_text: str | None,
) -> CallTail:
    cls = taxonomy.command_class(tool, command)
    mcp = taxonomy.mcp_parts(tool)
    return CallTail(
        mcp_server=mcp[0] if mcp else None,
        mcp_tool=mcp[1] if mcp else None,
        command_class=cls,
        signature=taxonomy.error_signature(tool, cls, category, error_text)
        if status == "error"
        else None,
    )


def message_fact(  # noqa: PLR0913 -- one fact from what both transcript formats record
    *,
    before_call: int,
    line: int,
    message_id: str,
    dedupe_key: str,
    timestamp: str,
    origin: MessageOrigin,
    text: str,
    is_interrupt: bool,
    slash_command: str | None,
) -> UserMessageFact:
    capped = text[:MESSAGE_CHARS]
    return UserMessageFact(
        before_call=before_call,
        line=line,
        message_id=message_id,
        dedupe_key=dedupe_key,
        timestamp=timestamp,
        origin=origin,
        text=capped,
        is_interrupt=is_interrupt,
        slash_command=slash_command,
        rule_kind=taxonomy.rule_message_kind(capped, origin, slash_command, is_interrupt),
        content_hash=taxonomy.short_hash(capped),
        on_main_branch=True,
    )
