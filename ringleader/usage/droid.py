from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from functools import cached_property, partial
from itertools import chain
from pathlib import Path
from typing import Any

from ..sources.base import iso_mtime
from ..sources.droid import default_sessions_dir, session_start
from .base import UsageRecord, as_dict, as_int, record_day, total_of
from .checkpoint import Checkpoint, decode_record

_SIDECAR_SUFFIX = ".settings.json"


def _sidecar(transcript: Path) -> dict[str, Any]:
    path = transcript.with_name(transcript.stem + _SIDECAR_SUFFIX)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {}
    except OSError as e:
        print(f"skipped {path}: {e}", file=sys.stderr)
        raise
    try:
        return as_dict(json.loads(raw))
    except json.JSONDecodeError as e:
        print(f"skipped {path}: {e}", file=sys.stderr)
        raise


def _model(settings: dict[str, Any]) -> str | None:
    router = settings.get("effectiveFactoryRouterModel")
    if isinstance(router, dict):
        router = router.get("modelId")
    for candidate in (router, settings.get("model")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


@dataclass(frozen=True)
class _Thread:
    session_id: str
    record: UsageRecord | None


def _identity(path: Path) -> tuple[str, dict[str, Any]]:
    start = session_start(path)
    session_id = start.get("id")
    return (session_id if isinstance(session_id, str) else ""), start


def _thread_rows(path: Path) -> list[_Thread]:
    session_id, start = _identity(path)
    settings = _sidecar(path)
    usage = as_dict(settings.get("tokenUsage"))
    if not usage:
        return [_Thread(session_id, None)]
    inp = as_int(usage.get("inputTokens"))
    out = as_int(usage.get("outputTokens"))
    cache_read = as_int(usage.get("cacheReadTokens"))
    cache_write = as_int(usage.get("cacheCreationTokens"))
    cwd = start.get("cwd")
    record = UsageRecord(
        source="droid",
        session_id=session_id,
        project=cwd if isinstance(cwd, str) and cwd else None,
        timestamp=iso_mtime(path.stat()),
        provider="factory",
        model=_model(settings),
        input=inp,
        output=out,
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=as_int(usage.get("thinkingTokens")),
        total=total_of(input=inp, cache_read=cache_read, cache_write=cache_write, output=out),
        record_id=f"droid|{session_id}",
    )
    return [_Thread(session_id, record)]


def _decode_threads(rows: list[dict[str, Any]]) -> list[_Thread]:
    (row,) = rows
    if not isinstance(row["session_id"], str):
        raise TypeError("invalid thread identity")
    record = row["record"]
    return [_Thread(row["session_id"], decode_record(record) if record is not None else None)]


@dataclass
class DroidUsageReader:
    name: str = "droid"
    sessions_dir: Path = field(default_factory=default_sessions_dir)

    def __post_init__(self) -> None:
        self.sessions_dir = Path(self.sessions_dir)

    def available(self) -> bool:
        return self.sessions_dir.exists()

    def records(
        self, since: date | None = None, session_prefix: str | None = None
    ) -> Iterator[UsageRecord]:
        start = since.isoformat() if since else ""
        prefix = session_prefix or ""
        for thread in self._threads:
            record = thread.record
            if (
                record is not None
                and record.timestamp[:10] >= start
                and thread.session_id.startswith(prefix)
            ):
                yield replace(record)

    def latest_day(self) -> str | None:
        return max((record_day(r) for r in self.records()), default=None) or None

    @cached_property
    def _threads(self) -> list[_Thread]:
        if not self.available():
            return []
        out: list[_Thread] = []
        seen: set[str] = set()
        globs = chain(self.sessions_dir.glob("*.jsonl"), self.sessions_dir.glob("*/*.jsonl"))
        files = sorted(globs)
        with Checkpoint(self.name, self.sessions_dir, files) as checkpoint:
            for f in files:
                if f.parent.name == "btw":
                    continue
                try:
                    (thread,) = checkpoint.load(
                        f,
                        partial(_thread_rows, f),
                        encode=asdict,
                        decode=_decode_threads,
                        companions=(f.with_name(f.stem + _SIDECAR_SUFFIX),),
                    )
                except (OSError, ValueError, OverflowError):
                    try:
                        thread = _Thread(_identity(f)[0], None)
                    except (OSError, ValueError, OverflowError):
                        continue
                if not thread.session_id or thread.session_id in seen:
                    continue
                seen.add(thread.session_id)
                out.append(thread)
        return out
