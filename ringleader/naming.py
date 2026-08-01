from __future__ import annotations

import json

from . import overview

ACTIVE_WINDOW_MINUTES = 10


def title_line(session_id: str, name: str) -> str:
    return json.dumps(
        {"type": "custom-title", "customTitle": name, "sessionId": session_id},
        ensure_ascii=False,
    )


def _short_suffix(session_id: str) -> str:
    return session_id.replace("-", "")[:6]


def unique_name(suggested: str, session_id: str, taken) -> str:
    if suggested not in taken:
        return suggested
    stripped = session_id.replace("-", "")
    # Try 6-hex suffix first (common path), then longer hex slices, then counters
    for length in (6, 8, 12):
        candidate = f"{suggested}-{stripped[:length]}"
        if candidate not in taken:
            return candidate
    n = 2
    while True:
        candidate = f"{suggested}-{n}"
        if candidate not in taken:
            return candidate
        n += 1


def _age_minutes(thread: dict, now_iso: str) -> float:
    return overview.age_days(thread["mtime"], now_iso) * 1440.0


def candidate_kind(thread: dict, now_iso: str,
                   active_window_minutes: float = ACTIVE_WINDOW_MINUTES):
    suggested = thread.get("suggestedName")
    if not suggested:
        return None
    if thread.get("status", "unclassified") == "unclassified":
        return None
    if _age_minutes(thread, now_iso) < active_window_minutes:
        return None                                  # too recently active
    title = thread.get("title")
    if not title:
        return "new"
    if title == thread.get("appliedName") and suggested != title:
        return "rename"
    return None                                      # user-owned or converged


def select_eligible(reg: dict, now_iso: str,
                    active_window_minutes: float = ACTIVE_WINDOW_MINUTES):
    # Seeded from ALL current titles (including ones about to be renamed away), so a
    # new candidate may get a suffix even though a soon-to-be-renamed title will free
    # up — intentional: global uniqueness, no ordering dependence (spec-compliant).
    taken = {t["title"] for t in reg["threads"] if t.get("title")}
    out = []
    for t in sorted(reg["threads"], key=lambda t: t["mtime"]):
        kind = candidate_kind(t, now_iso, active_window_minutes)
        if kind is None:
            continue
        final = unique_name(t["suggestedName"], t["sessionId"], taken)
        taken.add(final)
        out.append((t, final, kind))
    return out
