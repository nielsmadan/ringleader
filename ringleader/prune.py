from __future__ import annotations

import shutil
import sys
from pathlib import Path

from . import overview, registry

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


def delete_sessions(reg: dict, candidates: list[dict], now_iso: str) -> int:
    deleted = skipped = 0
    for t in candidates:
        f = Path(t["file"])
        sidecar = f.with_suffix("")  # <uuid>.jsonl -> <uuid>/ dir
        try:
            f.unlink(missing_ok=True)
            shutil.rmtree(sidecar, ignore_errors=True)
        except OSError as e:
            print(f"skipped {t['sessionId']}: {e}", file=sys.stderr)
            skipped += 1
            continue
        registry.prune_session(reg, t["sessionId"], now_iso)
        deleted += 1
    return deleted
