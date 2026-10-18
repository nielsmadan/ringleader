from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date

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
