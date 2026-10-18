from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

_SKEW_SECONDS = 86400


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


def as_dict(value: object) -> dict[str, Any]:
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


def record_day(rec: UsageRecord) -> str:
    return (rec.timestamp or "")[:10]


def mtime_cutoff(since: date | None) -> float | None:
    if since is None:
        return None
    midnight = datetime(since.year, since.month, since.day, tzinfo=UTC)
    return (midnight - timedelta(seconds=_SKEW_SECONDS)).timestamp()


def mtime_of(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def files_since(files: Iterable[Path], since: date | None) -> list[Path]:
    cutoff = mtime_cutoff(since)
    if cutoff is None:
        return sorted(files)
    return sorted(f for f in files if (m := mtime_of(f)) is not None and m >= cutoff)


def session_files(
    files: Iterable[Path],
    since: date | None,
    session_prefix: str | None,
    session_of: Callable[[Path], str],
) -> list[Path]:
    return [
        f
        for f in files_since(files, since)
        if not session_prefix or session_of(f).startswith(session_prefix)
    ]


def newest_first(files: Iterable[Path]) -> Iterator[tuple[datetime, Path]]:
    stamped = sorted(
        ((m, f) for f in files if (m := mtime_of(f)) is not None),
        key=lambda t: (-t[0], str(t[1])),
    )
    for mtime, f in stamped:
        yield datetime.fromtimestamp(mtime + _SKEW_SECONDS, UTC), f


def latest_day_of(
    files: Iterable[Path], records_in: Callable[[Path], Iterable[UsageRecord]]
) -> str | None:
    best = ""
    for ceiling, f in newest_first(files):
        floor = parse_iso(best)
        if floor is not None and ceiling < floor:
            break
        best = max([best, *(record_day(r) for r in records_in(f))])
    return best or None


@runtime_checkable
class UsageReader(Protocol):
    name: str

    def available(self) -> bool: ...
    def records(
        self, since: date | None = None, session_prefix: str | None = None
    ) -> Iterator[UsageRecord]: ...
    def latest_day(self) -> str | None: ...
