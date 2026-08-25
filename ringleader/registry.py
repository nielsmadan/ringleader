from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

from . import overview
from .sources.base import NormalizedSession, RawRef

REGISTRY_VERSION = 2

# maps registry key -> NormalizedSession field name
_METADATA_FIELDS = {
    "source": "source",
    "file": "file",
    "project": "cwd",
    "title": "custom_title",
    "aiTitle": "ai_title",
    "summary": "summary",
    "lastPrompt": "last_prompt",
    "mtime": "mtime",
    "sizeBytes": "size_bytes",
    "messageCount": "message_count",
    "gitBranch": "git_branch",  # from transcript; always available, refresh freely
    "cacheVersion": "cache_version",
    "fileFingerprint": "file_fingerprint",
}

# Git-project identity needs the live filesystem, so a vanished cwd resolves to None.
# Refresh only when the new scan actually resolved a value; otherwise keep the cached one.
_GIT_PROJECT_FIELDS = {
    "gitProjectKey": "git_project_key",
    "gitProjectLabel": "git_project_label",
    "gitRoot": "git_root",
    "gitRemote": "git_remote",
}


def empty_registry() -> dict:
    return {
        "version": REGISTRY_VERSION,
        "lastScan": None,
        "lastRoundupAt": None,
        "threads": [],
        "archived": [],
        "deleted": [],
        "projectsCleanedAt": {},
        "memoryEntries": [],
        "memoryProjectsCleanedAt": {},
    }


def load_registry(path) -> dict:
    p = Path(path)
    if not p.exists():
        return empty_registry()
    data = json.loads(p.read_text(encoding="utf-8"))
    data.setdefault("version", REGISTRY_VERSION)
    data.setdefault("lastScan", None)
    data.setdefault("lastRoundupAt", None)
    data.setdefault("threads", [])
    data.setdefault("archived", [])
    data.setdefault("deleted", [])
    data.setdefault("projectsCleanedAt", {})
    data.setdefault("memoryEntries", [])
    data.setdefault("memoryProjectsCleanedAt", {})
    return data


def save_registry(path, reg) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")


def _as_dict(meta) -> dict:
    return asdict(meta) if is_dataclass(meta) else dict(meta)


def _new_thread(md: dict) -> dict:
    thread = {rk: md.get(mk) for rk, mk in _METADATA_FIELDS.items()}
    thread.update({rk: md.get(mk) for rk, mk in _GIT_PROJECT_FIELDS.items()})
    thread.update(
        {
            "sessionId": md["session_id"],
            "status": "unclassified",
            "statusConfidence": 0.0,
            "whatItIs": None,
            "suggestedName": None,
            "appliedName": None,
            "namedAtMtime": None,
            "reviewedAtMtime": None,
            "classifiedAtMtime": None,
            "decision": None,
            "harvestPath": None,
            "todoId": None,
            "tags": [],
            "locationPinned": False,
            "snoozedAt": None,
            "keptAt": None,
            "selectedForDay": None,
        }
    )
    return thread


def cached_session(
    thread: dict | None,
    ref: RawRef,
    parser_version: int | None,
) -> NormalizedSession | None:
    if (
        thread is None
        or parser_version is None
        or ref.fingerprint is None
        or thread.get("cacheVersion") != parser_version
        or thread.get("fileFingerprint") != ref.fingerprint
    ):
        return None
    values = {mk: thread.get(rk) for rk, mk in _METADATA_FIELDS.items()}
    values.update({mk: thread.get(rk) for rk, mk in _GIT_PROJECT_FIELDS.items()})
    values.update(
        source=ref.source,
        session_id=thread["sessionId"],
        file=ref.file,
        mtime=ref.mtime,
        size_bytes=ref.size_bytes,
        message_count=thread.get("messageCount") or 0,
    )
    return NormalizedSession(**values)


def _key(rec: dict) -> tuple:
    return (rec.get("source") or "claude-code", rec["sessionId"])


def reconcile(reg: dict, scanned, now_iso: str) -> dict:
    scanned_dicts = [_as_dict(m) for m in scanned]
    by_id = {_key(t): t for t in reg["threads"]}
    seen = set()
    threads = []

    for md in scanned_dicts:
        k = (md["source"], md["session_id"])
        seen.add(k)
        existing = by_id.get(k)
        if existing is not None:
            existing = dict(by_id[k])  # copy-on-write: never mutate the input
            pinned = existing.get("locationPinned")
            for rk, mk in _METADATA_FIELDS.items():
                if pinned and rk == "project":  # relocated: keep the pinned cwd
                    continue
                existing[rk] = md.get(mk)
            if not pinned:  # relocated: keep the pinned git identity
                if md.get("git_resolved"):
                    for rk, mk in _GIT_PROJECT_FIELDS.items():
                        existing[rk] = md.get(mk)
                else:
                    for rk, mk in _GIT_PROJECT_FIELDS.items():
                        if md.get(mk):  # else keep cached value when cwd is gone
                            existing[rk] = md[mk]
            threads.append(existing)
        else:
            threads.append(_new_thread(md))

    deleted = list(reg.get("deleted", []))
    archived = list(reg.get("archived", []))
    gone = {x["sessionId"] for x in deleted} | {x["sessionId"] for x in archived}
    for t in reg["threads"]:
        sid = t["sessionId"]
        if _key(t) not in seen and sid not in gone:
            deleted.append(
                {
                    "sessionId": sid,
                    "deletedAt": now_iso,
                    "reason": "gc",
                    "harvestPath": t.get("harvestPath"),
                }
            )

    return {
        "version": REGISTRY_VERSION,
        "lastScan": now_iso,
        "lastRoundupAt": reg.get("lastRoundupAt"),
        "threads": threads,
        "archived": archived,
        "deleted": deleted,
        "projectsCleanedAt": dict(reg.get("projectsCleanedAt", {})),
        "memoryEntries": list(reg.get("memoryEntries", [])),
        "memoryProjectsCleanedAt": dict(reg.get("memoryProjectsCleanedAt", {})),
    }


