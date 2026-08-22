from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .base import UsageRecord, as_dict, as_float_or_none, as_int, total_of


def default_sessions_dir() -> Path:
    return Path.home() / ".pi" / "agent" / "sessions"


def _session_id(path: Path) -> str:
    return path.stem.split("_", 1)[-1]


def _decode_project(raw: str) -> str:
    return "/" + raw.strip("-").replace("-", "/")


@dataclass
class PiUsageReader:
    name: str = "pi"
    sessions_dir: Path = field(default_factory=default_sessions_dir)

    def __post_init__(self) -> None:
        self.sessions_dir = Path(self.sessions_dir)

    def available(self) -> bool:
        return self.sessions_dir.exists()

    def records(self) -> Iterator[UsageRecord]:
        if not self.available():
            return
        for f in sorted(self.sessions_dir.glob("*/*.jsonl")):
            try:
                fh = f.open(encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for ordinal, line in enumerate(fh):
                    if '"usage"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = as_dict(o.get("message"))
                    usage = as_dict(msg.get("usage"))
                    if not usage:
                        continue
                    inp = as_int(usage.get("input", 0))
                    out = as_int(usage.get("output", 0))
                    cr = as_int(usage.get("cacheRead", 0))
                    cw = as_int(usage.get("cacheWrite", 0))
                    cost = as_float_or_none(as_dict(usage.get("cost")).get("total"))
                    yield UsageRecord(
                        source="pi",
                        session_id=_session_id(f),
                        project=_decode_project(f.parent.name),
                        timestamp=o.get("timestamp") or msg.get("timestamp") or "",
                        provider=msg.get("provider"),
                        model=msg.get("model"),
                        input=inp,
                        output=out,
                        cache_read=cr,
                        cache_write=cw,
                        reasoning=as_int(usage.get("reasoning", 0)),
                        total=total_of(input=inp, cache_read=cr, cache_write=cw, output=out),
                        cost=float(cost) if cost is not None else None,
                        record_id=f"{f}|{ordinal}",
                    )
