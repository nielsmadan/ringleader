from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.widgets.option_list import Option

from .. import memory_catalog, memory_scan, projects, registry
from ..commands.mem_output import terminal_text


def scan_catalog(
    path: Path,
    started: str,
    previous: dict[str, Any] | None = None,
    *,
    read_only: bool = False,
    sources: memory_scan.SourceResolver = memory_scan.configured_sources,
) -> dict[str, Any]:
    baseline = registry.load_registry(path)
    scan_basis = dict(baseline)
    if read_only and isinstance(previous, registry.Registry):
        registry.retain_native_memory_view(scan_basis, previous)
    observed = memory_scan.observe_catalog(scan_basis, sources(scan_basis), started)
    if not read_only:
        return registry.reconcile_native_memory(baseline, observed, started)
    current = registry.load_registry(path)
    if isinstance(previous, registry.Registry):
        registry.retain_native_memory_view(current, previous)
    registry.replay_native_memory_scan(current, scan_basis, observed, started)
    return current


def entry_key(row: dict[str, Any]) -> str:
    return str(row.get("memoryId") or row["path"])


def revision(row: dict[str, Any]) -> tuple[object, ...]:
    return tuple(row.get(key) for key in ("source", "memoryId", "fingerprint", "incarnation"))


def source_lines(reg: dict[str, Any]) -> list[str]:
    lines = [
        f"{source}: {row['status']}" + (f" · {row['reason']}" if row.get("reason") else "")
        for source, row in sorted(reg.get("memorySources", {}).items())
    ]
    return lines + [
        (
            f"{row['source']} · {row['state']} / {row['completeness']} · {row['storeId']}"
            + (f" · {row['schemaVersion']}" if row.get("schemaVersion") else "")
            + (f" · {row['reason']}" if row.get("reason") else "")
        )
        for row in reg.get("memoryStores", {}).values()
        if row.get("configured") is not False
    ]


@dataclass
class MemoryListing:
    rows: dict[str, dict[str, Any]]
    options: list[Option]
    undecided: int
    diagnostics: int


def memory_listing(reg: dict[str, Any], queued: set[str]) -> MemoryListing:
    decisions = {revision(d): d for d in reg.get("memoryDecisions", {}).values()}
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    rows = {entry_key(row): row for row in projects.memory_project_rows(reg)}
    labels: dict[tuple[str, str, str], str] = {}
    for row in rows.values():
        project = row.get("project")
        scope = row.get("scope") or "project"
        source = row.get("source", "claude-code")
        identity = "" if scope == "global" else row.get("gitProjectKey") or project or ""
        group = scope, identity, source
        labels[group] = (
            "Global"
            if scope == "global"
            else row.get("gitProjectLabel") or project or "Unresolved project"
        )
        groups[group].append(row)
    duplicates = Counter((label, group[2]) for group, label in labels.items())
    stores = reg.get("memoryStores", {})
    options = []
    undecided = 0
    for group in sorted(groups, key=lambda key: (labels[key], key)):
        _, identity, source = group
        members = groups[group]
        label = labels[group]
        if identity and duplicates[label, source] > 1:
            label = f"{label} ({identity})"
        count = sum(revision(row) not in decisions for row in members)
        undecided += count
        options.append(
            Option(
                Text(terminal_text(f"{label} · {source} · {count} to review"), style="bold"),
                disabled=True,
            )
        )
        for row in sorted(members, key=lambda r: str(r.get("name") or "")):
            store = stores.get(memory_catalog.store_key(source, row.get("storeId", "")), {})
            decision = decisions.get(revision(row), {})
            details = (
                row.get("name") or row.get("sourceRef") or row.get("path"),
                row.get("memType"),
                store.get("schemaVersion"),
                decision.get("sourceState"),
                row.get("mutationBlock"),
                "queued" if entry_key(row) in queued else None,
            )
            label = " · ".join(str(value) for value in details if value)
            options.append(Option(Text(terminal_text("  " + label)), id=entry_key(row)))
    issues = sum(
        s["state"] != "ready" or s["completeness"] != "complete"
        for s in stores.values()
        if s.get("configured") is not False
    )
    issues += sum(s["status"] != "ready" for s in reg.get("memorySources", {}).values())
    return MemoryListing(rows, options, undecided, issues)
