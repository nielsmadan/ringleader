from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import namefilter, overview
from .agents import canonical_agent, source_of
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
    "startedAt": "started_at",
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
    "gitCheckedAt": "git_checked_at",
}


def empty_registry() -> dict[str, Any]:
    return {
        "version": REGISTRY_VERSION,
        "lastScan": None,
        "sourcesScannedAt": {},
        "lastRoundupAt": None,
        "threads": [],
        "archived": [],
        "deleted": [],
        "projectsCleanedAt": {},
        "memoryEntries": [],
        "memoryProjectsCleanedAt": {},
    }


def _normalized(data: dict[str, Any]) -> dict[str, Any]:
    data.setdefault("version", REGISTRY_VERSION)
    data.setdefault("lastScan", None)
    data.setdefault("sourcesScannedAt", {})
    data.setdefault("lastRoundupAt", None)
    data.setdefault("threads", [])
    data.setdefault("archived", [])
    data.setdefault("deleted", [])
    data.setdefault("projectsCleanedAt", {})
    data.setdefault("memoryEntries", [])
    data.setdefault("memoryProjectsCleanedAt", {})
    current_ids = {identity(row) for bucket in ("threads", "archived") for row in data[bucket]}
    data["deleted"] = [row for row in data["deleted"] if identity(row) not in current_ids]
    return data


def load_registry(path: str | Path) -> dict[str, Any]:
    return _load(path)


def _load(path: str | Path) -> Registry:
    p = Path(path)
    if not p.exists():
        return Registry(empty_registry(), path=p)
    text = p.read_text(encoding="utf-8")
    return Registry(_normalized(json.loads(text)), path=p, baseline_text=text)


class RegistryConflict(RuntimeError):
    pass


class AmbiguousSession(ValueError):
    pass


class Registry(dict[str, Any]):
    def __init__(
        self,
        data: dict[str, Any],
        *,
        path: Path | None = None,
        baseline: dict[str, Any] | None = None,
        baseline_text: str | None = None,
    ) -> None:
        super().__init__(data)
        self.path = path if path is not None else getattr(data, "path", None)
        self._set_baseline(data, baseline, baseline_text)
        self.scanned = False

    def _set_baseline(
        self, data: dict[str, Any], baseline: dict[str, Any] | None, text: str | None
    ) -> None:
        self._baseline_text = text if baseline is None else None
        self._baseline = (
            baseline if baseline is not None or text is not None else deepcopy(dict(data))
        )

    @property
    def baseline(self) -> dict[str, Any]:
        if self._baseline is None:
            assert self._baseline_text is not None
            self._baseline = _normalized(json.loads(self._baseline_text))
        return self._baseline

    def replace(
        self,
        current: dict[str, Any],
        *,
        baseline: dict[str, Any] | None = None,
        baseline_text: str | None = None,
    ) -> None:
        self.clear()
        self.update(current)
        self._set_baseline(current, baseline, baseline_text)
        self.scanned = False

    def adopt(self, other: Registry) -> None:
        self.replace(other, baseline=other._baseline, baseline_text=other._baseline_text)


def _write_registry(path: Path, reg: dict[str, Any]) -> str:
    text = json.dumps(reg, indent=2, ensure_ascii=False)
    atomic_write_text(path.resolve(), text)
    return text


def save_registry(path: str | Path, reg: dict[str, Any]) -> None:
    if isinstance(reg, Registry):
        commit_registry(path, reg)
        return
    with registry_lock(Path(path)):
        _write_registry(Path(path), reg)


def mutate_registry(path: str | Path, mutation: Callable[[dict[str, Any]], object]) -> Registry:
    with registry_lock(Path(path)):
        current = _load(path)
        mutation(current)
        if Path(path).exists() and dict(current) == current.baseline:
            return Registry(dict(current), path=current.path, baseline=current.baseline)
        text = _write_registry(Path(path), current)
        return Registry(dict(current), path=current.path, baseline_text=text)


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


@dataclass(frozen=True)
class _SessionRow:
    bucket: str
    record: dict[str, Any]


def _session_rows(reg: dict[str, Any]) -> dict[tuple[str, str], _SessionRow]:
    return {
        identity(row): _SessionRow(bucket, row)
        for bucket in _SESSION_BUCKETS
        for row in reg[bucket]
    }


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


