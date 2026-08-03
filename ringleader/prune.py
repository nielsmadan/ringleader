from __future__ import annotations

from . import overview

DEFAULT_OLDER_THAN_DAYS = 7
DEFAULT_MAX_TURNS = 12


def prune_candidates(
    reg: dict,
    now_iso: str,
    *,
    older_than_days: int = DEFAULT_OLDER_THAN_DAYS,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> list[dict]:
    out = []
    for t in reg["threads"]:
        if t.get("decision") in ("keep", "harvested"):
            continue
        if overview.age_days(t["mtime"], now_iso) < older_than_days:
            continue
        # Tininess is message-count-driven, not byte-driven: sources differ wildly in
        # bytes-per-message (Codex transcripts embed full request/response payloads),
        # so byte size stays displayed but no longer gates eligibility.
        small = (t.get("messageCount") or 0) < max_turns
        if small:
            out.append(t)
    return out
