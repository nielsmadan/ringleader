from __future__ import annotations

from . import overview

DEFAULT_OLDER_THAN_DAYS = 7
DEFAULT_MAX_SIZE_BYTES = 50 * 1024
DEFAULT_MAX_TURNS = 12


def prune_candidates(
    reg: dict,
    now_iso: str,
    *,
    older_than_days: int = DEFAULT_OLDER_THAN_DAYS,
    max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> list[dict]:
    out = []
    for t in reg["threads"]:
        if t.get("decision") in ("keep", "harvested"):
            continue
        if overview.age_days(t["mtime"], now_iso) < older_than_days:
            continue
        small = (t.get("sizeBytes") or 0) < max_size_bytes or (
            t.get("messageCount") or 0
        ) < max_turns
        if small:
            out.append(t)
    return out
