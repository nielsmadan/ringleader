from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast


def matches(name: str, pattern: str, *, ignore_case: bool = False) -> bool:
    """Glob with `*` and `?` only: `[` and `]` stay literal, so a pattern like
    `[cadet-city]*` matches the bracketed prefix instead of a character class.

    Backtracks linearly rather than through a regex: `search` runs patterns against
    whole transcript blurbs, and `*a*a*a*c` compiled to `.*a.*a.*a.*c` takes
    exponential time on a long subject that does not match."""
    if ignore_case:
        name, pattern = name.lower(), pattern.lower()
    n = p = 0
    star = -1
    resume = 0
    while n < len(name):
        if p < len(pattern) and pattern[p] == "*":
            star = p
            resume = n
            p += 1
        elif p < len(pattern) and pattern[p] in ("?", name[n]):
            n += 1
            p += 1
        elif star >= 0:
            resume += 1
            n = resume
            p = star + 1
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


def display_name(thread: dict[str, Any]) -> str | None:
    return cast(str | None, thread.get("title") or thread.get("suggestedName"))


def first_match(name: str | None, patterns: Iterable[str]) -> str | None:
    if not name:
        return None
    for pattern in patterns:
        if pattern and matches(name, pattern):
            return pattern
    return None


def thread_match(thread: dict[str, Any], patterns: Iterable[str]) -> str | None:
    return first_match(display_name(thread), patterns)
