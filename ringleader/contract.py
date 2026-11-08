from __future__ import annotations

import json
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, get_args

from . import overview, registry, resume, scanner
from .agents import source_of
from .codes import OutcomeCode
from .handoff import HandoffPlan, HandoffReady
from .home import state as home_state

if TYPE_CHECKING:
    from . import decisions
    from .decisions import Decision
    from .revalidate import Identity

CONTRACT_VERSION: Final = 1
TAIL_DEFAULT_CHARS: Final = 200_000
TAIL_MIN_CHARS: Final = 1_000
TAIL_MAX_CHARS: Final = 2_000_000
TAIL_MAX_IDS: Final = 16
LIST_TEXT_MAX_CHARS: Final = 500
type OutcomeStatus = Literal["ok", "refused", "empty", "failed"]
type PresenceCode = Literal["not_installed", "failed"]

PRESENCE_CODES: Final[tuple[PresenceCode, ...]] = get_args(PresenceCode.__value__)

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
    code: PresenceCode | None

    def __post_init__(self) -> None:
        if (self.live is None) != (self.code is not None):
            raise ValueError("unknown presence needs a code, and only unknown presence has one")

    @classmethod
    def ok(cls, live: frozenset[Identity]) -> PresenceView:
        return cls(live, None, None)

    @classmethod
    def unknown(cls, problem: str, code: PresenceCode) -> PresenceView:
        return cls(None, problem, code)


@dataclass(frozen=True)
class AgentView:
    source: str
    installed: bool


@dataclass(frozen=True)
class GuiOutcome:
    status: OutcomeStatus
    reason: str | None
    message: str
    wrote: bool = False
    path: str | None = None
    warnings: tuple[str, ...] = ()
    code: OutcomeCode | None = None

    def __post_init__(self) -> None:
        if (self.status in ("refused", "failed")) != (self.code is not None):
            raise ValueError(
                f"a {self.status} outcome {'needs' if self.code is None else 'has no'} code"
            )


@dataclass(frozen=True)
class Surroundings:
    presence: PresenceView
    stale_hours: int
    pending_harvests: int
    agents: Sequence[AgentView]


class UntilError(ValueError):
    def __init__(self, message: str, code: OutcomeCode) -> None:
        super().__init__(message)
        self.code = code


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
    mtime = thread.get("mtime")
    if isinstance(mtime, str):
        return _instant(mtime), registry.qualified_id(thread)
    raise ValueError(f"session {registry.qualified_id(thread)} has no string mtime")


def inbox_threads(reg: dict[str, Any], now_iso: str) -> list[dict[str, Any]]:
    return sorted(overview.triage_inbox(home_state.visible_threads(reg), now_iso), key=_inbox_order)


def _roundup_age(last_at: Any, now_iso: str) -> str | None:
    if not isinstance(last_at, str) or not last_at:
        return None
    try:
        return overview.age_human(last_at, now_iso)
    except ValueError:
        return None


def _roundup(state: home_state.HomeState, stale_hours: int) -> dict[str, Any]:
    return {
        "last_at": state.last_roundup_at,
        "age_human": _roundup_age(state.last_roundup_at, state.now_iso),
        "stale": state.roundup_stale,
        "stale_hours": stale_hours,
    }


def _project(ref: home_state.ProjectRef) -> dict[str, Any]:
    return {
        "key": ref.key,
        "label": ref.label,
        "pending": ref.pending,
        "unclassified": ref.unclassified,
        "oldest_mtime": ref.oldest_mtime,
        "newest_mtime": ref.newest_mtime,
    }


def _step(step: home_state.NextStep) -> dict[str, Any]:
    return {
        "kind": step.kind,
        "deletes": step.deletes,
        "costs_model": step.costs_model,
        "count": step.count,
        "project_key": step.project.key if step.project is not None else None,
        "id": ":".join(step.session) if step.session is not None else None,
    }


def _home(
    state: home_state.HomeState,
    already_listed: AbstractSet[str],
    live: frozenset[Identity] | None,
) -> dict[str, Any]:
    referenced = {registry.qualified_id(t): t for t in (*state.waking_soon, *state.in_flight)}
    return {
        "counts": {
            "triage_total": state.triage_total,
            "live": state.live_count,
            "unclassified": state.unclassified,
            "blocked": state.blocked_count,
            "stale": state.stale_count,
            "kept": state.kept_count,
            "projects": len(state.projects),
            "spread_projects": state.spread_projects,
        },
        "pending_harvests": state.pending_harvests,
        "projects": [_project(ref) for ref in state.projects],
        "waking_soon": [registry.qualified_id(t) for t in state.waking_soon],
        "in_flight": [registry.qualified_id(t) for t in state.in_flight],
        "next_steps": [_step(step) for step in home_state.next_steps(state)],
        "rows": [
            session_row(t, state.now_iso, live)
            for qualified, t in referenced.items()
            if qualified not in already_listed
        ],
    }


