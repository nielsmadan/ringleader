from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .base import UsageRecord, as_dict, as_float_or_none, as_int, total_of


def default_storage_dir() -> Path:
    return Path.home() / ".local" / "share" / "opencode" / "storage"


@dataclass
class OpenCodeUsageReader:
    name: str = "opencode"
    storage_dir: Path = field(default_factory=default_storage_dir)

    def __post_init__(self) -> None:
        self.storage_dir = Path(self.storage_dir)
        self._dirs: dict[str, str] | None = None

    def _project(self, session: str) -> str | None:
        if self._dirs is None:
            self._dirs = {}
            for f in sorted((self.storage_dir / "session").glob("*/*.json")):
                try:
                    o = json.loads(f.read_text(encoding="utf-8", errors="replace"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(o, dict) and o.get("id") and o.get("directory"):
                    self._dirs[str(o["id"])] = str(o["directory"])
        return self._dirs.get(session)

    def available(self) -> bool:
        return (self.storage_dir / "message").exists()

    def records(self) -> Iterator[UsageRecord]:
        if not self.available():
            return
        for f in sorted((self.storage_dir / "message").glob("*/*.json")):
            try:
                o = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            if o.get("role") != "assistant":
                continue
            tok = as_dict(o.get("tokens"))
            if not tok:
                continue
            cache = as_dict(tok.get("cache"))
            inp = as_int(tok.get("input", 0))
            out = as_int(tok.get("output", 0))
            cr = as_int(cache.get("read", 0))
            cw = as_int(cache.get("write", 0))
            created = as_float_or_none(as_dict(o.get("time")).get("created"))
            cost = as_float_or_none(o.get("cost"))
            session = o.get("sessionID") or f.parent.name
            yield UsageRecord(
                source="opencode",
                session_id=session,
                project=self._project(session),
                timestamp=datetime.fromtimestamp(created / 1000, UTC).isoformat()
                if created
                else "",
                provider=o.get("providerID"),
                model=o.get("modelID"),
                input=inp,
                output=out,
                cache_read=cr,
                cache_write=cw,
                reasoning=as_int(tok.get("reasoning", 0)),
                total=total_of(input=inp, cache_read=cr, cache_write=cw, output=out),
                cost=float(cost) if cost is not None else None,
                record_id=str(o.get("id") or f.stem),
            )