def needs_classification(thread: dict) -> bool:
    return thread.get("status") == "unclassified" or thread.get("classifiedAtMtime") != thread.get(
        "mtime"
    )


def sessions_needing_classification(reg: dict) -> list[dict]:
    return [t for t in reg["threads"] if needs_classification(t)]


def apply_classification(
    reg: dict,
    session_id: str,
    *,
    status: str,
    what_it_is: str,
    suggested_name: str,
    confidence: float | None = None,
) -> dict:
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
            t["status"] = status
            t["whatItIs"] = what_it_is
            t["suggestedName"] = suggested_name
            if confidence is not None:
                t["statusConfidence"] = confidence
            t["classifiedAtMtime"] = t["mtime"]
            return reg
    raise KeyError(session_id)


def apply_name(reg: dict, session_id: str, name: str, *, source: str | None = None) -> dict:
    for t in reg["threads"]:
        if t["sessionId"] == session_id and (source is None or _key(t)[0] == source):
            t["title"] = name
            t["appliedName"] = name
            t["namedAtMtime"] = t["mtime"]
            return reg
    raise KeyError(session_id)


def mark_harvested(reg: dict, session_id: str, harvest_paths) -> dict:
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
            t["decision"] = "harvested"
            t["harvestPath"] = harvest_paths or None
            return reg
    raise KeyError(session_id)


def mark_reviewed(reg: dict, session_id: str, mtime: str | None = None) -> dict:
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
            # mtime override lets the caller record the live file mtime — e.g. after
            # naming appended a record and bumped it — so the marker matches what the
            # next scan will see (otherwise the session re-surfaces once).
            t["reviewedAtMtime"] = mtime if mtime is not None else t["mtime"]
            return reg
    raise KeyError(session_id)


def select_for_day(reg: dict, session_id: str, day: str) -> dict:
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
            t["selectedForDay"] = day
            return reg
    raise KeyError(session_id)


def clear_selected(reg: dict, session_id: str) -> dict:
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
            t["selectedForDay"] = None
            return reg
    raise KeyError(session_id)


def selected_today(reg: dict, today: str) -> list[dict]:
    return [t for t in reg["threads"] if t.get("selectedForDay") == today]


def archive_session(
    reg: dict,
    session_id: str,
    archive_path: str,
    archived_at: str,
    *,
    summary: str | None = None,
    project: str | None = None,
) -> dict:
    threads = reg["threads"]
    idx = next((i for i, t in enumerate(threads) if t["sessionId"] == session_id), None)
    thread = threads.pop(idx) if idx is not None else None
    harvest = thread.get("harvestPath") if thread else None
    archived = reg.setdefault("archived", [])
    if not any(a["sessionId"] == session_id for a in archived):
        archived.append(
            {
                "sessionId": session_id,
                "archivedAt": archived_at,
                "archivePath": archive_path,
                "harvestPath": harvest,
                "summary": summary,
                "project": project,
                # where the transcript lived before the move: restoring puts it back
                # here, and the agent's picker index still points at this path.
                "originalPath": thread.get("file") if thread else None,
                # the whole record, so a restore doesn't come back stripped of its
                # classification and need re-enriching.
                "thread": thread,
            }
        )
    return reg


def mark_project_cleaned(reg: dict, key: str, when: str) -> dict:
    reg.setdefault("projectsCleanedAt", {})[key] = when
    return reg


def snooze_thread(reg: dict, session_id: str, when: str, until: str | None = None) -> dict:
    """Set a session aside: still open/resumable, but hidden from the default
    rl overview until woken. `when` records when it was snoozed; `until`, if
    given, is an ISO timestamp after which the snooze auto-wakes."""
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
            t["snoozedAt"] = when
            t["snoozedUntil"] = until
            return reg
    raise KeyError(session_id)


def wake_thread(reg: dict, session_id: str) -> dict:
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
            t["snoozedAt"] = None
            return reg
    raise KeyError(session_id)


def keep_thread(reg: dict, session_id: str, when: str) -> str | None:
    """Mark a session as kept long-term: it stays live and resumable but leaves the
    triage inbox permanently, regardless of later activity. Returns the decision it
    replaced (None if there wasn't one) so the caller can report it."""
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
            previous = t.get("decision")
            t["decision"] = "keep"
            t["keptAt"] = when
            return previous if previous != "keep" else None
    raise KeyError(session_id)


