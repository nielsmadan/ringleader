from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import namefilter, overview
from .slug import is_beneath
from .sources.base import NormalizedSession, RawRef
from .stateio import atomic_write_text, registry_lock

if TYPE_CHECKING:
    from .memory import MemoryEntry

REGISTRY_VERSION = 2

# maps registry key -> NormalizedSession field name
_METADATA_FIELDS = {
    "source": "source",
    "sourceRef": "source_ref",
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
    "gitCloneUrl": "git_clone_url",
}


def empty_registry() -> dict[str, Any]:
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


def load_registry(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return Registry(empty_registry(), path=p)
    data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    data.setdefault("version", REGISTRY_VERSION)
    data.setdefault("lastScan", None)
    data.setdefault("lastRoundupAt", None)
    data.setdefault("threads", [])
    data.setdefault("archived", [])
    data.setdefault("deleted", [])
    data.setdefault("projectsCleanedAt", {})
    data.setdefault("memoryEntries", [])
    data.setdefault("memoryProjectsCleanedAt", {})
    current_ids = {identity(row) for bucket in ("threads", "archived") for row in data[bucket]}
    data["deleted"] = [row for row in data["deleted"] if identity(row) not in current_ids]
    return Registry(data, path=p)


class RegistryConflict(RuntimeError):
    pass


class AmbiguousSession(ValueError):
    pass


class Registry(dict[str, Any]):
    def __init__(self, data: dict[str, Any], *, path: Path | None = None) -> None:
        super().__init__(data)
        self.path = path if path is not None else getattr(data, "path", None)
        self.baseline = deepcopy(dict(data))
        self.scanned = False

    def replace(self, current: dict[str, Any]) -> None:
        self.clear()
        self.update(current)
        self.baseline = deepcopy(dict(current))
        self.scanned = False


def _write_registry(path: Path, reg: dict[str, Any]) -> None:
    atomic_write_text(path.resolve(), json.dumps(reg, indent=2, ensure_ascii=False))


def save_registry(path: str | Path, reg: dict[str, Any]) -> None:
    if isinstance(reg, Registry):
        commit_registry(path, reg)
        return
    with registry_lock(Path(path)):
        _write_registry(Path(path), reg)


def mutate_registry(path: str | Path, mutation: Callable[[dict[str, Any]], object]) -> Registry:
    with registry_lock(Path(path)):
        current = load_registry(path)
        mutation(current)
        _write_registry(Path(path), current)
        return Registry(current)


_MISSING = object()
_SESSION_BUCKETS = ("threads", "archived", "deleted")


def _merge_fields(
    before: dict[str, Any], after: dict[str, Any], current: dict[str, Any], *, scan: bool = False
) -> dict[str, Any]:
    result = deepcopy(current)
    for field in before.keys() | after.keys():
        old, new, latest = (item.get(field, _MISSING) for item in (before, after, current))
        if new in (old, latest):
            continue
        if all(isinstance(value, dict) for value in (old, new, latest)):
            result[field] = _merge_fields(old, new, latest, scan=scan)
        elif latest != old:
            if scan:
                continue
            raise RegistryConflict(f"registry changed concurrently at {field}; retry the action")
        elif new is _MISSING:
            result.pop(field, None)
        else:
            result[field] = deepcopy(new)
    return result


def _session_rows(reg: dict[str, Any]) -> dict[tuple[str, str], tuple[str, dict[str, Any]]]:
    return {identity(row): (bucket, row) for bucket in _SESSION_BUCKETS for row in reg[bucket]}


def _scan_changes(
    before: dict[str, Any], after: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    changed = dict(after)
    protected: set[str] = set()
    if current.get("mtime") != before.get("mtime"):
        protected.update(_METADATA_FIELDS)
        protected.update(_GIT_PROJECT_FIELDS)
    if current.get("locationPinned"):
        protected.update(_GIT_PROJECT_FIELDS)
        protected.add("project")
    for field in protected:
        if field in before:
            changed[field] = before[field]
        else:
            changed.pop(field, None)
    return changed


def _merge_sessions(  # noqa: PLR0912
    before: dict[str, Any], after: dict[str, Any], current: dict[str, Any], *, scan: bool
) -> None:
    old_rows, new_rows, latest_rows = map(_session_rows, (before, after, current))
    buckets = {
        bucket: {identity(row): row for row in current[bucket]} for bucket in _SESSION_BUCKETS
    }
    for key in dict.fromkeys((*old_rows, *new_rows)):
        old, new, latest = (rows.get(key) for rows in (old_rows, new_rows, latest_rows))
        if new in (old, latest):
            continue
        if old is None:
            if latest is None:
                assert new is not None
                buckets[new[0]][key] = deepcopy(new[1])
            elif not scan:
                raise RegistryConflict(f"session already exists: {key}")
            continue
        if latest is None:
            if not scan:
                raise RegistryConflict(f"session disappeared concurrently: {key}")
            continue
        if (
            not scan
            and new is not None
            and old[0] == "threads"
            and new[0] in {"archived", "deleted"}
            and latest[0] == "deleted"
            and latest[1].get("reason") == "gc"
        ):
            del buckets["deleted"][key]
            latest = ("threads", latest[1].get("thread") or old[1])
            buckets["threads"][key] = latest[1]
        if new is None:
            if latest != old:
                raise RegistryConflict(f"session changed concurrently: {key}")
            del buckets[latest[0]][key]
        elif new[0] == old[0]:
            if latest[0] != old[0]:
                if not scan:
                    raise RegistryConflict(f"session lifecycle changed concurrently: {key}")
                continue
            changed = _scan_changes(old[1], new[1], latest[1]) if scan else new[1]
            merged = _merge_fields(old[1], changed, latest[1], scan=scan)
            buckets[latest[0]][key] = merged
        elif latest[0] == old[0]:
            if scan and latest != old:
                continue
            moved = deepcopy(new[1])
            if old[0] == "threads":
                embedded = _merge_fields(
                    old[1], new[1].get("thread") or old[1], latest[1], scan=scan
                )
                moved["thread"] = without_finish_mark(embedded)
                moved["harvestPath"] = embedded.get("harvestPath")
            elif new[0] == "threads" and isinstance(latest[1].get("thread"), dict):
                moved = _merge_fields(old[1].get("thread") or {}, moved, latest[1]["thread"])
            else:
                moved = _merge_fields(old[1], moved, latest[1])
            del buckets[latest[0]][key]
            buckets[new[0]][key] = moved
        elif not scan:
            raise RegistryConflict(f"session lifecycle changed concurrently: {key}")

    for bucket in _SESSION_BUCKETS:
        current[bucket] = list(buckets[bucket].values())


def _merge_memory(
    before: dict[str, Any], after: dict[str, Any], current: dict[str, Any], *, scan: bool
) -> None:
    old_rows, new_rows, latest_rows = (
        {row["path"]: row for row in reg["memoryEntries"]} for reg in (before, after, current)
    )
    for path in old_rows.keys() | new_rows.keys():
        old, new, latest = (rows.get(path) for rows in (old_rows, new_rows, latest_rows))
        if new in (old, latest):
            continue
        if old is None:
            if latest is None:
                latest_rows[path] = deepcopy(new)
            elif not scan:
                raise RegistryConflict(f"memory entry already exists: {path}")
        elif latest is None:
            if not scan:
                raise RegistryConflict(f"memory entry disappeared concurrently: {path}")
        elif new is None:
            if latest == old:
                latest_rows.pop(path)
            elif not scan:
                raise RegistryConflict(f"memory entry changed concurrently: {path}")
        elif not scan or latest.get("mtime") == old.get("mtime"):
            latest_rows[path] = _merge_fields(old, new, latest, scan=scan)
    current["memoryEntries"] = list(latest_rows.values())


def commit_registry(path: str | Path, reg: dict[str, Any]) -> None:
    if not isinstance(reg, Registry):
        raise TypeError("registry mutations require a loaded snapshot")
    with registry_lock(Path(path)):
        current = load_registry(path)
        _merge_sessions(reg.baseline, reg, current, scan=reg.scanned)
        _merge_memory(reg.baseline, reg, current, scan=reg.scanned)
        clocks = {"lastScan", "lastRoundupAt"}
        excluded = {*_SESSION_BUCKETS, "memoryEntries", *clocks}

        def other(data: dict[str, Any]) -> dict[str, Any]:
            return {key: value for key, value in data.items() if key not in excluded}

        current.update(_merge_fields(other(reg.baseline), other(reg), other(current)))
        for clock in clocks:
            if reg.get(clock) != reg.baseline.get(clock):
                current[clock] = max(
                    filter(None, (reg.get(clock), current.get(clock))), default=None
                )
        for key in other(reg.baseline).keys() - other(reg).keys():
            if current.get(key) == reg.baseline[key]:
                current.pop(key, None)
        _write_registry(Path(path), current)
    reg.replace(current)


def identity(record: dict[str, Any]) -> tuple[str, str]:
    embedded = record.get("thread") or {}
    return (record.get("source") or embedded.get("source") or "claude-code", record["sessionId"])


def qualified_id(record: dict[str, Any]) -> str:
    return ":".join(identity(record))


def find_session(
    reg: dict[str, Any], selector: str, *, bucket: str = "threads", source: str | None = None
) -> dict[str, Any] | None:
    if ":" in selector:
        selected_source, selector = selector.split(":", 1)
        selected_source = "claude-code" if selected_source == "claude" else selected_source
        if source is not None and source != selected_source:
            raise ValueError("conflicting session sources")
        source = selected_source
    matches = [
        record
        for record in reg.get(bucket, [])
        if record["sessionId"] == selector and (source is None or identity(record)[0] == source)
    ]
    all_matches = {
        identity(record)
        for name in _SESSION_BUCKETS
        for record in reg.get(name, [])
        if record["sessionId"] == selector and (source is None or identity(record)[0] == source)
    }
    if len(all_matches) > 1 or len(matches) > 1:
        raise AmbiguousSession(f"ambiguous session {selector}; qualify it as AGENT:{selector}")
    return matches[0] if matches else None


def session_identity(reg: dict[str, Any], selector: str) -> tuple[str, str]:
    for bucket in _SESSION_BUCKETS:
        record = find_session(reg, selector, bucket=bucket)
        if record is not None:
            return identity(record)
    if ":" in selector:
        source, native_id = selector.split(":", 1)
        return ("claude-code" if source == "claude" else source, native_id)
    return ("claude-code", selector)


def harvest_paths_by_identity(
    reg: dict[str, Any], paths_by_session: dict[str, list[str]]
) -> dict[tuple[str, str], list[str]]:
    result: dict[tuple[str, str], list[str]] = {}
    for selector, paths in paths_by_session.items():
        existing = result.setdefault(session_identity(reg, selector), [])
        existing.extend(path for path in paths if path not in existing)
    return result


def reconcile_harvest_paths(
    reg: dict[str, Any],
    paths_by_session: dict[str, list[str]],
    *,
    pending_root: str | Path | None = None,
    recorded_pending_paths: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    qualified_paths = harvest_paths_by_identity(reg, paths_by_session)
    root = (
        Path(os.path.normpath(Path(pending_root).absolute())) if pending_root is not None else None
    )
    recorded = {
        key: set(paths)
        for key, paths in harvest_paths_by_identity(reg, recorded_pending_paths or {}).items()
    }

    def is_pending_path(key: tuple[str, str], value: str) -> bool:
        if value in recorded.get(key, set()):
            return True
        candidate = Path(value)
        if root is None or not candidate.is_absolute():
            return False
        normalized = Path(os.path.normpath(candidate))
        return normalized.is_relative_to(root)

    def update(record: dict[str, Any]) -> None:
        key = identity(record)
        if key in qualified_paths:
            if root is None and not recorded_pending_paths:
                paths = qualified_paths[key]
            else:
                existing = record.get("harvestPath")
                existing_paths = [existing] if isinstance(existing, str) else existing or []
                direct_paths = [
                    path
                    for path in existing_paths
                    if isinstance(path, str) and not is_pending_path(key, path)
                ]
                paths = list(dict.fromkeys([*direct_paths, *qualified_paths[key]]))
            record["harvestPath"] = paths or None
        embedded = record.get("thread")
        if isinstance(embedded, dict):
            update(embedded)

    for bucket in ("threads", "archived", "deleted"):
        for record in reg.get(bucket, []):
            update(record)
    return reg


def _as_dict(meta: NormalizedSession | dict[str, Any]) -> dict[str, Any]:
    return asdict(meta) if isinstance(meta, NormalizedSession) else dict(meta)


def _new_thread(md: dict[str, Any]) -> dict[str, Any]:
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
            "filteredBy": None,
        }
    )
    return thread


def cached_session(
    thread: dict[str, Any] | None,
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
    return NormalizedSession(
        source=ref.source,
        session_id=thread["sessionId"],
        cwd=thread.get("project"),
        mtime=ref.mtime,
        size_bytes=ref.size_bytes,
        custom_title=thread.get("title"),
        summary=thread.get("summary"),
        last_prompt=thread.get("lastPrompt"),
        file=ref.file,
        ai_title=thread.get("aiTitle"),
        message_count=thread.get("messageCount") or 0,
        git_branch=thread.get("gitBranch"),
        git_project_key=thread.get("gitProjectKey"),
        git_project_label=thread.get("gitProjectLabel"),
        git_root=thread.get("gitRoot"),
        git_remote=thread.get("gitRemote"),
        git_clone_url=thread.get("gitCloneUrl"),
        cache_version=thread.get("cacheVersion"),
        file_fingerprint=thread.get("fileFingerprint"),
        source_ref=ref.identity,
    )


def _key(rec: dict[str, Any]) -> tuple[str, str]:
    return identity(rec)


def reconcile(  # noqa: PLR0912
    reg: dict[str, Any],
    scanned: Iterable[NormalizedSession],
    now_iso: str,
    *,
    unavailable_sources: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    scanned_dicts = [_as_dict(m) for m in scanned]
    by_id = {_key(t): t for t in reg["threads"]}
    for tombstone in reg.get("deleted", []):
        if tombstone.get("thread"):
            by_id.setdefault(_key(tombstone), tombstone["thread"])
    # an agent that writes after we archived its transcript recreates the file;
    # re-registering it would undo the archive
    archived_ids = {_key(x) for x in reg.get("archived", [])}
    seen = set()
    threads = []

    for md in scanned_dicts:
        k = (md["source"], md["session_id"])
        if k in archived_ids and by_id.get(k) is None:
            continue
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

    deleted = [row for row in reg.get("deleted", []) if _key(row) not in seen]
    archived = list(reg.get("archived", []))
    gone = {_key(x) for x in deleted} | {_key(x) for x in archived}
    for t in reg["threads"]:
        sid = t["sessionId"]
        key = _key(t)
        if key in seen or key in gone:
            continue
        if key[0] in unavailable_sources:
            threads.append(dict(t))
            continue
        deleted.append(
            {
                "sessionId": sid,
                "deletedAt": now_iso,
                "reason": "gc",
                "source": key[0],
                "thread": dict(t),
                "harvestPath": t.get("harvestPath"),
            }
        )

    result = deepcopy(reg)
    result.update(
        version=REGISTRY_VERSION,
        lastScan=now_iso,
        threads=threads,
        archived=archived,
        deleted=deleted,
    )
    if isinstance(result, Registry):
        result.scanned = True
    return result


def needs_classification(thread: dict[str, Any]) -> bool:
    return thread.get("status") == "unclassified" or thread.get("classifiedAtMtime") != thread.get(
        "mtime"
    )


def sessions_needing_classification(reg: dict[str, Any]) -> list[dict[str, Any]]:
    return [t for t in reg["threads"] if not t.get("filteredBy") and needs_classification(t)]


def apply_name_filters(reg: dict[str, Any], patterns: Iterable[str] | None) -> dict[str, Any]:
    """Stamp every live thread with the config pattern that matches its name, or None.
    Recomputed on each scan, so editing the filters takes effect without a re-parse."""
    patterns = list(patterns or ())
    for t in reg["threads"]:
        t["filteredBy"] = namefilter.thread_match(t, patterns)
    return reg


def apply_classification(
    reg: dict[str, Any],
    session_id: str,
    *,
    status: str,
    what_it_is: str,
    suggested_name: str,
    confidence: float | None = None,
) -> dict[str, Any]:
    t = find_session(reg, session_id)
    if t is not None:
        t["status"] = status
        t["whatItIs"] = what_it_is
        t["suggestedName"] = suggested_name
        if confidence is not None:
            t["statusConfidence"] = confidence
        t["classifiedAtMtime"] = t["mtime"]
        return reg
    raise KeyError(session_id)


def apply_name(
    reg: dict[str, Any], session_id: str, name: str, *, source: str | None = None
) -> dict[str, Any]:
    t = find_session(reg, session_id, source=source)
    if t is not None:
        t["title"] = name
        t["appliedName"] = name
        t["namedAtMtime"] = t["mtime"]
        return reg
    raise KeyError(session_id)


def mark_harvested(
    reg: dict[str, Any], session_id: str, harvest_paths: list[str]
) -> dict[str, Any]:
    t = find_session(reg, session_id)
    if t is not None:
        t["decision"] = "harvested"
        t["harvestPath"] = harvest_paths or None
        return reg
    raise KeyError(session_id)


def append_harvest_path(reg: dict[str, Any], session_id: str, harvest_path: str) -> dict[str, Any]:
    t = find_session(reg, session_id)
    if t is not None:
        existing = t.get("harvestPath")
        paths = existing if isinstance(existing, list) else [existing] if existing else []
        t["decision"] = "harvested"
        t["harvestPath"] = [*paths, harvest_path]
        return reg
    raise KeyError(session_id)


def mark_reviewed(reg: dict[str, Any], session_id: str, mtime: str | None = None) -> dict[str, Any]:
    t = find_session(reg, session_id)
    if t is not None:
        # mtime override lets the caller record the live file mtime — e.g. after
        # naming appended a record and bumped it — so the marker matches what the
        # next scan will see (otherwise the session re-surfaces once).
        t["reviewedAtMtime"] = mtime if mtime is not None else t["mtime"]
        return reg
    raise KeyError(session_id)


def select_for_day(reg: dict[str, Any], session_id: str, day: str) -> dict[str, Any]:
    t = find_session(reg, session_id)
    if t is not None:
        t["selectedForDay"] = day
        return reg
    raise KeyError(session_id)


def clear_selected(reg: dict[str, Any], session_id: str) -> dict[str, Any]:
    t = find_session(reg, session_id)
    if t is not None:
        t["selectedForDay"] = None
        return reg
    raise KeyError(session_id)


def selected_today(reg: dict[str, Any], today: str) -> list[dict[str, Any]]:
    return [t for t in reg["threads"] if t.get("selectedForDay") == today]


def archive_session(
    reg: dict[str, Any],
    session_id: str,
    archive_path: str,
    archived_at: str,
    *,
    summary: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    threads = reg["threads"]
    thread = find_session(reg, session_id)
    key = identity(thread) if thread is not None else session_identity(reg, session_id)
    if thread is not None:
        threads.remove(thread)
    session_id = key[1]
    if thread is not None:
        thread = without_finish_mark(thread)
    harvest = thread.get("harvestPath") if thread else None
    archived = reg.setdefault("archived", [])
    if not any(identity(a) == key for a in archived):
        archived.append(
            {
                "sessionId": session_id,
                "source": key[0],
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


def mark_project_cleaned(reg: dict[str, Any], key: str, when: str) -> dict[str, Any]:
    reg.setdefault("projectsCleanedAt", {})[key] = when
    return reg


def snooze_thread(
    reg: dict[str, Any], session_id: str, when: str, until: str | None = None
) -> dict[str, Any]:
    """Set a session aside: still open/resumable, but hidden from the default
    rl overview until woken. `when` records when it was snoozed; `until`, if
    given, is an ISO timestamp after which the snooze auto-wakes."""
    t = find_session(reg, session_id)
    if t is not None:
        t["snoozedAt"] = when
        t["snoozedUntil"] = until
        return reg
    raise KeyError(session_id)


def wake_thread(reg: dict[str, Any], session_id: str) -> dict[str, Any]:
    t = find_session(reg, session_id)
    if t is not None:
        t["snoozedAt"] = None
        return reg
    raise KeyError(session_id)


def keep_thread(reg: dict[str, Any], session_id: str, when: str) -> str | None:
    """Mark a session as kept long-term: it stays live and resumable but leaves the
    triage inbox permanently, regardless of later activity. Returns the decision it
    replaced (None if there wasn't one) so the caller can report it."""
    t = find_session(reg, session_id)
    if t is not None:
        previous: str | None = t.get("decision")
        t["decision"] = "keep"
        t["keptAt"] = when
        return previous if previous != "keep" else None
    raise KeyError(session_id)


def mark_finish_on_exit(reg: dict[str, Any], session_id: str, when: str) -> dict[str, Any]:
    """Mark a session to be archived once its transcript goes quiet. The transcript
    cannot be moved while the agent is still writing to it, so `rl session sweep`
    does the archiving later; this only records the intent."""
    t = find_session(reg, session_id)
    if t is not None:
        t["finishOnExit"] = True
        t["finishedAt"] = when
        return t
    raise KeyError(session_id)


def without_finish_mark(thread: dict[str, Any]) -> dict[str, Any]:
    """A copy of `thread` with the finish mark dropped. The mark means "archive this
    once it goes quiet", so it must not survive the archive that consumed it — a
    restored thread carrying it is swept straight back out, live agent or not."""
    out = dict(thread)
    out.pop("finishOnExit", None)
    out.pop("finishedAt", None)
    return out


def unkeep_thread(reg: dict[str, Any], session_id: str) -> bool:
    """Undo `keep_thread`, returning the session to the triage inbox. Returns False if
    the session wasn't kept — a non-"keep" decision (e.g. "harvested") is left alone."""
    t = find_session(reg, session_id)
    if t is not None:
        if t.get("decision") != "keep":
            return False
        t["decision"] = None
        t["keptAt"] = None
        return True
    raise KeyError(session_id)


def restore_session(reg: dict[str, Any], session_id: str, file_path: str) -> dict[str, Any]:
    """Reverse an archive at the registry level: drop the archived record so the next scan
    re-discovers the transcript at `file_path`. Returns the archived record that was removed.

    The record holds only a summary of the thread, so the thread row is not rebuilt
    here — reconcile re-creates it in full from the restored transcript."""
    archived = reg.setdefault("archived", [])
    record = find_session(reg, session_id, bucket="archived")
    if record is None:
        raise KeyError(session_id)
    archived.remove(record)
    return record


def pin_thread_location(
    t: dict[str, Any], new_project: str, git: dict[str, Any] | None = None
) -> None:
    """Pin one thread dict to a new cwd/git identity. `locationPinned` is what tells
    reconcile to keep these instead of re-deriving them from the transcript's stale
    baked-in cwd — and it is permanent, since reconcile never refreshes a pinned
    thread's git fields again."""
    t["project"] = new_project
    t["locationPinned"] = True
    if git is None:
        # the new path has no resolvable git identity (a deleted subdir, say). Keep the
        # cached one, as reconcile does — pinning would otherwise make the loss permanent.
        return
    t["gitProjectKey"] = git.get("key")
    t["gitProjectLabel"] = git.get("label")
    t["gitRoot"] = git.get("root")
    t["gitRemote"] = git.get("remote")
    t["gitCloneUrl"] = git.get("clone_url")


def remap_cached_git_root(thread: dict[str, Any], old: str, new: str) -> None:
    """Carry a cached `gitRoot` across with the tree.

    `pin_thread_location` keeps the cached git fields when the destination has no
    resolvable git identity — a deleted worktree, say. That is right for the
    identity itself, but the cached root is a path, and left alone it names a
    location that no longer exists. Remapping keeps the identity and makes the
    path true.
    """
    root = thread.get("gitRoot")
    if isinstance(root, str) and (root == old or is_beneath(root, old)):
        thread["gitRoot"] = new + root[len(old) :]


def prune_session(
    reg: dict[str, Any], session_id: str, deleted_at: str, *, reason: str = "prune"
) -> dict[str, Any]:
    threads = reg["threads"]
    thread = find_session(reg, session_id)
    key = identity(thread) if thread is not None else session_identity(reg, session_id)
    session_id = key[1]
    harvest = thread.get("harvestPath") if thread is not None else None
    if thread is not None:
        threads.remove(thread)
    deleted = reg.setdefault("deleted", [])
    if not any(identity(d) == key for d in deleted):
        deleted.append(
            {
                "sessionId": session_id,
                "deletedAt": deleted_at,
                "reason": reason,
                "source": key[0],
                "thread": thread,
                "harvestPath": harvest,
            }
        )
    return reg


def purge_archived_session(reg: dict[str, Any], session_id: str, purged_at: str) -> dict[str, Any]:
    """Archive retention removed the archived copy: the transcript is now really gone,
    so the record moves out of `archived` and into `deleted`."""
    archived = reg.setdefault("archived", [])
    rec = find_session(reg, session_id, bucket="archived")
    if rec is None:
        return reg
    archived.remove(rec)
    rec["reason"] = "purged"
    rec["purgedAt"] = purged_at
    rec.setdefault("deletedAt", purged_at)
    reg.setdefault("deleted", []).append(rec)
    return reg


def _mem_entry_dict(m: MemoryEntry) -> dict[str, Any]:
    d = asdict(m)
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


def reconcile_memory(
    reg: dict[str, Any], scanned: Iterable[MemoryEntry], now_iso: str
) -> dict[str, Any]:
    by_path = {e["path"]: e for e in reg.get("memoryEntries", [])}
    entries = []
    for m in scanned:
        nd = _mem_entry_dict(m)
        old = by_path.get(nd["path"])
        if old is not None:
            nd = {**old, **nd}
            nd["reviewedAtMtime"] = old.get("reviewedAtMtime")
            nd["disposition"] = old.get("disposition")
        entries.append(nd)
    reg = deepcopy(reg)
    if isinstance(reg, Registry):
        reg.scanned = True
    reg["memoryEntries"] = entries  # gone entries simply drop (no tombstone)
    reg["lastScan"] = now_iso
    reg.setdefault("memoryProjectsCleanedAt", {})
    return reg


def memory_needing_review(reg: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for e in reg.get("memoryEntries", []):
        rev = e.get("reviewedAtMtime")
        if rev is None or overview.newer_than(e["mtime"], rev):
            out.append(e)
    return out


def mark_memory_reviewed(
    reg: dict[str, Any], path: str, mtime: str | None = None
) -> dict[str, Any]:
    for e in reg.get("memoryEntries", []):
        if e["path"] == path:
            e["reviewedAtMtime"] = mtime if mtime is not None else e["mtime"]
            return reg
    raise KeyError(path)


def mark_memory_project_cleaned(reg: dict[str, Any], key: str, when: str) -> dict[str, Any]:
    reg.setdefault("memoryProjectsCleanedAt", {})[key] = when
    return reg
