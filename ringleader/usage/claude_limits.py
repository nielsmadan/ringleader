from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

from ..paths import state_dir
from .base import WindowSnapshot, as_dict

DEFAULT_TTL_SECONDS = 600
_EPOCH_LEAK_FLOOR = 1000  # above this a "percentage" is the leaked resets_at epoch
_PERCENT_MAX = 100
_WINDOWS = {"five_hour": ("5h", 300), "seven_day": ("weekly", 10080)}


def default_capture_path() -> Path:
    return state_dir() / "claude-limits.json"


def _pct(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
    except OverflowError:
        return None
    if value > _EPOCH_LEAK_FLOOR:
        return None
    return float(_PERCENT_MAX) if value > _PERCENT_MAX else float(value)


def _epoch_iso(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
    except OverflowError:
        return None
    try:
        return datetime.fromtimestamp(int(value), UTC).isoformat()
    except (OverflowError, ValueError, OSError):
        return None


def read_windows(
    path: str | Path, now: datetime, *, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> list[WindowSnapshot]:
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    body = as_dict(body)
    captured = _epoch_iso(body.get("captured_at"))
    if captured is None:
        return []
    age = now.timestamp() - datetime.fromisoformat(captured).timestamp()
    if age > ttl_seconds:
        return []

    out = []
    limits = as_dict(body.get("rate_limits"))
    for key, (window, minutes) in _WINDOWS.items():
        w = as_dict(limits.get(key))
        pct = _pct(w.get("used_percentage"))
        resets = _epoch_iso(w.get("resets_at"))
        if pct is None or resets is None:
            continue
        out.append(
            WindowSnapshot(
                source="claude",
                timestamp=captured,
                plan_type=None,
                window=window,
                window_minutes=minutes,
                used_percent=pct,
                resets_at=resets,
            )
        )
    return out
