from __future__ import annotations

from . import overview


def _session_json(t: dict, now_iso: str) -> dict:
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
    }


def triage_json(threads, now_iso: str, *, unclassified_only: bool = False) -> list[dict]:
    """JSON-safe view of the triage inbox — same sessions `render_triage` shows.

    `unclassified_only` mirrors `render_triage`'s kwarg of the same name: it filters
    on the thread's raw `status` field (not the computed `status` in the output dict),
    exactly like the text path does.
    """
    inbox = overview.triage_inbox(threads, now_iso)
    if unclassified_only:
        inbox = [t for t in inbox if t.get("status", "unclassified") == "unclassified"]
    return [_session_json(t, now_iso) for t in inbox]


def rollup_json(reg: dict, now_iso: str) -> dict:
    """JSON-safe view of the overview rollup metrics — same numbers `render`/
    `render_rollup` derive from `compute_metrics`."""
    m = overview.compute_metrics(reg, now_iso)
    return {
        "live_count": m["live_count"],
        "spread_projects": m["spread_projects"],
        "by_project": m["by_project"],
        "by_status": m["by_status"],
        "unclassified": m["unclassified"],
        "blocked": m["blocked_count"],
        "stale": m["stale_count"],
        "oldest_age_days": m["oldest_age_days"],
    }
