from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import memory_catalog, memory_scan, overview, projects, registry
from ..agents import canonical_agent, source_of
from .mem_output import MemoryParser, terminal_text

_ENTRY_FIELDS = (
    "source",
    "memoryId",
    "storeId",
    "sourceRef",
    "fingerprint",
    "incarnation",
    "project",
    "scope",
    "name",
    "description",
    "memType",
    "mtime",
    "mutationBlock",
    "path",
    "isInline",
    "reviewedAtMtime",
    "obsoleteSignals",
)
_DECISION_FIELDS = (
    "decision",
    "sourceState",
    "cleanup",
    "harvestId",
    "harvestPath",
    "harvestToken",
    "destinationProject",
    "publicationReceiptId",
    "recoveryExpiresAt",
    "recoveryAvailable",
    "directPaths",
)


def catalog(path: Path, *, no_scan: bool, persist: bool) -> dict[str, Any]:
    reg = registry.load_registry(path)
    if no_scan:
        return reg
    started = datetime.now(UTC).isoformat()
    observed = memory_scan.observe_catalog(reg, memory_scan.configured_sources(reg), started)
    projected = registry.reconcile_native_memory(reg, observed, started)
    if not persist:
        return projected
    registry.commit_registry(path, projected)
    return registry.load_registry(path)


def _revision(row: dict[str, Any]) -> tuple[object, ...]:
    return tuple(row.get(key) for key in ("source", "memoryId", "fingerprint", "incarnation"))


def _record(
    row: dict[str, Any], decisions: dict[tuple[object, ...], dict[str, Any]]
) -> dict[str, Any]:
    return {
        **{key: row[key] for key in _ENTRY_FIELDS if key in row},
        "decision": decisions.get(_revision(row)),
    }


def _decisions(reg: dict[str, Any]) -> dict[tuple[object, ...], dict[str, Any]]:
    return {
        _revision(row): {
            "operationId": operation_id,
            **{key: row[key] for key in _DECISION_FIELDS if key in row},
        }
        for operation_id, row in reg.get("memoryDecisions", {}).items()
    }


def entry_metadata(row: dict[str, Any], reg: dict[str, Any]) -> dict[str, Any]:
    return _record(row, _decisions(reg))


def _filtered_rows(
    args: argparse.Namespace, reg: dict[str, Any], verb: str
) -> list[dict[str, Any]]:
    rows = projects.memory_project_rows(reg)
    selectors = args.project or []
    selector = getattr(args, "selector", None)
    if selector:
        entries = [
            row
            for row in rows
            if row.get("path") == selector or row.get("memoryId", "").startswith(selector)
        ]
        if entries and not any(projects.matches(row, selector) for row in rows):
            rows = entries
        else:
            selectors = [*selectors, selector]
    rows = projects.filter_by_projects(rows, selectors)
    if args.source:
        rows = [row for row in rows if source_of(row) in args.source]
    if verb == "search":
        query = args.query.casefold().strip()
        if not query:
            raise ValueError("Enter a search query")
        rows = [
            row
            for row in rows
            if any(
                query in str(row.get(key) or "").casefold()
                for key in ("memoryId", "name", "description", "sourceRef", "path", "project")
            )
        ]
    return sorted(
        rows,
        key=lambda row: (overview.group_label(row), source_of(row), row.get("name") or ""),
    )


def _emit_status(statuses: list[dict[str, Any]], stores: list[dict[str, Any]]) -> None:
    counts = Counter((store["source"], store["state"]) for store in stores)
    for status in statuses:
        source = status["source"]
        detail = ", ".join(
            f"{count} {state}" for (name, state), count in sorted(counts.items()) if name == source
        )
        suffix = status["reason"] or (f"stores: {detail}" if detail else None)
        note = f" ({suffix})" if suffix else ""
        print(terminal_text(f"{source}: {status['status']}{note}"), file=sys.stderr)
    for store in stores:
        if store["reason"]:
            print(
                terminal_text(f"{store['source']} {store['storeId']}: {store['reason']}"),
                file=sys.stderr,
            )


def _entry_line(record: dict[str, Any]) -> str:
    where = record.get("project") or (
        "global" if record.get("scope") == "global" else "unresolved project"
    )
    name = record.get("name") or record.get("path") or "(unnamed)"
    ref = record.get("memoryId") or record.get("path")
    decision = record.get("decision")
    state = f" [{decision['decision']}, {decision['sourceState']}]" if decision else ""
    kind = f" [{record['memType']}]" if record.get("memType") else ""
    path = f" ({record['path']})" if record.get("path") and name != record["path"] else ""
    block = f" — {record['mutationBlock']}" if record.get("mutationBlock") else ""
    return f"{ref}  {source_of(record)}  {name}{kind}{path}  {where}{state}{block}"