def unkeep_thread(reg: dict, session_id: str) -> bool:
    """Undo `keep_thread`, returning the session to the triage inbox. Returns False if
    the session wasn't kept — a non-"keep" decision (e.g. "harvested") is left alone."""
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
            if t.get("decision") != "keep":
                return False
            t["decision"] = None
            t["keptAt"] = None
            return True
    raise KeyError(session_id)


def restore_session(reg: dict, session_id: str, file_path: str) -> dict:
    """Reverse an archive at the registry level: drop the archived record so the next scan
    re-discovers the transcript at `file_path`. Returns the archived record that was removed.

    The record holds only a summary of the thread, so the thread row is not rebuilt
    here — reconcile re-creates it in full from the restored transcript."""
    archived = reg.setdefault("archived", [])
    idx = next((i for i, a in enumerate(archived) if a["sessionId"] == session_id), None)
    if idx is None:
        raise KeyError(session_id)
    return archived.pop(idx)


def relocate_thread(reg: dict, session_id: str, new_project: str, git: dict | None = None) -> dict:
    """Pin a thread to a new cwd/git identity after its directory moved on disk.
    The transcript's baked-in cwd is stale, so `locationPinned` tells reconcile to
    keep these values instead of re-deriving them from the (old) transcript."""
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
            t["project"] = new_project
            t["locationPinned"] = True
            t["gitProjectKey"] = (git or {}).get("key")
            t["gitProjectLabel"] = (git or {}).get("label")
            t["gitRoot"] = (git or {}).get("root")
            t["gitRemote"] = (git or {}).get("remote")
            return reg
    raise KeyError(session_id)


def prune_session(reg: dict, session_id: str, deleted_at: str, *, reason: str = "prune") -> dict:
    threads = reg["threads"]
    idx = next((i for i, t in enumerate(threads) if t["sessionId"] == session_id), None)
    harvest = threads[idx].get("harvestPath") if idx is not None else None
    if idx is not None:
        threads.pop(idx)
    deleted = reg.setdefault("deleted", [])
    if not any(d["sessionId"] == session_id for d in deleted):
        deleted.append(
            {
                "sessionId": session_id,
                "deletedAt": deleted_at,
                "reason": reason,
                "harvestPath": harvest,
            }
        )
    return reg


def purge_archived_session(reg: dict, session_id: str, purged_at: str) -> dict:
    """Archive retention removed the archived copy: the transcript is now really gone,
    so the record moves out of `archived` and into `deleted`."""
    archived = reg.setdefault("archived", [])
    idx = next((i for i, a in enumerate(archived) if a["sessionId"] == session_id), None)
    if idx is None:
        return reg
    rec = archived.pop(idx)
    rec["reason"] = "purged"
    rec["purgedAt"] = purged_at
    rec.setdefault("deletedAt", purged_at)
    reg.setdefault("deleted", []).append(rec)
    return reg


def _mem_entry_dict(m) -> dict:
    d = asdict(m) if is_dataclass(m) else dict(m)
    return {
        "path": d["path"],
        "project": d.get("project"),
        "mtime": d["mtime"],
        "gitProjectKey": d.get("git_project_key"),
        "gitProjectLabel": d.get("git_project_label"),
        "gitRoot": d.get("git_root"),
        "gitRemote": d.get("git_remote"),
        "name": d.get("name"),
        "description": d.get("description"),
        "memType": d.get("mem_type"),
        "isInline": d.get("is_inline", False),
        "obsoleteSignals": list(d.get("obsolete_signals") or []),
        "reviewedAtMtime": None,
        "disposition": None,
    }


def reconcile_memory(reg: dict, scanned, now_iso: str) -> dict:
    by_path = {e["path"]: e for e in reg.get("memoryEntries", [])}
    entries = []
    for m in scanned:
        nd = _mem_entry_dict(m)
        old = by_path.get(nd["path"])
        if old is not None:
            nd["reviewedAtMtime"] = old.get("reviewedAtMtime")
            nd["disposition"] = old.get("disposition")
        entries.append(nd)
    reg = dict(reg)
    reg["memoryEntries"] = entries  # gone entries simply drop (no tombstone)
    reg["lastScan"] = now_iso
    reg.setdefault("memoryProjectsCleanedAt", {})
    return reg


def memory_needing_review(reg: dict) -> list[dict]:
    out = []
    for e in reg.get("memoryEntries", []):
        rev = e.get("reviewedAtMtime")
        if rev is None or overview.newer_than(e["mtime"], rev):
            out.append(e)
    return out


def mark_memory_reviewed(reg: dict, path: str, mtime: str | None = None) -> dict:
    for e in reg.get("memoryEntries", []):
        if e["path"] == path:
            e["reviewedAtMtime"] = mtime if mtime is not None else e["mtime"]
            return reg
    raise KeyError(path)


def mark_memory_project_cleaned(reg: dict, key: str, when: str) -> dict:
    reg.setdefault("memoryProjectsCleanedAt", {})[key] = when
    return reg