def _merge_lifecycle_move(
    old: _SessionRow, new: _SessionRow, latest: _SessionRow, *, scan: bool
) -> dict[str, Any]:
    moved = deepcopy(new.record)
    if old.bucket == "threads":
        embedded = _merge_fields(
            old.record, new.record.get("thread") or old.record, latest.record, scan=scan
        )
        moved["thread"] = without_finish_mark(embedded)
        moved["harvestPath"] = embedded.get("harvestPath")
        return moved
    if new.bucket == "threads" and isinstance(latest.record.get("thread"), dict):
        return _merge_fields(old.record.get("thread") or {}, moved, latest.record["thread"])
    return _merge_fields(old.record, moved, latest.record)


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
                buckets[new.bucket][key] = deepcopy(new.record)
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
            and old.bucket == "threads"
            and new.bucket in {"archived", "deleted"}
            and latest.bucket == "deleted"
            and latest.record.get("reason") == "gc"
        ):
            del buckets["deleted"][key]
            latest = _SessionRow("threads", latest.record.get("thread") or old.record)
            buckets["threads"][key] = latest.record
        if new is None:
            if latest != old:
                raise RegistryConflict(f"session changed concurrently: {key}")
            del buckets[latest.bucket][key]
        elif new.bucket == old.bucket:
            if latest.bucket != old.bucket:
                if not scan:
                    raise RegistryConflict(f"session lifecycle changed concurrently: {key}")
                continue
            changed = _scan_changes(old.record, new.record, latest.record) if scan else new.record
            merged = _merge_fields(old.record, changed, latest.record, scan=scan)
            buckets[latest.bucket][key] = merged
        elif latest.bucket == old.bucket:
            if scan and latest != old:
                continue
            moved = _merge_lifecycle_move(old, new, latest, scan=scan)
            del buckets[latest.bucket][key]
            buckets[new.bucket][key] = moved
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
        current = _load(path)
        _merge_sessions(reg.baseline, reg, current, scan=reg.scanned)
        _merge_memory(reg.baseline, reg, current, scan=reg.scanned)
        clocks = {"lastScan", "lastRoundupAt"}
        excluded = {*_SESSION_BUCKETS, "memoryEntries", "sourcesScannedAt", *clocks}

        def other(data: dict[str, Any]) -> dict[str, Any]:
            return {key: value for key, value in data.items() if key not in excluded}

        current.update(_merge_fields(other(reg.baseline), other(reg), other(current)))
        for clock in clocks:
            if reg.get(clock) != reg.baseline.get(clock):
                current[clock] = max(
                    filter(None, (reg.get(clock), current.get(clock))), default=None
                )
        current["sourcesScannedAt"] = _merge_scan_times(
            reg.baseline.get("sourcesScannedAt") or {},
            reg.get("sourcesScannedAt") or {},
            current.get("sourcesScannedAt") or {},
        )
        for key in other(reg.baseline).keys() - other(reg).keys():
            if current.get(key) == reg.baseline[key]:
                current.pop(key, None)
        if Path(path).exists() and dict(current) == current.baseline:
            reg.replace(current, baseline=current.baseline)
            return
        text = _write_registry(Path(path), current)
    reg.replace(current, baseline_text=text)


def _merge_scan_times(
    before: dict[str, str], after: dict[str, str], current: dict[str, str]
) -> dict[str, str]:
    changed = {name: at for name, at in after.items() if before.get(name) != at}
    return current | {name: max(at, current.get(name) or at) for name, at in changed.items()}


def identity(record: dict[str, Any]) -> tuple[str, str]:
    return source_of(record), record["sessionId"]


def qualified_id(record: dict[str, Any]) -> str:
    return ":".join(identity(record))


def _parse_selector(selector: str, source: str | None) -> tuple[str, str | None]:
    if ":" in selector:
        selected_source, selector = selector.split(":", 1)
        selected_source = canonical_agent(selected_source)
        if source is not None and source != selected_source:
            raise ValueError("conflicting session sources")
        source = selected_source
    return selector, source


def _pick(
    rows: Iterable[tuple[str, dict[str, Any]]], native: str, bucket: str, source: str | None
) -> dict[str, Any] | None:
    chosen = [
        (name, record)
        for name, record in rows
        if record["sessionId"] == native and (source is None or identity(record)[0] == source)
    ]
    matches = [record for name, record in chosen if name == bucket]
    if len({identity(record) for _, record in chosen}) > 1 or len(matches) > 1:
        raise AmbiguousSession(f"ambiguous session {native}; qualify it as AGENT:{native}")
    return matches[0] if matches else None


