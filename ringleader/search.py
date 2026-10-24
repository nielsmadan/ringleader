from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from . import namefilter, registry

BLURB_FIELDS = ("aiTitle", "whatItIs", "summary", "lastPrompt")

_TIERS = {"name": 0, "id": 1}

# registry text is transcript-derived: an ESC that reaches a terminal is a control
# sequence, not a character, and multi-line text breaks the one-row-per-hit layout
_UNPRINTABLE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class Scope(Enum):
    ALL = "all"
    LIVE = "live"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class Match:
    thread: dict[str, Any]
    matched_on: str
    value: str
    span: tuple[int, int]
    archived: bool = False
    filtered: bool = False


def clean(value: str | None) -> str | None:
    if not value:
        return None
    return " ".join(_UNPRINTABLE.sub(" ", value).split()) or None


def _tier(matched_on: str) -> int:
    return _TIERS.get(matched_on, 2)


def _find(value: str, query: str, glob: bool) -> tuple[int, int] | None:
    if glob:
        return (0, len(value)) if namefilter.matches(value, query, ignore_case=True) else None
    # match the original string, not a lowered copy: "İ".lower() is two code points,
    # so an offset into the copy is not an offset into what the renderer highlights
    found = re.search(re.escape(query), value, re.IGNORECASE)
    return None if found is None else found.span()


def _find_id(value: str, query: str, glob: bool) -> tuple[int, int] | None:
    if glob:
        return (0, len(value)) if namefilter.matches(value, query, ignore_case=True) else None
    found = re.match(re.escape(query), value, re.IGNORECASE)
    return None if found is None else found.span()


def _match_thread(
    thread: dict[str, Any], query: str, glob: bool, *, archived: bool
) -> Match | None:
    fields: list[tuple[str, str | None]] = [
        ("name", clean(namefilter.display_name(thread))),
        ("id", clean(thread.get("sessionId"))),
        *((field, clean(thread.get(field))) for field in BLURB_FIELDS),
    ]
    for matched_on, value in fields:
        if not value:
            continue
        find = _find_id if matched_on == "id" else _find
        span = find(value, query, glob)
        if span is not None:
            return Match(
                thread,
                matched_on,
                value,
                span,
                archived=archived,
                filtered=bool(thread.get("filteredBy")),
            )
    return None


def _archived_thread(rec: dict[str, Any]) -> dict[str, Any]:
    """The tombstone's own fields fill the gaps in the thread it embeds rather than
    losing to it: `rl session archive` derives a summary from a sidecar when the thread
    has no `whatItIs`, and stores that only on the tombstone. A session archived without
    a registry record embeds no thread at all, leaving these as the only fields to match."""
    thread = dict(rec.get("thread") or {})
    fallbacks = {
        "sessionId": rec.get("sessionId"),
        "source": rec.get("source"),
        "project": rec.get("project"),
        "summary": rec.get("summary"),
        "mtime": rec.get("archivedAt"),
    }
    for key, value in fallbacks.items():
        if value and not thread.get(key):
            thread[key] = value
    return thread


def search(reg: dict[str, Any], query: str, *, scope: Scope = Scope.ALL) -> list[Match]:
    q = (query or "").strip().lower()
    if not q:
        return []
    glob = "*" in q or "?" in q
    out: list[Match] = []
    if scope in (Scope.ALL, Scope.LIVE):
        for thread in reg.get("threads", []):
            m = _match_thread(thread, q, glob, archived=False)
            if m is not None:
                out.append(m)
    if scope in (Scope.ALL, Scope.ARCHIVED):
        # a session can legitimately sit in both buckets — that is an unarchive in
        # progress — and there the live thread is the current truth for it
        live = (
            {registry.identity(t) for t in reg.get("threads", [])} if scope is Scope.ALL else set()
        )
        for rec in reg.get("archived", []):
            if registry.identity(rec) in live:
                continue
            m = _match_thread(_archived_thread(rec), q, glob, archived=True)
            if m is not None:
                out.append(m)
    out.sort(key=lambda m: m.thread.get("mtime") or "", reverse=True)
    out.sort(key=lambda m: _tier(m.matched_on))
    return out
