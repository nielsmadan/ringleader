from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from . import overview, registry, resume, roundup_clock, scanner
from .agents import source_of
from .home import state as home_state

if TYPE_CHECKING:
    from .revalidate import Identity

CONTRACT_VERSION: Final = 1
TAIL_DEFAULT_CHARS: Final = 200_000
TAIL_MIN_CHARS: Final = 1_000
TAIL_MAX_CHARS: Final = 2_000_000
TAIL_MAX_IDS: Final = 16
LIST_TEXT_MAX_CHARS: Final = 500
SESSION_ROW_KEYS: Final[tuple[str, ...]] = (
    "age_days",
    "age_human",
    "agent",
    "badges",
    "decision",
    "file",
    "file_fingerprint",
    "finish_on_exit",
    "git_branch",
    "git_project_key",
    "git_project_label",
    "id",
    "in_recent",
    "kept_at",
    "label",
    "last_prompt",
    "lifecycle_block",
    "message_count",
    "mtime",
    "project",
    "project_key",
    "project_label",
    "project_short",
    "reviewed_at_mtime",
    "running",
    "session_id",
    "size_bytes",
    "size_human",
    "snoozed_at",
    "snoozed_until",
    "source",
    "status",
    "what_it_is",
    "woke",
)


@dataclass(frozen=True)
class PresenceView:
    live: frozenset[Identity] | None
    problem: str | None


def clip(text: str | None, limit: int) -> str | None:
    if not isinstance(text, str):
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def is_running(thread: dict[str, Any], live: frozenset[Identity] | None) -> bool | None:
    if live is None:
        return None
    return (resume.agent_name(source_of(thread)), thread.get("sessionId") or "") in live


def lifecycle_block(thread: dict[str, Any]) -> str | None:
    try:
        scanner.filesystem_path(thread, "archive")
    except (RuntimeError, ValueError, KeyError) as error:
        return str(error)
    return None


def session_row(
    thread: dict[str, Any], now_iso: str, live: frozenset[Identity] | None
) -> dict[str, Any]:
    source = source_of(thread)
    project = thread.get("project")
    mtime = thread["mtime"]
    age = overview.age_days(mtime, now_iso)
    size_bytes = thread.get("sizeBytes")
    return {
        "id": registry.qualified_id(thread),
        "source": source,
        "agent": resume.agent_name(source),
        "session_id": thread["sessionId"],
        "label": resume.label(thread),
        "project": project,
        "project_short": home_state.short_path(project),
        "project_key": overview.group_key(thread),
        "project_label": home_state.project_label(thread),
        "git_project_key": thread.get("gitProjectKey"),
        "git_project_label": thread.get("gitProjectLabel"),
        "git_branch": thread.get("gitBranch"),
        "last_prompt": clip(thread.get("lastPrompt"), LIST_TEXT_MAX_CHARS),
        "what_it_is": clip(thread.get("whatItIs"), LIST_TEXT_MAX_CHARS),
        "status": overview.effective_status(thread, now_iso),
        "mtime": mtime,
        "age_days": round(age, 1),
        "age_human": overview.age_human(mtime, now_iso),
        "size_bytes": size_bytes,
        "size_human": overview.human_size(size_bytes),
        "message_count": thread.get("messageCount") or 0,
        "file": thread.get("file"),
        "decision": thread.get("decision"),
        "reviewed_at_mtime": thread.get("reviewedAtMtime"),
        "snoozed_at": thread.get("snoozedAt"),
        "snoozed_until": thread.get("snoozedUntil"),
        "kept_at": thread.get("keptAt"),
        "finish_on_exit": bool(thread.get("finishOnExit")),
        "file_fingerprint": thread.get("fileFingerprint"),
        "running": is_running(thread, live),
        "in_recent": age <= home_state.RECENT_DAYS,
        "woke": home_state.is_woke(thread, now_iso),
        "lifecycle_block": lifecycle_block(thread),
        "badges": resume.badges(thread, now_iso),
    }


def _instant(iso: str) -> datetime:
    parsed = datetime.fromisoformat(iso)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _inbox_order(thread: dict[str, Any]) -> tuple[datetime, str]:
    return _instant(thread["mtime"]), registry.qualified_id(thread)


def inbox_threads(reg: dict[str, Any], now_iso: str) -> list[dict[str, Any]]:
    return sorted(overview.triage_inbox(home_state.visible_threads(reg), now_iso), key=_inbox_order)


def _roundup_age(last_at: Any, now_iso: str) -> str | None:
    if not isinstance(last_at, str) or not last_at:
        return None
    try:
        return overview.age_human(last_at, now_iso)
    except ValueError:
        return None


def _roundup(reg: dict[str, Any], now_iso: str, stale_hours: int) -> dict[str, Any]:
    last_at = reg.get("lastRoundupAt")
    return {
        "last_at": last_at,
        "age_human": _roundup_age(last_at, now_iso),
        "stale": roundup_clock.is_stale(reg, now_iso, stale_hours),
        "stale_hours": stale_hours,
    }


def list_payload(
    reg: dict[str, Any],
    now_iso: str,
    *,
    registry_path: str,
    presence: PresenceView,
    stale_hours: int,
) -> dict[str, Any]:
    return {
        "version": CONTRACT_VERSION,
        "kind": "list",
        "generated_at": now_iso,
        "registry": registry_path,
        "recent_days": home_state.RECENT_DAYS,
        "presence": {
            "state": "unknown" if presence.live is None else "ok",
            "problem": presence.problem,
        },
        "roundup": _roundup(reg, now_iso, stale_hours),
        "sessions": [session_row(t, now_iso, presence.live) for t in inbox_threads(reg, now_iso)],
    }


def scan_payload(
    scanned_at: str, before: set[Identity], after: set[Identity], unavailable: frozenset[str]
) -> dict[str, Any]:
    return {
        "version": CONTRACT_VERSION,
        "kind": "scan",
        "scanned_at": scanned_at,
        "added": len(after - before),
        "removed": len(before - after),
        "unavailable_sources": sorted(unavailable),
    }


def tail_item(
    qualified: str,
    thread: dict[str, Any] | None,
    text: str | None,
    max_chars: int,
    unavailable: str | None,
) -> dict[str, Any]:
    body = "" if unavailable is not None or text is None else text
    return {
        "id": qualified,
        "text": body,
        "max_chars": max_chars,
        "truncated": len(body) >= max_chars,
        "unavailable": unavailable,
        "mtime": thread["mtime"] if thread is not None else None,
        "file_fingerprint": thread.get("fileFingerprint") if thread is not None else None,
    }


def tails_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"version": CONTRACT_VERSION, "kind": "tails", "items": items}


def error_payload(message: str) -> dict[str, Any]:
    return {"version": CONTRACT_VERSION, "kind": "error", "message": message}


def encode_document(doc: dict[str, Any]) -> bytes:
    return (json.dumps(doc, ensure_ascii=False) + "\n").encode("utf-8", "replace")
