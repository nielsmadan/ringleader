from __future__ import annotations

from .base import UsageRecord

# USD per token: (input, output, cache_write, cache_read)
PRICES = {
    "opus": (5 / 1e6, 25 / 1e6, 6.25 / 1e6, 0.5 / 1e6),
    "sonnet": (3 / 1e6, 15 / 1e6, 3.75 / 1e6, 0.3 / 1e6),
    "haiku": (1 / 1e6, 5 / 1e6, 1.25 / 1e6, 0.1 / 1e6),
    "fable": (10 / 1e6, 50 / 1e6, 12.5 / 1e6, 1.0 / 1e6),
}


def model_family(model: str | None) -> str | None:
    m = model or ""
    if "claude" not in m:
        return None
    if "fable" in m or "mythos" in m:
        return "fable"
    for fam in ("opus", "haiku"):
        if fam in m:
            return fam
    return "sonnet"


def estimate(rec: UsageRecord) -> float | None:
    if rec.cost is not None:
        return rec.cost
    fam = model_family(rec.model)
    if fam is None:
        return None
    inp, out, cw, cr = PRICES[fam]
    return rec.input * inp + rec.output * out + rec.cache_write * cw + rec.cache_read * cr