def _fallback_identity(selector: str) -> tuple[str, str]:
    if ":" in selector:
        source, native_id = selector.split(":", 1)
        return (canonical_agent(source), native_id)
    return ("claude-code", selector)


class SessionIndex:
    def __init__(self, reg: dict[str, Any], *, native: str | None = None) -> None:
        self._rows: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for bucket in _SESSION_BUCKETS:
            for record in reg.get(bucket, []):
                if native is None or record["sessionId"] == native:
                    self._add(bucket, record)

    def _add(self, bucket: str, record: dict[str, Any]) -> None:
        self._rows.setdefault(record["sessionId"], []).append((bucket, record))

    def _discard(self, bucket: str, record: dict[str, Any]) -> None:
        rows = self._rows.get(record["sessionId"], [])
        rows[:] = [(name, row) for name, row in rows if not (name == bucket and row is record)]

    def _remove(self, reg: dict[str, Any], bucket: str, record: dict[str, Any]) -> None:
        _drop(reg[bucket], record)
        self._discard(bucket, record)

    def _append(self, reg: dict[str, Any], bucket: str, record: dict[str, Any]) -> None:
        reg.setdefault(bucket, []).append(record)
        self._add(bucket, record)

    def has(self, key: tuple[str, str], bucket: str) -> bool:
        return any(
            name == bucket and identity(row) == key for name, row in self._rows.get(key[1], [])
        )

    def find(
        self, selector: str, *, bucket: str = "threads", source: str | None = None
    ) -> dict[str, Any] | None:
        native, source = _parse_selector(selector, source)
        return _pick(self._rows.get(native, []), native, bucket, source)

    def session_identity(self, selector: str) -> tuple[str, str]:
        for bucket in _SESSION_BUCKETS:
            record = self.find(selector, bucket=bucket)
            if record is not None:
                return identity(record)
        return _fallback_identity(selector)


def _index_for(reg: dict[str, Any], selector: str, index: SessionIndex | None) -> SessionIndex:
    if index is not None:
        return index
    return SessionIndex(reg, native=_parse_selector(selector, None)[0])


def find_session(
    reg: dict[str, Any], selector: str, *, bucket: str = "threads", source: str | None = None
) -> dict[str, Any] | None:
    return _index_for(reg, selector, None).find(selector, bucket=bucket, source=source)


def session_identity(reg: dict[str, Any], selector: str) -> tuple[str, str]:
    return SessionIndex(reg).session_identity(selector)


def _drop(rows: list[dict[str, Any]], record: dict[str, Any]) -> None:
    del rows[next(index for index, row in enumerate(rows) if row is record)]


def harvest_paths_by_identity(
    reg: dict[str, Any], paths_by_session: dict[str, list[str]]
) -> dict[tuple[str, str], list[str]]:
    index = SessionIndex(reg)
    result: dict[tuple[str, str], list[str]] = {}
    for selector, paths in paths_by_session.items():
        existing = result.setdefault(index.session_identity(selector), [])
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


def session_lifecycle(
    reg: dict[str, Any], session_id: str, *, source: str
) -> tuple[str, dict[str, Any] | None]:
    for bucket, lifecycle in (
        ("deleted", "deleted"),
        ("archived", "archived"),
        ("threads", "active"),
    ):
        record = find_session(reg, session_id, source=source, bucket=bucket)
        if record is not None:
            return lifecycle, record
    return "missing", None


def session_lineage(record: dict[str, Any]) -> dict[str, Any] | None:
    while record:
        if record.get("lineage"):
            lineage: dict[str, Any] = record["lineage"]
            return lineage
        record = record.get("thread") or {}
    return None


def conversion_lineage(
    reg: dict[str, Any], session_id: str, *, source: str
) -> tuple[dict[str, Any], ...]:
    entries = dict(reg.get("conversionLineage", {}))
    for bucket in _SESSION_BUCKETS:
        for record in reg[bucket]:
            item = session_lineage(record)
            if item:
                entries.setdefault(item["operationId"], item)
    qualified = {"agent": source, "nativeId": session_id}
    return tuple(item for item in entries.values() if qualified in (item["source"], item["target"]))


