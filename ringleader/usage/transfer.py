from __future__ import annotations

import json
import socket
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .base import UsageRecord

EXPORT_VERSION = 1


def machine_id() -> str:
    return socket.gethostname() or "unknown"


def dump(records: Iterable[UsageRecord], machine: str) -> dict[str, Any]:
    return {
        "version": EXPORT_VERSION,
        "machine": machine,
        "records": [asdict(r) for r in records],
    }


def write_export(
    records: Iterable[UsageRecord], path: str | Path, machine: str | None = None
) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dump(records, machine or machine_id())), encoding="utf-8")
    return p


def load_exports(dir_: str | Path) -> list[UsageRecord]:
    out: list[UsageRecord] = []
    for f in sorted(Path(dir_).glob("*.json")):
        try:
            body = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(body, dict) or "records" not in body:
            continue
        machine = body.get("machine")
        for raw in body["records"]:
            try:
                rec = UsageRecord(**raw)
            except TypeError:
                continue
            rec.machine = rec.machine or machine
            out.append(rec)
    return out


def dedupe(records: Iterable[UsageRecord]) -> list[UsageRecord]:
    me = machine_id()
    seen: set[tuple[Any, ...]] = set()
    out: list[UsageRecord] = []
    for r in records:
        # record_id is unique per read; the composite is the fallback for older exports
        key = (
            (r.machine or me, r.source, r.record_id)
            if r.record_id
            else (r.machine or me, r.source, r.session_id, r.timestamp)
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out
