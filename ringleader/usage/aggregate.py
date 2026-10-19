from __future__ import annotations

import argparse
import contextlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, TextIO

from .base import UsageReader, UsageRecord, record_day
from .claude import ClaudeUsageReader
from .codex import CodexUsageReader
from .droid import DroidUsageReader
from .opencode import OpenCodeUsageReader
from .pi import PiUsageReader
from .pricing import estimate

# Each reader's constructor takes its own reader-specific kwargs (e.g. ClaudeUsageReader's
# projects_dir), so this can't be typed any more precisely than "produces a UsageReader" --
# callers that need a specific reader's own kwargs should import that reader directly
# instead of going through this registry.
READERS: dict[str, Callable[..., UsageReader]] = {
    "claude": ClaudeUsageReader,
    "codex": CodexUsageReader,
    "droid": DroidUsageReader,
    "opencode": OpenCodeUsageReader,
    "pi": PiUsageReader,
}
ARCHIVE_KWARG: dict[str, str] = {
    "claude": "projects_dir",
    "codex": "sessions_dir",
    "droid": "sessions_dir",
}
AMBIGUOUS_LIMIT = 10
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_day(value: str) -> date:
    if _DAY.fullmatch(value):
        with contextlib.suppress(ValueError):
            return date.fromisoformat(value)
    raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date")


def readers(
    sources: Iterable[str], dirs: Mapping[str, Mapping[str, Any]] | None = None
) -> list[UsageReader]:
    built = (
        cls(**(dirs or {}).get(name, {}))
        for name in sources
        if (cls := READERS.get(name)) is not None
    )
    return [reader for reader in built if reader.available()]


def archive_readers(sources: Iterable[str], root: Path) -> list[UsageReader]:
    return readers(
        (name for name in sources if name in ARCHIVE_KWARG),
        {name: {kwarg: root} for name, kwarg in ARCHIVE_KWARG.items()},
    )


def flush_caches(found: Iterable[UsageReader]) -> None:
    for reader in found:
        if isinstance(reader, CodexUsageReader):
            reader.flush()


def collect_archived(sources: Iterable[str], root: Path) -> list[UsageRecord]:
    found = archive_readers(sources, root)
    records = [rec for reader in found for rec in reader.records()]
    flush_caches(found)
    return records


def latest_day(found: Iterable[UsageReader]) -> str | None:
    return max((d for reader in found if (d := reader.latest_day())), default=None)


def print_ambiguous(
    query: str, matches: Sequence[tuple[str, str]], width: int, *, file: TextIO | None = None
) -> None:
    print(f"ambiguous session id {query!r} — matches {len(matches)} sessions:", file=file)
    for source, session_id in matches[:AMBIGUOUS_LIMIT]:
        print(f"  {source:<{width}} {session_id}", file=file)
    if len(matches) > AMBIGUOUS_LIMIT:
        print(f"  … {len(matches) - AMBIGUOUS_LIMIT} more", file=file)


@dataclass
class Bucket:
    total: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    cost: float = 0.0
    records: int = 0
    priced_records: int = 0
    unpriced_records: int = 0

    @property
    def cost_partial(self) -> bool:
        return bool(self.priced_records and self.unpriced_records)

    @property
    def cost_or_none(self) -> float | None:
        return self.cost if self.priced_records else None

    def add(self, rec: UsageRecord) -> None:
        self.total += rec.total
        self.input += rec.input
        self.output += rec.output
        self.cache_read += rec.cache_read
        self.cache_write += rec.cache_write
        self.reasoning += rec.reasoning
        c = estimate(rec)
        if c is None:
            self.unpriced_records += 1
        else:
            self.cost += c
            self.priced_records += 1
        self.records += 1


def rollup(records: Iterable[UsageRecord], key: Callable[[UsageRecord], str]) -> dict[str, Bucket]:
    out: dict[str, Bucket] = {}
    for rec in records:
        out.setdefault(key(rec) or "(unknown)", Bucket()).add(rec)
    return out


def in_window(rec: UsageRecord, since: date | None, until: date | None) -> bool:
    d = record_day(rec)
    if since and d < since.isoformat():
        return False
    return not (until and d > until.isoformat())
