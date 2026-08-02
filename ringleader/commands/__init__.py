from __future__ import annotations

from pathlib import Path


def default_registry() -> Path:
    """Interim default registry path — the repo-root registry.json, same as the old
    bin/rl-* scripts. Task 4 replaces this with paths.default_registry()."""
    return Path(__file__).resolve().parents[2] / "registry.json"


from . import (
    overview, triage, prune, export, archive, delete, mark_harvested,
    mark_reviewed, cleaned, relocate, snooze, wake, sync, name, tokens,
    mem_scan, mem_prune, mem_cleaned, mark_mem_reviewed,
)

TABLE = {
    "overview": overview.cmd_overview,
    "triage": triage.cmd_triage,
    "prune": prune.cmd_prune,
    "export": export.cmd_export,
    "archive": archive.cmd_archive,
    "delete": delete.cmd_delete,
    "mark-harvested": mark_harvested.cmd_mark_harvested,
    "mark-reviewed": mark_reviewed.cmd_mark_reviewed,
    "cleaned": cleaned.cmd_cleaned,
    "relocate": relocate.cmd_relocate,
    "snooze": snooze.cmd_snooze,
    "wake": wake.cmd_wake,
    "sync": sync.cmd_sync,
    "name": name.cmd_name,
    "tokens": tokens.cmd_tokens,
    "mem-scan": mem_scan.cmd_mem_scan,
    "mem-prune": mem_prune.cmd_mem_prune,
    "mem-cleaned": mem_cleaned.cmd_mem_cleaned,
    "mark-mem-reviewed": mark_mem_reviewed.cmd_mark_mem_reviewed,
}
