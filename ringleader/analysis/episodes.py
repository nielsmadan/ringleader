from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from ..usage.base import parse_iso, total_of
from . import taxonomy
from .facts import ToolCallFact, TranscriptFacts
from .health import PENALTIES
from .signals import STRETCH_KINDS, SignalHit, SignalKind, is_failure, live_calls

EXCERPT_CHARS = 4000
MESSAGE_PREVIEW_CHARS = 300
CALL_PREVIEW_CHARS = 160
ERROR_PREVIEW_CHARS = 200
EPISODE_SPLIT_GAP_CALLS = 30
EPISODE_SPLIT_GAP_SECONDS = 1800


@dataclass(frozen=True)
class Episode:
    episode_id: str
    anchor: str
    agent: str
    session_id: str
    thread_id: str
    thread_key: str
    path: str
    kinds: tuple[SignalKind, ...]
    start_seq: int
    end_seq: int
    start_line: int
    end_line: int
    start_ts: str
    end_ts: str
    calls: int
    severity: int
    tokens: int
    content_hash: str
    excerpt: str


@dataclass(frozen=True)
class _Stretch:
    start: int
    end: int
    kinds: frozenset[SignalKind]


def episode_anchor(calls: Sequence[ToolCallFact], first: ToolCallFact) -> str:
    shared = sum(1 for c in calls if c.call_id == first.call_id) > 1
    return f"seq:{first.seq}" if not first.call_id or shared else first.call_id


def episode_id(agent: str, thread_key: str, anchor: str) -> str:
    return taxonomy.short_hash(f"{agent}|{thread_key}|{anchor}")


def episode_hash(calls: Sequence[ToolCallFact]) -> str:
    return taxonomy.short_hash(json.dumps([[c.call_id or c.seq, c.status] for c in calls]))


def _stretch(seqs: Sequence[int], hit: SignalHit) -> _Stretch | None:
    start, end = bisect_left(seqs, hit.start_seq), bisect_right(seqs, hit.end_seq) - 1
    return _Stretch(start, end, frozenset({hit.kind})) if start <= end else None


def _merged(stretches: Sequence[_Stretch]) -> tuple[_Stretch, ...]:
    merged: list[_Stretch] = []
    for s in sorted(stretches, key=lambda s: (s.start, s.end)):
        if merged and s.start <= merged[-1].end + 1:
            last = merged[-1]
            merged[-1] = _Stretch(last.start, max(last.end, s.end), last.kinds | s.kinds)
        else:
            merged.append(s)
    return tuple(merged)


def _apart(calls: Sequence[ToolCallFact], earlier: _Stretch, later: _Stretch) -> bool:
    if later.start - earlier.start > EPISODE_SPLIT_GAP_CALLS:
        return True
    before, after = (
        parse_iso(calls[earlier.start].timestamp),
        parse_iso(calls[later.start].timestamp),
    )
    return bool(before and after and (after - before).total_seconds() > EPISODE_SPLIT_GAP_SECONDS)


def _cuts(calls: Sequence[ToolCallFact], stretches: Sequence[_Stretch]) -> frozenset[int]:
    ordered = sorted(stretches, key=lambda s: (s.start, s.end))
    return frozenset(
        later.start
        for earlier, later in pairwise(ordered)
        if later.start != earlier.start and _apart(calls, earlier, later)
    )


def _pieces(calls: Sequence[ToolCallFact], stretches: Sequence[_Stretch]) -> tuple[_Stretch, ...]:
    cuts = _cuts(calls, stretches)
    bounds = (
        (merged.start, *sorted(c for c in cuts if merged.start < c <= merged.end), merged.end + 1)
        for merged in _merged(stretches)
    )
    return tuple(
        _Stretch(
            low,
            high - 1,
            frozenset(k for s in stretches if s.start < high and s.end >= low for k in s.kinds),
        )
        for edges in bounds
        for low, high in pairwise(edges)
    )


def _call_line(call: ToolCallFact) -> str:
    category = f" [{call.error_category}]" if call.error_category else ""
    error = f" {taxonomy.preview(call.error_text, ERROR_PREVIEW_CHARS)}" if call.error_text else ""
    shown = taxonomy.preview(call.input_preview, CALL_PREVIEW_CHARS)
    return f"#{call.seq} {call.tool} {call.kind} {shown} -> {call.status}{category}{error}"


def _excerpt(facts: TranscriptFacts, calls: Sequence[ToolCallFact]) -> str:
    human = [
        m
        for m in facts.user_messages
        if m.origin == "human" and m.on_main_branch and m.before_call <= calls[0].seq
    ]
    lead = [f"user: {taxonomy.preview(human[-1].text, MESSAGE_PREVIEW_CHARS)}"] if human else []
    return "\n".join([*lead, *(_call_line(c) for c in calls)])[:EXCERPT_CHARS]


def _episode(
    facts: TranscriptFacts, calls: Sequence[ToolCallFact], kinds: frozenset[SignalKind]
) -> Episode:
    first, last = calls[0], calls[-1]
    anchor = episode_anchor(facts.tool_calls, first)
    ordered = tuple(sorted(kinds))
    return Episode(
        episode_id=episode_id(facts.ref.agent, facts.thread_key, anchor),
        anchor=anchor,
        agent=facts.ref.agent,
        session_id=facts.session_id,
        thread_id=facts.thread_id,
        thread_key=facts.thread_key,
        path=str(facts.ref.path),
        kinds=ordered,
        start_seq=first.seq,
        end_seq=last.seq,
        start_line=first.line,
        end_line=last.line,
        start_ts=first.timestamp,
        end_ts=last.timestamp,
        calls=len(calls),
        severity=sum(PENALTIES[k][0] for k in ordered) + sum(is_failure(c) for c in calls),
        tokens=sum(
            total_of(
                input=r.input, cache_read=r.cache_read, cache_write=r.cache_write, output=r.output
            )
            for r in facts.requests
            if r.on_main_branch and first.line <= r.line <= last.line
        ),
        content_hash=episode_hash(calls),
        excerpt=_excerpt(facts, calls),
    )


def episodes_for(facts: TranscriptFacts, hits: Sequence[SignalHit]) -> tuple[Episode, ...]:
    live = live_calls(facts)
    seqs = [c.seq for c in live]
    found = (_stretch(seqs, h) for h in hits if h.kind in STRETCH_KINDS)
    pieces = _pieces(live, [s for s in found if s is not None])
    return tuple(_episode(facts, live[s.start : s.end + 1], s.kinds) for s in pieces)


def fingerprint_material() -> str:
    material = {
        "excerpt_chars": EXCERPT_CHARS,
        "message_preview_chars": MESSAGE_PREVIEW_CHARS,
        "call_preview_chars": CALL_PREVIEW_CHARS,
        "error_preview_chars": ERROR_PREVIEW_CHARS,
        "episode_split_gap_calls": EPISODE_SPLIT_GAP_CALLS,
        "episode_split_gap_seconds": EPISODE_SPLIT_GAP_SECONDS,
    }
    return json.dumps(material, sort_keys=True, ensure_ascii=False)
