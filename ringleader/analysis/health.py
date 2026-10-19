from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from ..usage.base import parse_iso
from .facts import LastEvent, TranscriptFacts
from .signals import SIGNAL_KINDS, SignalKind, is_failure, live_calls

if TYPE_CHECKING:
    from .episodes import Episode

type Outcome = Literal["open", "completed", "abandoned", "errored", "unknown"]
type PenaltyName = SignalKind | Literal["tool_error_rate", "outcome_errored", "outcome_abandoned"]

RECENCY_GUARD = timedelta(minutes=10)
TOOL_ERROR_RATE_MIN_CALLS = 5
PENALTIES: dict[PenaltyName, tuple[int, int]] = {
    "retry_run": (5, 15),
    "runaway_window": (10, 20),
    "edit_churn": (3, 12),
    "failure_streak": (5, 15),
    "reread_churn": (1, 4),
    "search_loop": (2, 6),
    "unsupported_completion": (10, 10),
    "user_interruption": (3, 12),
    "rejected_tool_use": (2, 8),
    "permission_denial": (1, 5),
    "compaction": (1, 5),
    "mid_task_compaction": (5, 10),
    "rewind": (3, 9),
    "tool_failure": (0, 0),
    "tool_error_rate": (30, 15),
    "outcome_errored": (10, 10),
    "outcome_abandoned": (5, 5),
}
OUTCOME_PENALTIES: dict[Outcome, PenaltyName] = {
    "errored": "outcome_errored",
    "abandoned": "outcome_abandoned",
}
ABANDONED_EVENTS: frozenset[LastEvent] = frozenset(
    {"interrupt", "turn_aborted", "tool_pending", "user"}
)
COMPLETED_EVENTS: frozenset[LastEvent] = frozenset({"assistant_text", "turn_complete"})


@dataclass(frozen=True)
class Penalty:
    signal: PenaltyName
    count: int
    points: int


@dataclass(frozen=True)
class HealthScore:
    score: int
    points: int
    per_100_calls: float
    penalties: tuple[Penalty, ...]


@dataclass(frozen=True)
class ThreadStats:
    calls: int
    live_calls: int
    errors: int
    writes: int
    episode_tokens: int


def classify_outcome(
    last_event: LastEvent, last_ts: str | None, now: datetime, running: bool | None
) -> Outcome:
    if running:
        return "open"
    last = parse_iso(last_ts)
    if running is None and last is not None and now - last < RECENCY_GUARD:
        return "open"
    if last_ts is None:
        return "unknown"
    if last_event == "api_error":
        return "errored"
    if last_event in ABANDONED_EVENTS:
        return "abandoned"
    if last_event in COMPLETED_EVENTS:
        return "completed"
    return "unknown"


def _signal_penalty(signal: SignalKind, count: int) -> Penalty:
    points, cap = PENALTIES[signal]
    return Penalty(signal, count, min(points * count, cap))


def _error_rate_penalty(tool_calls: int, tool_errors: int) -> Penalty | None:
    if tool_calls < TOOL_ERROR_RATE_MIN_CALLS:
        return None
    scale, cap = PENALTIES["tool_error_rate"]
    return Penalty(
        "tool_error_rate", tool_errors, min(round(scale * tool_errors / tool_calls), cap)
    )


def _outcome_penalty(outcome: Outcome) -> Penalty | None:
    signal = OUTCOME_PENALTIES.get(outcome)
    return Penalty(signal, 1, PENALTIES[signal][0]) if signal else None


def known_signals(counts: Mapping[str, int]) -> dict[SignalKind, int]:
    return {kind: n for name, n in counts.items() if (kind := SIGNAL_KINDS.get(name)) is not None}


def health(
    signal_counts: Mapping[SignalKind, int], tool_calls: int, tool_errors: int, outcome: Outcome
) -> HealthScore:
    candidates = (
        *(_signal_penalty(signal, count) for signal, count in signal_counts.items()),
        _error_rate_penalty(tool_calls, tool_errors),
        _outcome_penalty(outcome),
    )
    penalties = tuple(
        sorted(
            (p for p in candidates if p is not None and p.points > 0),
            key=lambda p: (-p.points, p.signal),
        )
    )
    points = sum(p.points for p in penalties)
    return HealthScore(
        score=max(0, 100 - points),
        points=points,
        per_100_calls=points * 100 / max(tool_calls, 1),
        penalties=penalties,
    )


def thread_stats(facts: TranscriptFacts, episodes: Sequence[Episode]) -> ThreadStats:
    live = live_calls(facts)
    return ThreadStats(
        calls=len(facts.tool_calls),
        live_calls=len(live),
        errors=sum(1 for c in live if is_failure(c)),
        writes=sum(1 for c in live if c.kind == "write"),
        episode_tokens=sum(e.tokens for e in episodes),
    )


def fingerprint_material() -> str:
    material = {
        "recency_guard_seconds": RECENCY_GUARD.total_seconds(),
        "tool_error_rate_min_calls": TOOL_ERROR_RATE_MIN_CALLS,
        "penalties": {k: list(v) for k, v in PENALTIES.items()},
        "outcome_penalties": OUTCOME_PENALTIES,
        "abandoned_events": sorted(ABANDONED_EVENTS),
        "completed_events": sorted(COMPLETED_EVENTS),
    }
    return json.dumps(material, sort_keys=True, ensure_ascii=False)