def register_conversion(
    reg: dict[str, Any],
    metadata: dict[str, Any],
    lineage: dict[str, Any],
    native_roots: dict[str, Any],
    *,
    create_active: bool = True,
) -> None:
    _, existing = session_lifecycle(reg, metadata["session_id"], source=metadata["source"])
    if existing is not None:
        existing_lineage = session_lineage(existing)
        if existing_lineage is not None and existing_lineage != lineage:
            raise ValueError("Registry lineage collision")
        while existing:
            existing["lineage"] = lineage
            existing.setdefault("nativeRoots", native_roots)
            existing = existing.get("thread")
    elif create_active and lineage["operationId"] not in reg.get("conversionLineage", {}):
        record = _new_thread(metadata)
        record["lineage"] = lineage
        record["nativeRoots"] = native_roots
        reg["threads"].append(record)
    reg.setdefault("conversionLineage", {})[lineage["operationId"]] = lineage


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
        started_at=thread.get("startedAt"),
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
    partial: bool = False,
    scanned_sources: Iterable[str] = (),
) -> dict[str, Any]:
    """Fold a scan into the registry. A `partial` scan looked at only part of each source,
    so a thread it did not see is kept rather than tombstoned and no clock moves. Otherwise
    each available source in `scanned_sources` gets `now_iso` in `sourcesScannedAt`, the
    baseline a later partial scan trusts, so `now_iso` must be read before discovery."""
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
        if partial or key[0] in unavailable_sources:
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
        lastScan=reg.get("lastScan") if partial else now_iso,
        sourcesScannedAt=(reg.get("sourcesScannedAt") or {})
        | (
            {}
            if partial
            else {name: now_iso for name in scanned_sources if name not in unavailable_sources}
        ),
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


def archive_session(  # noqa: PLR0913 -- the archived record's fields, plus the index
    reg: dict[str, Any],
    session_id: str,
    archive_path: str,
    archived_at: str,
    *,
    summary: str | None = None,
    project: str | None = None,
    index: SessionIndex | None = None,
) -> dict[str, Any]:
    index = _index_for(reg, session_id, index)
    thread = index.find(session_id)
    key = identity(thread) if thread is not None else index.session_identity(session_id)
    if thread is not None:
        index._remove(reg, "threads", thread)
    if index.has(key, "archived"):
        return reg
    finished = without_finish_mark(thread) if thread is not None else None
    index._append(
        reg,
        "archived",
        {
            "sessionId": key[1],
            "source": key[0],
            "archivedAt": archived_at,
            "archivePath": archive_path,
            "harvestPath": finished.get("harvestPath") if finished else None,
            "summary": summary,
            "project": project,
            # where the transcript lived before the move: restoring puts it back
            # here, and the agent's picker index still points at this path.
            "originalPath": finished.get("file") if finished else None,
            # the whole record, so a restore doesn't come back stripped of its
            # classification and need re-enriching.
            "thread": finished,
        },
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
    reg: dict[str, Any],
    session_id: str,
    deleted_at: str,
    *,
    reason: str = "prune",
    index: SessionIndex | None = None,
) -> dict[str, Any]:
    index = _index_for(reg, session_id, index)
    thread = index.find(session_id)
    key = identity(thread) if thread is not None else index.session_identity(session_id)
    if thread is not None:
        index._remove(reg, "threads", thread)
    if not index.has(key, "deleted"):
        tombstone = {
            "sessionId": key[1],
            "deletedAt": deleted_at,
            "reason": reason,
            "source": key[0],
            "thread": thread,
            "harvestPath": thread.get("harvestPath") if thread is not None else None,
        }
        index._append(reg, "deleted", tombstone)
    return reg


def purge_archived_session(
    reg: dict[str, Any], session_id: str, purged_at: str, *, index: SessionIndex | None = None
) -> dict[str, Any]:
    """Archive retention removed the archived copy: the transcript is now really gone,
    so the record moves out of `archived` and into `deleted`."""
    index = _index_for(reg, session_id, index)
    rec = index.find(session_id, bucket="archived")
    if rec is None:
        return reg
    index._remove(reg, "archived", rec)
    rec["reason"] = "purged"
    rec["purgedAt"] = purged_at
    rec.setdefault("deletedAt", purged_at)
    index._append(reg, "deleted", rec)
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
