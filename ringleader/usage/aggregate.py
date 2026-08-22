from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .base import UsageRecord
from .claude import ClaudeUsageReader
from .codex import CodexUsageReader
from .opencode import OpenCodeUsageReader
from .pi import PiUsageReader
from .pricing import estimate

READERS = {
    "claude": ClaudeUsageReader,
    "codex": CodexUsageReader,
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


def day_of(rec: UsageRecord) -> str:
    return (rec.timestamp or "")[:10]


def in_window(rec: UsageRecord, since: str | None, until: str | None) -> bool:
    d = day_of(rec)
    if since and d < since:
        return False
    return not (until and d > until)
