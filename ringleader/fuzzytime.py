import datetime as dt
import re

_UNIT_DELTAS = {
    "h": lambda n: dt.timedelta(hours=n),
    "d": lambda n: dt.timedelta(days=n),
    "w": lambda n: dt.timedelta(weeks=n),
    "mo": lambda n: dt.timedelta(days=n * 30),
}

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_AMOUNT_UNIT_RE = re.compile(r"^(\d+)(h|d|w|mo)$")
_BARE_INT_RE = re.compile(r"^\d+$")


def parse_duration(text: str, now: dt.datetime) -> str:
    normalized = text.strip().lower()

    match = _AMOUNT_UNIT_RE.match(normalized)
    if match:
        amount, unit = match.groups()
        return (now + _UNIT_DELTAS[unit](int(amount))).isoformat()

    if _BARE_INT_RE.match(normalized):
        return (now + dt.timedelta(days=int(normalized))).isoformat()

    if normalized == "tomorrow":
        return (now + dt.timedelta(days=1)).isoformat()

    if normalized == "next week":
        return (now + dt.timedelta(weeks=1)).isoformat()

    weekday_name = normalized.removeprefix("next ")
    if weekday_name in _WEEKDAYS:
        return _next_weekday(now, _WEEKDAYS[weekday_name]).isoformat()

    raise ValueError(f"unparseable duration: {text!r}")


def _next_weekday(now: dt.datetime, target_weekday: int) -> dt.datetime:
    days_ahead = (target_weekday - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return now + dt.timedelta(days=days_ahead)
