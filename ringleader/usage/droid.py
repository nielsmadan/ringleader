from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from functools import cached_property
from itertools import chain
from pathlib import Path
from typing import Any

from ..sources.base import iso_mtime
from ..sources.droid import default_sessions_dir, session_start
from .base import UsageRecord, as_dict, as_int, record_day, total_of

_SIDECAR_SUFFIX = ".settings.json"


def _sidecar(transcript: Path) -> dict[str, Any]:
    path = transcript.with_name(transcript.stem + _SIDECAR_SUFFIX)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {}
    except OSError as e:
        print(f"skipped {path}: {e}", file=sys.stderr)
        return {}
    try:
        return as_dict(json.loads(raw))
    except json.JSONDecodeError as e:
        print(f"skipped {path}: {e}", file=sys.stderr)
        return {}


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
    path: Path
    timestamp: str
    start: dict[str, Any]
    settings: dict[str, Any]

    @property
    def session_id(self) -> str:
        session_id = self.start.get("id")
        return session_id if isinstance(session_id, str) else ""


def _record(thread: _Thread, usage: dict[str, Any]) -> UsageRecord:
    inp = as_int(usage.get("inputTokens"))
    out = as_int(usage.get("outputTokens"))
    cache_read = as_int(usage.get("cacheReadTokens"))
    cache_write = as_int(usage.get("cacheCreationTokens"))
    cwd = thread.start.get("cwd")
    return UsageRecord(
        source="droid",
        session_id=thread.session_id,
        project=cwd if isinstance(cwd, str) and cwd else None,
        timestamp=thread.timestamp,
        provider="factory",
        model=_model(thread.settings),
        input=inp,
        output=out,
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=as_int(usage.get("thinkingTokens")),
        total=total_of(input=inp, cache_read=cache_read, cache_write=cache_write, output=out),
        record_id=f"droid|{thread.session_id}",
    )


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
            if thread.timestamp[:10] < start or not thread.session_id.startswith(prefix):
                continue
            usage = as_dict(thread.settings.get("tokenUsage"))
            if usage:
                yield _record(thread, usage)

    def latest_day(self) -> str | None:
        return max((record_day(r) for r in self.records()), default=None) or None

    @cached_property
    def _threads(self) -> list[_Thread]:
        if not self.available():
            return []
        out: list[_Thread] = []
        seen: set[str] = set()
        globs = chain(self.sessions_dir.glob("*.jsonl"), self.sessions_dir.glob("*/*.jsonl"))
        for f in sorted(globs):
            if f.parent.name == "btw":
                continue  # a btw fork is its own layout, never re-filed under a slug dir
            try:
                thread = _Thread(f, iso_mtime(f.stat()), session_start(f), _sidecar(f))
            except (OSError, json.JSONDecodeError):
                continue
            if not thread.session_id or thread.session_id in seen:
                continue
            seen.add(thread.session_id)
            out.append(thread)
        return out