def _agents(agents: Sequence[AgentView]) -> list[dict[str, Any]]:
    return [
        {"source": a.source, "agent": resume.agent_name(a.source), "installed": a.installed}
        for a in agents
    ]


def list_payload(
    reg: dict[str, Any],
    now_iso: str,
    *,
    registry_path: str,
    scan: dict[str, Any] | None,
    surroundings: Surroundings,
) -> dict[str, Any]:
    presence = surroundings.presence
    stale_hours = surroundings.stale_hours
    for thread in home_state.visible_threads(reg):
        _inbox_order(thread)
    state = home_state.build_home_state(
        reg, now_iso, pending_harvests=surroundings.pending_harvests, stale_hours=stale_hours
    )
    rows = [session_row(t, now_iso, presence.live) for t in sorted(state.inbox, key=_inbox_order)]
    return {
        "version": CONTRACT_VERSION,
        "kind": "list",
        "generated_at": now_iso,
        "registry": registry_path,
        "recent_days": home_state.RECENT_DAYS,
        "presence": {
            "state": "unknown" if presence.live is None else "ok",
            "problem": presence.problem,
            "code": presence.code,
        },
        "roundup": _roundup(state, stale_hours),
        "scan": scan,
        "agents": _agents(surroundings.agents),
        "home": _home(state, {row["id"] for row in rows}, presence.live),
        "sessions": rows,
    }


def scan_summary(
    scanned_at: str,
    before: AbstractSet[Identity],
    after: AbstractSet[Identity],
    unavailable: AbstractSet[str],
) -> dict[str, Any]:
    return {
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


def handoff_payload(plan: HandoffPlan) -> dict[str, Any]:
    ready = isinstance(plan, HandoffReady)
    return {
        "version": CONTRACT_VERSION,
        "kind": "handoff",
        "purpose": "triage",
        "status": "ok" if ready else "refused",
        "code": None if isinstance(plan, HandoffReady) else plan.code,
        "reason": None if isinstance(plan, HandoffReady) else plan.message,
        "source": plan.source,
        "agent": resume.agent_name(plan.source),
        "project_key": plan.project_key,
        "cwd": plan.cwd if isinstance(plan, HandoffReady) else None,
        "argv": list(plan.argv) if isinstance(plan, HandoffReady) else None,
    }


def error_payload(message: str) -> dict[str, Any]:
    return {"version": CONTRACT_VERSION, "kind": "error", "message": message}


def encode_document(doc: dict[str, Any]) -> bytes:
    return (json.dumps(doc, ensure_ascii=False) + "\n").encode("utf-8", "replace")


def refused(reason: str, code: OutcomeCode) -> GuiOutcome:
    return GuiOutcome("refused", reason, reason, code=code)


def from_outcome(outcome: decisions.Outcome) -> GuiOutcome:
    status: OutcomeStatus
    reason: str | None
    code: OutcomeCode | None
    if outcome.ok:
        status, reason, code = "ok", None, None
    elif outcome.refused:
        status, reason, code = "refused", outcome.refused[0][1], outcome.code or "other"
    else:
        status, reason, code = "failed", None, outcome.code or "failed"
    return GuiOutcome(
        status, reason, outcome.message, outcome.wrote, outcome.path, outcome.warnings, code
    )


def outcome_payload(action: Decision, qualified: str, result: GuiOutcome) -> dict[str, Any]:
    return {
        "version": CONTRACT_VERSION,
        "kind": "outcome",
        "action": action,
        "id": qualified,
        "status": result.status,
        "reason": result.reason,
        "code": result.code,
        "message": result.message,
        "wrote": result.wrote,
        "path": result.path,
        "warnings": list(result.warnings),
    }


def normalize_until(text: str, now: datetime) -> str:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise UntilError(f"--until is not an ISO 8601 time: {text!r}", "until_invalid") from None
    if parsed.tzinfo is None:
        raise UntilError("--until needs a timezone offset", "until_naive")
    if parsed <= now:
        raise UntilError("snooze time must be in the future", "until_past")
    return parsed.astimezone(UTC).isoformat()
