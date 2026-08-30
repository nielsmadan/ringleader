from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from . import overview


def matches(thread: dict, selector: str) -> bool:
    sel = selector.strip().lower()
    # equality against label / key / root / cwd
    for key in ("gitProjectLabel", "gitProjectKey", "gitRoot", "project"):
        val = thread.get(key)
        if val and val.lower() == sel:
            return True
    # path-prefix against root / cwd (directory-boundary-aware)
    for key in ("gitRoot", "project"):
        val = thread.get(key)
        if val and val.lower().startswith(sel.rstrip("/") + "/"):
            return True
    return False


def filter_by_projects(threads, selectors) -> list[dict]:
    if not selectors:
        return list(threads)
    return [t for t in threads if any(matches(t, s) for s in selectors)]


@dataclass
class ProjectStat:
    key: str
    label: str
    is_git: bool
    total: int
    classified: int
    unnamed: int
    checkouts: list[str]  # distinct cwds this repo was worked from, sorted
    cleaned_at: str | None  # ISO of the last session-triage run, or None
    needs_attention: bool  # the project has sessions represented by the default total
    snoozed: int  # snoozed sessions in this group (0 unless include_snoozed)
    mem_entries: int  # memory entries currently tracked for this project
    mem_cleaned_at: str | None  # ISO of the last memory-triage run, or None


def project_summary(
    reg: dict,
    now_iso: str | None = None,
    include_snoozed: bool = False,
    triage_counts: bool = False,
) -> list[ProjectStat]:
    if now_iso is None:
        now_iso = datetime.now(UTC).isoformat()
    cleaned = reg.get("projectsCleanedAt", {})
    mem_cleaned = reg.get("memoryProjectsCleanedAt", {})
    mem_counts: dict[str, int] = {}
    for e in reg.get("memoryEntries", []):
        k = overview.group_key(e)
        mem_counts[k] = mem_counts.get(k, 0) + 1
    groups: dict[str, dict] = {}
    for t in reg["threads"]:
        snoozed = overview.snooze_active(t, now_iso)
        if snoozed and not include_snoozed:
            continue  # hidden from the default view
        key = overview.group_key(t)
        g = groups.get(key)
        if g is None:
            g = {
                "label": overview.group_label(t),
                "is_git": bool(t.get("gitProjectKey")),
                "total": 0,
                "classified": 0,
                "unnamed": 0,
                "checkouts": set(),
                "cleaned_at": cleaned.get(key),
                "fresh_since_clean": False,
                "snoozed": 0,
            }
            groups[key] = g
        if snoozed:
            g["snoozed"] += 1
        if t.get("project"):
            g["checkouts"].add(t["project"])
        if not triage_counts or overview.in_triage_inbox(t, now_iso):
            g["total"] += 1
            if t.get("status", "unclassified") != "unclassified":
                g["classified"] += 1
                if not t.get("title"):
                    g["unnamed"] += 1
        ca = g["cleaned_at"]
        # snoozed and kept sessions are deliberately set aside — don't let them raise the flag
        if (
            not snoozed
            and not overview.is_kept(t)
            and ca
            and t.get("mtime")
            and overview.newer_than(t["mtime"], ca)
        ):
            g["fresh_since_clean"] = True

    stats = [
        ProjectStat(
            key=k,
            label=g["label"],
            is_git=g["is_git"],
            total=g["total"],
            classified=g["classified"],
            unnamed=g["unnamed"],
            checkouts=sorted(g["checkouts"]),
            cleaned_at=g["cleaned_at"],
            needs_attention=(
                g["total"] > 0
                if triage_counts
                else g["cleaned_at"] is None or g["fresh_since_clean"]
            ),
            snoozed=g["snoozed"],
            mem_entries=mem_counts.get(k, 0),
            mem_cleaned_at=mem_cleaned.get(k),
        )
        for k, g in groups.items()
    ]
    # needs-attention first, then git groups, then most sessions, then label
    stats.sort(key=lambda s: (not s.needs_attention, not s.is_git, -s.total, s.label.lower()))
    return stats
