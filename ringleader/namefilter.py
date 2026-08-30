from __future__ import annotations

import re
from functools import lru_cache

_WILDCARDS = {"*": ".*", "?": "."}


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile("".join(_WILDCARDS.get(ch, re.escape(ch)) for ch in pattern))


def matches(name: str, pattern: str) -> bool:
    """Glob with `*` and `?` only: `[` and `]` stay literal, so a pattern like
    `[cadet-city]*` matches the bracketed prefix instead of a character class."""
    return _compiled(pattern).fullmatch(name) is not None


def display_name(thread: dict) -> str | None:
    return thread.get("title") or thread.get("suggestedName")


def first_match(name: str | None, patterns) -> str | None:
    if not name:
        return None
    for pattern in patterns:
        if pattern and matches(name, pattern):
            return pattern
    return None


def thread_match(thread: dict, patterns) -> str | None:
    return first_match(display_name(thread), patterns)
