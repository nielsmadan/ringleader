from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .sources.claude_code import default_projects_dir as projects_dir

# USD per token: (input, output, cache_creation, cache_read).
# cache_creation is the 5m-TTL write (1.25x input); cache_read is 0.1x input.
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
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    if "fable" in m or "mythos" in m:
        return "fable"
    if "sonnet" in m:
        return "sonnet"
    return "sonnet"


def _cost(usage: dict, fam: str) -> float:
    inp, out, cw, cr = PRICES[fam]
    return (
        usage.get("input_tokens", 0) * inp
        + usage.get("output_tokens", 0) * out
        + usage.get("cache_creation_input_tokens", 0) * cw
        + usage.get("cache_read_input_tokens", 0) * cr
    )


@dataclass
class Bucket:
    cost: float = 0.0
    total_tokens: int = 0
    output_tokens: int = 0
    messages: int = 0


@dataclass
class Cell:
    date: str
    project: str  # decoded, e.g. "wrksp/oss/ShortcutKit"
    project_raw: str  # the encoded dir name
    session_id: str
    family: str
    b: Bucket = field(default_factory=Bucket)


def _decode_project(raw: str) -> str:
    home = str(Path.home()).replace("/", "-")  # e.g. -Users-nielsmadan
    label = raw[len(home) :] if raw.startswith(home) else raw
    return label.lstrip("-").replace("-", "/")


def collect(root=None, extra_roots=()) -> dict[tuple, Cell]:
    """Scan Claude transcripts and aggregate token cost by (date, project, session,
    model family). Dedupes assistant records by (message id, requestId) — Claude Code
    rewrites records across resumes/summaries, so a raw sum double-counts. Only
    Claude models are counted (the weekly limit is Claude-specific)."""
    roots = [Path(root) if root else projects_dir()]
    roots += [Path(r) for r in extra_roots]

    seen: set[tuple] = set()
    cells: dict[tuple, Cell] = {}

    for base in roots:
        if not base.exists():
            continue
        for f in sorted(base.glob("*/*.jsonl")):
            project_raw = f.parent.name
            session_id = f.stem
            try:
                fh = f.open(encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"assistant"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if o.get("type") != "assistant":
                        continue
                    msg = o.get("message") or {}
                    usage = msg.get("usage") or {}
                    if not usage:
                        continue
                    fam = model_family(msg.get("model"))
                    if fam is None:
                        continue
                    key = (msg.get("id"), o.get("requestId"))
                    if key in seen:
                        continue
                    seen.add(key)

                    date = (o.get("timestamp") or "")[:10]
                    if not date:
                        continue
                    ck = (date, project_raw, session_id, fam)
                    cell = cells.get(ck)
                    if cell is None:
                        cell = cells[ck] = Cell(
                            date=date,
                            project=_decode_project(project_raw),
                            project_raw=project_raw,
                            session_id=session_id,
                            family=fam,
                        )
                    cell.b.cost += _cost(usage, fam)
                    cell.b.total_tokens += (
                        usage.get("input_tokens", 0)
                        + usage.get("output_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                    )
                    cell.b.output_tokens += usage.get("output_tokens", 0)
                    cell.b.messages += 1
    return cells


def rollup(cells, key) -> dict[str, Bucket]:
    out: dict[str, Bucket] = {}
    for cell in cells.values():
        k = key(cell)
        b = out.setdefault(k, Bucket())
        b.cost += cell.b.cost
        b.total_tokens += cell.b.total_tokens
        b.output_tokens += cell.b.output_tokens
        b.messages += cell.b.messages
    return out
