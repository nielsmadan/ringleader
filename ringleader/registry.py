from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

from . import overview

REGISTRY_VERSION = 2

# maps registry key -> SessionMeta field name
_METADATA_FIELDS = {
    "file": "file",
    "project": "project",
    "title": "title",
    "aiTitle": "ai_title",
    "summary": "summary",
    "lastPrompt": "last_prompt",
    "mtime": "mtime",
    "sizeBytes": "size_bytes",
    "messageCount": "message_count",
    "gitBranch": "git_branch",        # from transcript; always available, refresh freely
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
    return {"version": REGISTRY_VERSION, "lastScan": None,
            "threads": [], "tombstones": [], "projectsCleanedAt": {},
            "memoryEntries": [], "memoryProjectsCleanedAt": {}}


def load_registry(path) -> dict:
    p = Path(path)
    if not p.exists():
        return empty_registry()
    data = json.loads(p.read_text(encoding="utf-8"))
    data.setdefault("version", REGISTRY_VERSION)
    data.setdefault("lastScan", None)
    data.setdefault("threads", [])
    data.setdefault("tombstones", [])
    data.setdefault("projectsCleanedAt", {})
    data.setdefault("memoryEntries", [])
    data.setdefault("memoryProjectsCleanedAt", {})
    return data


def save_registry(path, reg) -> None:
    Path(path).write_text(
        json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")


def _as_dict(meta) -> dict:
    return asdict(meta) if is_dataclass(meta) else dict(meta)


def _new_thread(md: dict) -> dict:
    return {
        "sessionId": md["session_id"],
        "file": md["file"],
        "project": md["project"],
        "title": md["title"],
        "aiTitle": md["ai_title"],
        "summary": md["summary"],
        "lastPrompt": md["last_prompt"],
        "mtime": md["mtime"],
        "sizeBytes": md["size_bytes"],
        "messageCount": md["message_count"],
        "gitBranch": md["git_branch"],
        "gitProjectKey": md["git_project_key"],
        "gitProjectLabel": md["git_project_label"],
        "gitRoot": md["git_root"],
        "gitRemote": md["git_remote"],
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
    }


def reconcile(reg: dict, scanned, now_iso: str) -> dict:
    scanned_dicts = [_as_dict(m) for m in scanned]
    by_id = {t["sessionId"]: t for t in reg["threads"]}
    seen = set()
    threads = []

    for md in scanned_dicts:
        sid = md["session_id"]
        seen.add(sid)
        existing = by_id.get(sid)
        if existing is not None:
            existing = dict(by_id[sid])         # copy-on-write: never mutate the input
            pinned = existing.get("locationPinned")
            for rk, mk in _METADATA_FIELDS.items():
                if pinned and rk == "project":  # relocated: keep the pinned cwd
                    continue
                existing[rk] = md[mk]
            if not pinned:                      # relocated: keep the pinned git identity
                for rk, mk in _GIT_PROJECT_FIELDS.items():
                    if md[mk]:                  # else keep cached value when cwd is gone
                        existing[rk] = md[mk]
            threads.append(existing)
        else:
            threads.append(_new_thread(md))

    tombstones = list(reg.get("tombstones", []))
    tombstoned = {ts["sessionId"] for ts in tombstones}
    for t in reg["threads"]:
        sid = t["sessionId"]
        if sid not in seen and sid not in tombstoned:
            tombstones.append({
                "sessionId": sid,
                "deletedAt": now_iso,
                "reason": "gc",
                "harvestPath": t.get("harvestPath"),
            })

    return {"version": REGISTRY_VERSION, "lastScan": now_iso,
            "threads": threads, "tombstones": tombstones,
            "projectsCleanedAt": dict(reg.get("projectsCleanedAt", {})),
            "memoryEntries": list(reg.get("memoryEntries", [])),
            "memoryProjectsCleanedAt": dict(reg.get("memoryProjectsCleanedAt", {}))}


def needs_classification(thread: dict) -> bool:
    return (thread.get("status") == "unclassified"
            or thread.get("classifiedAtMtime") != thread.get("mtime"))


def sessions_needing_classification(reg: dict) -> list[dict]:
    return [t for t in reg["threads"] if needs_classification(t)]


def apply_classification(reg: dict, session_id: str, *, status: str,
                         what_it_is: str, suggested_name: str,
                         confidence: float | None = None) -> dict:
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


def apply_name(reg: dict, session_id: str, name: str) -> dict:
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
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


def archive_session(reg: dict, session_id: str, archive_path: str,
                    archived_at: str, *, summary: str | None = None,
                    project: str | None = None) -> dict:
    threads = reg["threads"]
    idx = next((i for i, t in enumerate(threads)
                if t["sessionId"] == session_id), None)
    harvest = threads[idx].get("harvestPath") if idx is not None else None
    if idx is not None:
        threads.pop(idx)
    tombstones = reg.setdefault("tombstones", [])
    if not any(ts["sessionId"] == session_id for ts in tombstones):
        tombstones.append({"sessionId": session_id, "deletedAt": archived_at,
                           "reason": "archived", "archivePath": archive_path,
                           "harvestPath": harvest, "summary": summary,
                           "project": project})
    return reg


def mark_project_cleaned(reg: dict, key: str, when: str) -> dict:
    reg.setdefault("projectsCleanedAt", {})[key] = when
    return reg


def snooze_thread(reg: dict, session_id: str, when: str) -> dict:
    """Set a session aside: still open/resumable, but hidden from the default
    rl overview until woken. `when` records when it was snoozed."""
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
            t["snoozedAt"] = when
            return reg
    raise KeyError(session_id)


def wake_thread(reg: dict, session_id: str) -> dict:
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
            t["snoozedAt"] = None
            return reg
    raise KeyError(session_id)


def relocate_thread(reg: dict, session_id: str, new_project: str,
                    git: dict | None = None) -> dict:
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


def prune_session(reg: dict, session_id: str, deleted_at: str, *,
                  reason: str = "prune") -> dict:
    threads = reg["threads"]
    idx = next((i for i, t in enumerate(threads)
                if t["sessionId"] == session_id), None)
    harvest = threads[idx].get("harvestPath") if idx is not None else None
    if idx is not None:
        threads.pop(idx)
    tombstones = reg.setdefault("tombstones", [])
    if not any(ts["sessionId"] == session_id for ts in tombstones):
        tombstones.append({"sessionId": session_id, "deletedAt": deleted_at,
                           "reason": reason, "harvestPath": harvest})
    return reg


def _mem_entry_dict(m) -> dict:
    d = asdict(m) if is_dataclass(m) else dict(m)
    return {
        "path": d["path"], "project": d.get("project"), "mtime": d["mtime"],
        "gitProjectKey": d.get("git_project_key"),
        "gitProjectLabel": d.get("git_project_label"),
        "gitRoot": d.get("git_root"), "gitRemote": d.get("git_remote"),
        "name": d.get("name"), "description": d.get("description"),
        "memType": d.get("mem_type"), "isInline": d.get("is_inline", False),
        "obsoleteSignals": list(d.get("obsolete_signals") or []),
        "reviewedAtMtime": None, "disposition": None,
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
    reg["memoryEntries"] = entries          # gone entries simply drop (no tombstone)
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
