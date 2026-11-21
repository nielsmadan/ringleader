from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from functools import partial
from pathlib import Path

from ..sources.pi import default_sessions_dir as pi_sessions_dir
from .base import (
    UsageRecord,
    as_dict,
    as_float_or_none,
    as_int,
    latest_day_of,
    project_dir,
    session_files,
    total_of,
)
from .checkpoint import Checkpoint


def default_sessions_dir() -> Path:
    return pi_sessions_dir()


def session_of(path: Path) -> str:
    if path.name == "session.jsonl" and path.parent.name.startswith("run-"):
        return path.parents[2].name.split("_", 1)[-1]
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

    def _files(self) -> list[Path]:
        return [
            *self.sessions_dir.glob("*/*.jsonl"),
            *self.sessions_dir.glob("*/*/*/run-*/session.jsonl"),
        ]

    def records(
        self, since: date | None = None, session_prefix: str | None = None
    ) -> Iterator[UsageRecord]:
        if not self.available():
            return
        files = self._files()
        with Checkpoint(self.name, self.sessions_dir, files) as checkpoint:
            for f in session_files(files, since, session_prefix, session_of):
                yield from checkpoint.records(f, partial(self._file_records, f))

    def latest_day(self) -> str | None:
        if not self.available():
            return None
        files = self._files()
        with Checkpoint(self.name, self.sessions_dir, files) as checkpoint:
            return latest_day_of(
                files, lambda f: checkpoint.records(f, partial(self._file_records, f))
            )

    def _file_records(self, f: Path) -> Iterator[UsageRecord]:
        with f.open(encoding="utf-8", errors="replace") as fh:
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
                    session_id=session_of(f),
                    project=_decode_project(project_dir(f, self.sessions_dir)),
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
