from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .memory_sources.base import digest, memory_id
from .memory_types import MemoryObservation, MemoryRecord, MemoryRef, MemoryScan


def normalize_entry(row: dict[str, Any]) -> dict[str, Any]:
    if (
        row.get("memoryId")
        or not row.get("path")
        or row.get("source", "claude-code") != "claude-code"
    ):
        return row
    path = Path(row["path"])
    if not path.is_absolute():
        return row
    store = str(path.parent)
    return {
        **row,
        "source": "claude-code",
        "storeId": store,
        "sourceRef": str(path),
        "memoryId": memory_id("claude-code", store, path.name),
    }


def rows_by_id(reg: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    rows = map(normalize_entry, reg.get("memoryEntries", ()))
    return {
        (row.get("source", "claude-code"), row.get("memoryId") or row["path"]): row for row in rows
    }


def store_key(source: str, store_id: str) -> str:
    return f"{source}:{store_id}"


def configured(reg: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    key = store_key(row.get("source", "claude-code"), row.get("storeId", ""))
    return reg.get("memoryStores", {}).get(key, {}).get("configured") is not False


def reviewed_ref(row: Mapping[str, Any]) -> MemoryRef:
    return MemoryRef(
        row["source"],
        row["memoryId"],
        row["storeId"],
        row["sourceRef"],
        row["fingerprint"],
        row["incarnation"],
    )


def native_ref(row: Mapping[str, Any]) -> MemoryRef:
    return replace(reviewed_ref(row), incarnation=row["nativeIncarnation"])


def bind_record(record: MemoryRecord, row: Mapping[str, Any]) -> MemoryRecord:
    if record.ref != native_ref(row):
        raise ValueError("native memory changed since its review")
    return replace(
        record,
        ref=reviewed_ref(row),
        project=row.get("project"),
        obsolete_signals=tuple(row.get("obsoleteSignals", ())),
    )


class UnknownMemoryEntry(ValueError):
    pass


def select_entry(reg: Mapping[str, Any], selector: str) -> dict[str, Any]:
    matches = [
        row
        for row in rows_by_id(reg).values()
        if selector
        and configured(reg, row)
        and (row.get("path") == selector or row.get("memoryId", "").startswith(selector))
    ]
    if not matches:
        raise UnknownMemoryEntry(f"Unknown memory entry: {selector}. Run `rl mem refresh`.")
    if len(matches) > 1:
        raise ValueError(f"Ambiguous memory entry: {selector}")
    return matches[0]


def decision_states(reg: Mapping[str, Any]) -> dict[tuple[str, str], list[tuple[str, str]]]:
    decisions: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for op, row in sorted(reg.get("memoryDecisions", {}).items()):
        key = row.get("source"), row.get("memoryId")
        if key[0] and key[1]:
            decisions.setdefault((key[0], key[1]), []).append((op, row.get("sourceState", "")))
    return decisions


def _review_incarnation(
    native: str, decisions: list[tuple[str, str]], absence: str | None, generation: int = 0
) -> str:
    if generation:
        native = digest(native, generation)
    completed = [item for item in decisions if item[1] in {"removed", "restored"}]
    if absence is not None:
        return digest(native, completed, absence)
    return digest(native, completed) if completed else native


def _metadata(
    record: MemoryObservation,
    decisions: list[tuple[str, str]],
    absence: str | None,
    generation: int,
) -> dict[str, Any]:
    ref = record.ref
    return {
        "source": ref.source,
        "memoryId": ref.memory_id,
        "storeId": ref.store_id,
        "sourceRef": ref.source_ref,
        "fingerprint": ref.fingerprint,
        "nativeIncarnation": ref.incarnation,
        "incarnation": _review_incarnation(ref.incarnation, decisions, absence, generation),
        "project": record.project,
        "scope": record.scope,
        "mtime": record.mtime,
        "name": record.name,
        "description": record.description,
        "memType": record.mem_type,
        "mutationBlock": record.mutation_block,
        **(
            {
                "path": ref.source_ref,
                "isInline": record.is_inline,
                "obsoleteSignals": list(record.obsolete_signals),
            }
            if ref.source == "claude-code"
            else {}
        ),
    }


def _missing_entries(
    rows: dict[tuple[str, str], dict[str, Any]],
    complete_stores: set[str],
    retained: set[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        key: row
        for key, row in rows.items()
        if key not in retained and store_key(key[0], row.get("storeId", "")) in complete_stores
    }


def _record_inventory(reg: dict[str, Any], scan: MemoryScan, started_at: str) -> set[str]:
    clocks = reg.setdefault("memoryScannedAt", {})
    stores = reg.setdefault("memoryStores", {})
    sources = reg.setdefault("memorySources", {})
    accepted = set()
    reported = {store_key(store.source, store.store_id) for store in scan.stores}
    for key, previous in list(stores.items()):
        source = previous["source"]
        if (
            source in scan.inventoried_sources
            and key not in reported
            and clocks.get(key, "") <= started_at
        ):
            stores[key] = previous | {"configured": False, "scannedAt": started_at}
            clocks[key] = started_at
    for store in scan.stores:
        key = store_key(store.source, store.store_id)
        if clocks.get(key, "") > started_at or (
            key not in stores and sources.get(store.source, {}).get("inventoryAt", "") > started_at
        ):
            continue
        clocks[key] = started_at
        accepted.add(key)
        stores[key] = {
            "source": store.source,
            "storeId": store.store_id,
            "state": store.state,
            "completeness": store.completeness,
            "schemaVersion": store.schema_version,
            "reason": store.reason,
            "scannedAt": started_at,
        }
    for status in scan.statuses:
        if sources.get(status.source, {}).get("scannedAt", "") <= started_at:
            sources[status.source] = {
                "source": status.source,
                "status": status.status,
                "reason": status.reason,
                "scannedAt": started_at,
                "inventoryAt": started_at
                if status.source in scan.inventoried_sources
                else sources.get(status.source, {}).get("inventoryAt", ""),
            }
    return accepted


def _project_fields(
    scan: MemoryScan, record: MemoryObservation, previous: Mapping[str, Any]
) -> dict[str, Any]:
    git = scan.project_identities.get(record.project) if record.project else None
    if git and git.checked_at:
        project = git.project
        return {
            "gitProjectKey": project.key if project else None,
            "gitProjectLabel": project.label if project else None,
            "gitRoot": project.root if project else None,
            "gitCheckedAt": git.checked_at,
        }
    if previous.get("project") != record.project:
        return dict.fromkeys(("gitProjectKey", "gitProjectLabel", "gitRoot", "gitCheckedAt"))
    return {}


def apply_scan(
    reg: dict[str, Any],
    scan: MemoryScan,
    started_at: str,
    *,
    protected: frozenset[tuple[str, str]] = frozenset(),
    baseline: Mapping[str, Any] | None = None,
) -> None:
    entries = rows_by_id(reg)
    states = decision_states(reg)
    accepted = _record_inventory(reg, scan, started_at)
    clocks = reg["memoryScannedAt"]
    observed = {(record.ref.source, record.ref.memory_id) for record in scan.records}
    complete = {
        store_key(store.source, store.store_id)
        for store in scan.stores
        if store.completeness == "complete"
    }
    absences = reg.get("memoryAbsences", {})
    retained = observed | protected
    missing = _missing_entries(entries, complete & accepted, retained)
    previously_observed = {
        **rows_by_id(baseline or {}),
        **{
            key: row
            for key, row in entries.items()
            if row.get("firstObservedAt", started_at) < started_at
        },
    }
    earlier_missing = _missing_entries(previously_observed, complete - accepted, retained)
    for identity, row in (missing | earlier_missing).items():
        key = store_key(*identity)
        if row.get("nativeIncarnation") and absences.get(key, "") < started_at:
            absences[key] = started_at
            current = entries.get(identity)
            if identity in earlier_missing and current and current.get("nativeIncarnation"):
                entries[identity] = {
                    **current,
                    "incarnation": _review_incarnation(
                        current["nativeIncarnation"],
                        states.get(identity, []),
                        started_at,
                        current.get("revisionGeneration", 0),
                    ),
                }
    entries = {key: row for key, row in entries.items() if key not in missing}
    if absences:
        reg["memoryAbsences"] = absences
    for record in scan.records:
        ref = record.ref
        identity = ref.source, ref.memory_id
        if identity in protected:
            continue
        old = entries.get(identity, {})
        key = store_key(ref.source, ref.store_id)
        absence = absences.get(store_key(*identity))
        generation = old.get("revisionGeneration", 0)
        transition = old.get("revisionStartedAt") or old.get("firstObservedAt")
        changed = bool(old.get("fingerprint")) and (
            old["fingerprint"],
            old.get("nativeIncarnation"),
        ) != (ref.fingerprint, ref.incarnation)
        if key not in accepted:
            if (
                changed
                and key in clocks
                and transition
                and transition < started_at
                and configured(reg, old)
            ):
                entries[identity] = old | {
                    "revisionGeneration": generation + 2,
                    "revisionStartedAt": clocks[key],
                    "incarnation": _review_incarnation(
                        old["nativeIncarnation"], states.get(identity, []), absence, generation + 2
                    ),
                }
            continue
        if changed:
            generation += 1
            transition = started_at
        entries[identity] = {
            **old,
            **_project_fields(scan, record, old),
            **_metadata(record, states.get(identity, []), absence, generation),
            "revisionGeneration": generation,
            "revisionStartedAt": transition or started_at,
            "firstObservedAt": old.get("firstObservedAt") or started_at,
        }
    reg["memoryEntries"] = list(entries.values())
