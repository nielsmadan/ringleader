from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from . import overview

if TYPE_CHECKING:
    from .search import Match


# search reports which field matched; the JSON contract is snake_case throughout
_MATCH_FIELDS = {"aiTitle": "ai_title", "whatItIs": "what_it_is", "lastPrompt": "last_prompt"}


def _session_json(t: dict[str, Any], now_iso: str) -> dict[str, Any]:
    mtime = t.get("mtime")
    return {
        "session_id": t.get("sessionId"),
        "project": t.get("project"),
        "status": overview.effective_status(t, now_iso),
        "name": t.get("title") or t.get("suggestedName") or (t.get("sessionId") or "")[:8],
        "what_it_is": t.get("whatItIs"),
        "mtime": mtime,
        "age_days": round(overview.age_days(mtime, now_iso), 1) if mtime else None,
        "size_bytes": t.get("sizeBytes"),
        "kept": overview.is_kept(t),
    }


def sessions_json(threads: Iterable[dict[str, Any]], now_iso: str) -> list[dict[str, Any]]:
    return [_session_json(t, now_iso) for t in threads]


def snoozed_json(threads: Iterable[dict[str, Any]], now_iso: str) -> list[dict[str, Any]]:
    out = []
    for t in threads:
        if overview.snooze_active(t, now_iso):
            row = _session_json(t, now_iso)
            row["snoozed_until"] = t.get("snoozedUntil")
            out.append(row)
    return out


def kept_json(threads: Iterable[dict[str, Any]], now_iso: str) -> list[dict[str, Any]]:
    out = []
    for t in threads:
        if overview.is_kept(t):
            row = _session_json(t, now_iso)
            row["kept_at"] = t.get("keptAt")
            out.append(row)
    return out


def filtered_json(threads: Iterable[dict[str, Any]], now_iso: str) -> list[dict[str, Any]]:
    out = []
    for t in threads:
        if overview.is_filtered(t):
            row = _session_json(t, now_iso)
            row["filtered_by"] = t.get("filteredBy")
            out.append(row)
    return out


def triage_json(
    threads: list[dict[str, Any]], now_iso: str, *, unclassified_only: bool = False
) -> list[dict[str, Any]]:
    """JSON-safe view of the triage inbox — same sessions `render_triage` shows.

    `unclassified_only` mirrors `render_triage`'s kwarg of the same name: it filters
    on the thread's raw `status` field (not the computed `status` in the output dict),
    exactly like the text path does.
    """
    inbox = overview.triage_inbox(threads, now_iso)
    if unclassified_only:
        inbox = [t for t in inbox if t.get("status", "unclassified") == "unclassified"]
    return [_session_json(t, now_iso) for t in inbox]


def rollup_json(
    reg: dict[str, Any],
    now_iso: str,
    *,
    selection: overview.StatusSelection = overview.DEFAULT_STATUS_SELECTION,
) -> dict[str, Any]:
    scoped = dict(reg)
    scoped["threads"] = overview.filter_by_status(reg["threads"], now_iso, selection)
    m = overview.compute_metrics(
        scoped,
        now_iso,
        include_snoozed=selection.mode is not overview.StatusMode.DEFAULT,
    )
    return {
        "live_count": m["live_count"],
        "spread_projects": m["spread_projects"],
        "by_project": m["by_project"],
        "by_status": m["by_status"],
        "unclassified": m["unclassified"],
        "blocked": m["blocked_count"],
        "stale": m["stale_count"],
        "oldest_age_days": m["oldest_age_days"],
        "kept": m["kept_count"],
    }


def search_json(matches: list[Match], now_iso: str, *, limit: int | None = None) -> dict[str, Any]:
    """Search hits as `_session_json` rows plus why each one matched. `total` is the
    whole result set, so a caller that passed `--limit` can tell 50-of-900 from 50."""
    shown = matches if limit is None else matches[:limit]
    return {
        "total": len(matches),
        "matches": [
            {
                **_session_json(m.thread, now_iso),
                "matched_on": _MATCH_FIELDS.get(m.matched_on, m.matched_on),
                "matched_text": m.value,
                "archived": m.archived,
                "filtered": m.filtered,
            }
            for m in shown
        ],
    }
