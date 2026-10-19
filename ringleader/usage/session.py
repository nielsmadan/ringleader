from __future__ import annotations

from dataclasses import dataclass, field

from .aggregate import Bucket
from .base import UsageRecord

_TOP_TURNS_LIMIT = 10


@dataclass
class SessionReport:
    session_id: str = ""
    source: str = ""
    turns: int = 0
    totals: Bucket = field(default_factory=Bucket)
    cache_hit_rate: float = 0.0
    top_turns: list[tuple[int, int]] = field(default_factory=list)
    context_growth: list[int] = field(default_factory=list)
    cost: float | None = None


def analyze(records: list[UsageRecord]) -> SessionReport:
    rep = SessionReport()
    if not records:
        return rep
    ordered = sorted(records, key=lambda r: r.timestamp)
    rep.session_id = ordered[0].session_id
    rep.source = ordered[0].source
    rep.turns = len(ordered)
    for r in ordered:
        rep.totals.add(r)
    denom = rep.totals.cache_read + rep.totals.input + rep.totals.cache_write
    rep.cache_hit_rate = round(rep.totals.cache_read / denom, 4) if denom else 0.0
    main = [r for r in ordered if not r.nested]
    rep.top_turns = sorted(((i, r.total) for i, r in enumerate(main)), key=lambda t: -t[1])[
        :_TOP_TURNS_LIMIT
    ]
    rep.context_growth = [r.input + r.cache_read for r in main]
    rep.cost = round(rep.totals.cost, 4) if rep.totals.cost else None
    return rep
