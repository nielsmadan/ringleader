from __future__ import annotations

from typing import Any

from . import overview

_HOURS_PER_DAY = 24


def is_stale(reg: dict[str, Any], now_iso: str, stale_hours: int) -> bool:
    last = reg.get("lastRoundupAt")
    if not isinstance(last, str) or not last:
        return True
    try:
        return overview.age_days(last, now_iso) * _HOURS_PER_DAY >= stale_hours
    except ValueError:
        return True
