from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@dataclass
class UsageRecord:
    source: str
    session_id: str
    project: str | None
    timestamp: str
    provider: str | None
    model: str | None
    input: int  # uncached input only
    output: int  # includes reasoning
    cache_read: int
    cache_write: int
    reasoning: int  # subset of output; never summed into total
    total: int
    cost: float | None = None
    machine: str | None = None
    # stable per-reader identity; dedupe keys on it instead of the timestamp
    record_id: str | None = None


@dataclass
class WindowSnapshot:
    source: str
    timestamp: str
    plan_type: str | None
    window: str  # "5h" | "weekly"
    window_minutes: int
    used_percent: float
    resets_at: str


def parse_iso(value: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat(value or "")
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def window_is_current(w: WindowSnapshot, now: datetime) -> bool:
    end = parse_iso(w.resets_at)
    if end is None:
        seen = parse_iso(w.timestamp)
        if seen is None:
            return False
        end = seen + timedelta(minutes=w.window_minutes)
    return end > now


def total_of(*, input: int, cache_read: int, cache_write: int, output: int) -> int:
    return input + cache_read + cache_write + output


def as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return int(value)
    return 0


def as_float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


@runtime_checkable
class UsageReader(Protocol):
    name: str

    def available(self) -> bool: ...
    def records(self) -> Iterator[UsageRecord]: ...