def _rollup(rows: list[dict[str, Any]], cleaned: dict[str, str]) -> str:
    groups = {projects.memory_group_key(row): projects.memory_group_label(row) for row in rows}
    counts = Counter(projects.memory_group_key(row) for row in rows)
    obsolete = Counter(projects.memory_group_key(row) for row in rows if row.get("obsoleteSignals"))
    lines = []
    for key in sorted(groups.keys() | cleaned.keys(), key=lambda k: (-counts[k], k)):
        label = groups.get(key) or (key if key.startswith("/") else key.rsplit("/", 1)[-1])
        status = f"cleaned {cleaned[key]}" if key in cleaned else "uncleaned"
        lines.append(
            f"{label}: {counts[key]} entries ({obsolete[key]} obsolete-candidate) [{status}]"
        )
    return "\n".join(lines) or "no memory entries"


def _show_catalog(parser: MemoryParser, args: argparse.Namespace, verb: str) -> int:
    reg = catalog(args.registry, no_scan=args.no_scan, persist=not args.dry_run)
    rows = _filtered_rows(args, reg, verb)
    decisions = _decisions(reg)
    if verb == "triage":
        rows = [row for row in rows if _revision(row) not in decisions]
    records = [_record(row, decisions) for row in rows]
    cached = reg.get("memorySources", {})
    statuses = [
        cached.get(source)
        or {"source": source, "status": "not-scanned", "reason": None, "scannedAt": None}
        for source in memory_scan.SOURCE_NAMES
    ]
    stores = [
        store
        for _, store in sorted(reg.get("memoryStores", {}).items())
        if store.get("configured") is not False
        and (not args.source or store["source"] in args.source)
    ]
    cleaned = reg.get("memoryProjectsCleanedAt", {})
    _emit_status(statuses, stores)
    if verb == "overview" and not (args.selector or args.project or args.source):
        message = _rollup(rows, cleaned)
    else:
        message = "\n".join(map(_entry_line, records)) or (
            "no memory awaiting review" if verb == "triage" else "no matching memory entries"
        )
    parser.emit(
        {
            "records": records,
            "statuses": statuses,
            "stores": stores,
            "projectsCleanedAt": cleaned,
            "cached": args.no_scan,
            "dryRun": args.dry_run,
        },
        message,
    )
    return 0


def _catalog_command(verb: str, argv: list[str], description: str) -> int:
    parser = MemoryParser(verb, argv, description)
    parser.add_argument("--project", action="append", help="filter by project label or path")
    parser.add_argument(
        "--source", action="append", type=canonical_agent, choices=memory_scan.SOURCE_NAMES
    )
    parser.add_argument("--no-scan", action="store_true", help="read cached metadata only")
    parser.add_argument("--dry-run", action="store_true", help="show results without saving a scan")
    if verb == "overview":
        parser.add_argument("selector", nargs="?", help="project label, path or memory ID prefix")
    elif verb == "search":
        parser.add_argument("query", help="search cached names, descriptions, paths and IDs")
    args = parser.parse_args(argv)
    if verb == "search":
        args.no_scan = True
    return parser.run(lambda: _show_catalog(parser, args, verb))


def cmd_mem_scan(argv: list[str]) -> int:
    return _catalog_command("refresh", argv, "Scan native memory and refresh the review catalog.")


def cmd_mem_triage(argv: list[str]) -> int:
    return _catalog_command("triage", argv, "List native memory entries awaiting a decision.")


def cmd_mem_overview(argv: list[str]) -> int:
    return _catalog_command("overview", argv, "Show native memory sources and active entries.")


def cmd_mem_search(argv: list[str]) -> int:
    return _catalog_command("search", argv, "Search the cached native memory catalog.")


def _detail(parser: MemoryParser, args: argparse.Namespace) -> int:
    reg = registry.load_registry(args.registry)
    row = memory_catalog.select_entry(reg, args.id)
    record = entry_metadata(row, reg)
    if not args.no_scan:
        observed = memory_scan.read_record(reg, row)
        record.update(content=observed.content, mutationBlock=observed.mutation_block)
    parser.emit({"record": record}, detail_text(record))
    return 0


def detail_text(record: dict[str, Any]) -> str:
    lines = [_entry_line(record)]
    if record.get("fingerprint") and record.get("incarnation"):
        lines.extend(
            [f"Fingerprint {record['fingerprint']}", f"Incarnation {record['incarnation']}"]
        )
    if record.get("description"):
        lines.append(record["description"])
    decision = record.get("decision")
    if decision:
        lines.append(f"Operation {decision['operationId']}")
        if decision.get("harvestToken"):
            lines.append(f"Harvest token {decision['harvestToken']}")
    if "content" in record:
        lines.extend(["", record["content"]])
    return "\n".join(lines)


def cmd_mem_detail(argv: list[str]) -> int:
    parser = MemoryParser("detail", argv, "Read one exact native memory entry.")
    parser.add_argument("id", help="unique memory ID prefix or legacy Claude memory file path")
    parser.add_argument("--no-scan", action="store_true", help="read cached metadata only")
    args = parser.parse_args(argv)
    return parser.run(lambda: _detail(parser, args))
