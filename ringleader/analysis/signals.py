from __future__ import annotations

import json
import re
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass, replace
from itertools import groupby, pairwise
from pathlib import PurePosixPath
from typing import Literal, get_args

from . import taxonomy
from .facts import CompactionFact, ErrorCategory, RequestFact, ToolCallFact, TranscriptFacts

type SignalKind = Literal[
    "tool_failure",
    "retry_run",
    "runaway_window",
    "edit_churn",
    "failure_streak",
    "reread_churn",
    "search_loop",
    "unsupported_completion",
    "user_interruption",
    "rejected_tool_use",
    "permission_denial",
    "compaction",
    "mid_task_compaction",
    "rewind",
]
type Span = tuple[int, int]
SIGNAL_KINDS: dict[str, SignalKind] = {k: k for k in get_args(SignalKind.__value__)}

STRETCH_KINDS: frozenset[SignalKind] = frozenset(
    {
        "retry_run",
        "runaway_window",
        "edit_churn",
        "failure_streak",
        "reread_churn",
        "search_loop",
        "unsupported_completion",
        "mid_task_compaction",
    }
)
RETRY_RUN_MIN = 3
RUNAWAY_WINDOW = 12
RUNAWAY_FAILURES = 6
RUNAWAY_CLASS_FAILURES = 3
RUNAWAY_CLASS_CALLS = 10
EDIT_CHURN_EDITS = 3
EDIT_CHURN_SPAN = 10
FAILURE_STREAK_MIN = 3
REREAD_MIN = 5
REREAD_SPAN = 10
DEFAULT_READ_LIMIT = 2000
PAGED_SUFFIXES = frozenset({".pdf"})
SEARCH_LOOP_MIN = 20
MID_TASK_BEFORE = 10
MID_TASK_AFTER = 5
MID_TASK_FILE_OVERLAP = 1
CONTEXT_DROP_RATIO = 0.30
COMPLETION_CLAIM = re.compile(
    r"\b(?:done|fixed|all tests pass(?:ed|ing)?|completed?|implemented|works now)\b",
    re.IGNORECASE,
)
NEGATED_CLAIM = re.compile(
    r"\b(?:not|(?:isn|aren|wasn|weren)['\u2019]t)\s+(?:yet\s+)?"
    r"(?:done|fixed|complete|completed|implemented|working)\b"
    r"|\bhaven['\u2019]t\b",
    re.IGNORECASE,
)
CODEX_POLLING_TOOLS = frozenset({"wait_agent", "wait", "sleep", "write_stdin", "list_agents"})
LOOK_KINDS = frozenset({"read", "search"})
PROGRESS_KINDS = frozenset({"write", "validate"})
NON_AGENT_FAILURE_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {"interruption", "user_rejected", "permission_denied", "sibling_cancelled"}
)


@dataclass(frozen=True)
class SignalHit:
    kind: SignalKind
    start_seq: int
    end_seq: int
    detail: str


def live_calls(facts: TranscriptFacts) -> tuple[ToolCallFact, ...]:
    return tuple(c for c in facts.tool_calls if c.on_main_branch)


def _runs(keys: Sequence[Hashable | None], minimum: int) -> tuple[Span, ...]:
    spans: list[Span] = []
    position = 0
    for key, group in groupby(keys):
        size = sum(1 for _ in group)
        if key is not None and size >= minimum:
            spans.append((position, position + size - 1))
        position += size
    return tuple(spans)


def _merge(spans: Iterable[Span]) -> tuple[Span, ...]:
    merged: list[Span] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _hit(calls: Sequence[ToolCallFact], kind: SignalKind, span: Span, detail: str) -> SignalHit:
    return SignalHit(kind, calls[span[0]].seq, calls[span[1]].seq, detail)


def is_agent_failure(status: str, category: str | None) -> bool:
    return status == "error" and category not in NON_AGENT_FAILURE_CATEGORIES


def is_failure(call: ToolCallFact) -> bool:
    return is_agent_failure(call.status, call.error_category)


def _name(path: str) -> str:
    return PurePosixPath(path).name or path


def _tool_failures(calls: Sequence[ToolCallFact]) -> tuple[SignalHit, ...]:
    return tuple(
        SignalHit("tool_failure", c.seq, c.seq, f"{c.tool}: {c.error_category or 'error'}")
        for c in calls
        if is_failure(c)
    )


def _retry_runs(calls: Sequence[ToolCallFact]) -> tuple[SignalHit, ...]:
    keys = [None if c.tool in CODEX_POLLING_TOOLS else (c.tool, c.input_hash) for c in calls]
    spans = _runs(keys, RETRY_RUN_MIN)
    return tuple(
        _hit(calls, "retry_run", s, f"{calls[s[0]].tool} repeated {s[1] - s[0] + 1}x")
        for s in spans
    )


def _failure_streaks(calls: Sequence[ToolCallFact]) -> tuple[SignalHit, ...]:
    spans = _runs([True if is_failure(c) else None for c in calls], FAILURE_STREAK_MIN)
    return tuple(
        _hit(calls, "failure_streak", s, f"{s[1] - s[0] + 1} consecutive errors") for s in spans
    )


def _runaway_span(window: Sequence[ToolCallFact], offset: int) -> Span | None:
    errors = [offset + i for i, c in enumerate(window) if is_failure(c)]
    if len(errors) >= RUNAWAY_FAILURES:
        return errors[0], errors[-1]
    classes = Counter(c.command_class for c in window).most_common(1)
    if not classes or classes[0][1] < RUNAWAY_CLASS_CALLS:
        return None
    dominant = classes[0][0]
    class_errors = [
        offset + i for i, c in enumerate(window) if c.command_class == dominant and is_failure(c)
    ]
    if len(class_errors) < RUNAWAY_CLASS_FAILURES:
        return None
    return class_errors[0], class_errors[-1]


def _runaway_windows(calls: Sequence[ToolCallFact]) -> tuple[SignalHit, ...]:
    starts = range(max(len(calls) - RUNAWAY_WINDOW + 1, 1))
    found = (_runaway_span(calls[i : i + RUNAWAY_WINDOW], i) for i in starts)
    spans = _merge(s for s in found if s is not None)
    return tuple(
        _hit(
            calls,
            "runaway_window",
            s,
            f"{sum(is_failure(c) for c in calls[s[0] : s[1] + 1])} errors"
            f" in {s[1] - s[0] + 1} calls",
        )
        for s in spans
    )


def _reverted(writes: Sequence[ToolCallFact]) -> bool:
    return any(
        later.edit_old_hash is not None
        and later.edit_new_hash is not None
        and (later.edit_old_hash, later.edit_new_hash)
        == (earlier.edit_new_hash, earlier.edit_old_hash)
        for i, earlier in enumerate(writes)
        for later in writes[i + 1 :]
    )


def _churned(calls: Sequence[ToolCallFact], group: Sequence[int]) -> bool:
    between = calls[group[0] + 1 : group[-1]]
    return any(is_failure(c) for c in between) or _reverted([calls[p] for p in group])


def _churn_spans(
    calls: Sequence[ToolCallFact], events: Sequence[tuple[int, bool]]
) -> tuple[tuple[Span, int], ...]:
    positions = [p for p, is_write in events if is_write]
    spans: list[Span] = []
    for k, last in enumerate(positions):
        group = [p for p in positions[: k + 1] if last - p < EDIT_CHURN_SPAN]
        if len(group) >= EDIT_CHURN_EDITS and _churned(calls, group):
            spans.append((group[0], group[-1]))
    return tuple((s, sum(1 for p in positions if s[0] <= p <= s[1])) for s in _merge(spans))


def _edit_churn(calls: Sequence[ToolCallFact]) -> tuple[SignalHit, ...]:
    return tuple(
        _hit(calls, "edit_churn", span, f"{edits} edits to {_name(path)}")
        for path, events in _file_events(calls).items()
        for span, edits in _churn_spans(calls, events)
    )


def _file_events(calls: Sequence[ToolCallFact]) -> dict[str, list[tuple[int, bool]]]:
    found: dict[str, list[tuple[int, bool]]] = defaultdict(list)
    for i, c in enumerate(calls):
        if c.file_path and c.kind in {"read", "write"}:
            found[c.file_path].append((i, c.kind == "write"))
    return found


def _read_range(call: ToolCallFact) -> Span:
    start = call.read_offset or 0
    return start, start + (call.read_limit or DEFAULT_READ_LIMIT)


def _overlaps(a: ToolCallFact, b: ToolCallFact) -> bool:
    if a.input_hash == b.input_hash:
        return True
    if PurePosixPath(a.file_path or "").suffix.lower() in PAGED_SUFFIXES:
        return False
    (a_start, a_end), (b_start, b_end) = _read_range(a), _read_range(b)
    return a_start < b_end and b_start < a_end


def _reread_spans(
    calls: Sequence[ToolCallFact], events: Sequence[tuple[int, bool]]
) -> tuple[Span, ...]:
    spans: list[Span] = []
    reads: list[int] = []
    for position, is_write in events:
        if is_write:
            reads = []
            continue
        reads = [p for p in reads if position - p < REREAD_SPAN] + [position]
        same = [p for p in reads if _overlaps(calls[p], calls[position])]
        if len(same) >= REREAD_MIN:
            spans.append((same[0], position))
    return _merge(spans)


def _reread_churn(calls: Sequence[ToolCallFact]) -> tuple[SignalHit, ...]:
    return tuple(
        _hit(calls, "reread_churn", s, f"{_name(path)} reread without a write")
        for path, events in _file_events(calls).items()
        for s in _reread_spans(calls, events)
    )


def _human_turns(facts: TranscriptFacts) -> list[int]:
    return sorted(
        m.before_call for m in facts.user_messages if m.origin == "human" and m.on_main_branch
    )


def _look_keys(calls: Sequence[ToolCallFact], turns: Sequence[int]) -> list[int | None]:
    keys: list[int | None] = []
    run = 0
    for i, c in enumerate(calls):
        prior = calls[i - 1].seq if i else -1
        if bisect_right(turns, c.seq) > bisect_right(turns, prior):
            run += 1
        keys.append(run if c.kind in LOOK_KINDS else None)
        if c.kind in PROGRESS_KINDS:
            run += 1
    return keys


def _search_loops(facts: TranscriptFacts, calls: Sequence[ToolCallFact]) -> tuple[SignalHit, ...]:
    first_write = next((i for i, c in enumerate(calls) if c.kind == "write"), None)
    if first_write is None:
        return ()
    relevant = [c for c in calls[first_write:] if c.kind in LOOK_KINDS or c.kind in PROGRESS_KINDS]
    spans = _runs(_look_keys(relevant, _human_turns(facts)), SEARCH_LOOP_MIN)
    return tuple(
        _hit(relevant, "search_loop", s, f"{s[1] - s[0] + 1} reads and searches without progress")
        for s in spans
    )


def _claims_completion(final_text: str | None) -> bool:
    return bool(
        final_text and COMPLETION_CLAIM.search(final_text) and not NEGATED_CLAIM.search(final_text)
    )


def _unsupported_completion(
    calls: Sequence[ToolCallFact], final_text: str | None
) -> tuple[SignalHit, ...]:
    if not _claims_completion(final_text):
        return ()
    last_ok = max(
        (i for i, c in enumerate(calls) if c.kind == "validate" and c.status == "ok"), default=-1
    )
    writes = [i for i, c in enumerate(calls) if i > last_ok and c.kind == "write"]
    paths = [p for p in (calls[i].file_path for i in writes) if p]
    code = [p for p in paths if taxonomy.is_code_file(p)]
    if not code:
        return ()
    span = (writes[-1], len(calls) - 1)
    return (_hit(calls, "unsupported_completion", span, f"claimed done after editing {code[-1]}"),)


def _category_hits(
    calls: Sequence[ToolCallFact], kind: SignalKind, category: ErrorCategory
) -> tuple[SignalHit, ...]:
    return tuple(
        SignalHit(kind, c.seq, c.seq, c.tool) for c in calls if c.error_category == category
    )


def _interruptions(facts: TranscriptFacts) -> tuple[SignalHit, ...]:
    return tuple(
        SignalHit("user_interruption", m.before_call, m.before_call, "user interrupted")
        for m in facts.user_messages
        if m.is_interrupt and m.origin == "human" and m.on_main_branch
    )


def _rewinds(facts: TranscriptFacts) -> tuple[SignalHit, ...]:
    return tuple(
        SignalHit(
            "rewind",
            r.before_call,
            r.before_call,
            f"user rewound, abandoning {r.abandoned_calls} calls",
        )
        for r in facts.rewinds
    )


def _next_seq(calls: Sequence[ToolCallFact], line: int) -> int:
    fallback = calls[-1].seq + 1 if calls else 0
    return next((c.seq for c in calls if c.line >= line), fallback)


def _dropped(previous: RequestFact, current: RequestFact) -> bool:
    drop = previous.context_tokens - current.context_tokens
    return previous.context_tokens > 0 and drop > CONTEXT_DROP_RATIO * previous.context_tokens


def inferred_compactions(
    requests: Sequence[RequestFact], calls: Sequence[ToolCallFact]
) -> tuple[CompactionFact, ...]:
    return tuple(
        CompactionFact(
            before_call=_next_seq(calls, current.line),
            line=current.line,
            timestamp=current.timestamp,
            trigger="inferred",
            pre_tokens=previous.context_tokens,
            post_tokens=current.context_tokens,
        )
        for previous, current in pairwise(requests)
        if _dropped(previous, current)
    )


def _compactions(
    facts: TranscriptFacts, calls: Sequence[ToolCallFact]
) -> tuple[CompactionFact, ...]:
    if facts.compactions:
        return facts.compactions
    return inferred_compactions(tuple(r for r in facts.requests if r.on_main_branch), calls)


def _compaction_hits(compactions: Sequence[CompactionFact]) -> tuple[SignalHit, ...]:
    return tuple(
        SignalHit(
            "compaction",
            c.before_call,
            c.before_call,
            f"{c.trigger} compaction {c.pre_tokens or '?'} -> {c.post_tokens or '?'} tokens",
        )
        for c in compactions
    )


def _human_between(facts: TranscriptFacts, after_seq: int, upto_seq: int) -> bool:
    return any(
        m.origin == "human" and m.on_main_branch and after_seq < m.before_call <= upto_seq
        for m in facts.user_messages
    )


def _mid_task(
    facts: TranscriptFacts, calls: Sequence[ToolCallFact], compaction: CompactionFact
) -> SignalHit | None:
    before = [c for c in calls if c.seq < compaction.before_call][-MID_TASK_BEFORE:]
    after = [c for c in calls if c.seq >= compaction.before_call][:MID_TASK_AFTER]
    if not before or not after or _human_between(facts, before[-1].seq, after[0].seq):
        return None
    shared = {c.file_path for c in before if c.file_path} & {
        c.file_path for c in after if c.file_path
    }
    if len(shared) < MID_TASK_FILE_OVERLAP:
        return None
    names = ", ".join(sorted(_name(p) for p in shared))
    return SignalHit(
        "mid_task_compaction", before[0].seq, after[-1].seq, f"compacted mid-task on {names}"
    )


def _mid_task_hits(
    facts: TranscriptFacts, calls: Sequence[ToolCallFact], compactions: Sequence[CompactionFact]
) -> tuple[SignalHit, ...]:
    found = (_mid_task(facts, calls, c) for c in compactions)
    return tuple(h for h in found if h is not None)


def _collapsed(hits: Iterable[SignalHit]) -> tuple[SignalHit, ...]:
    ordered = sorted(hits, key=lambda h: (h.start_seq, h.kind, h.end_seq, h.detail))
    return tuple(
        replace(
            group[0],
            end_seq=max(h.end_seq for h in group),
            detail=group[0].detail if len(group) == 1 else f"{group[0].detail} (x{len(group)})",
        )
        for _, members in groupby(ordered, key=lambda h: (h.kind, h.start_seq))
        for group in [list(members)]
    )


def detect(facts: TranscriptFacts) -> tuple[SignalHit, ...]:
    calls = live_calls(facts)
    compactions = _compactions(facts, calls)
    hits = (
        *_tool_failures(calls),
        *_retry_runs(calls),
        *_runaway_windows(calls),
        *_edit_churn(calls),
        *_failure_streaks(calls),
        *_reread_churn(calls),
        *_search_loops(facts, calls),
        *_unsupported_completion(calls, facts.final_text),
        *_interruptions(facts),
        *_category_hits(calls, "rejected_tool_use", "user_rejected"),
        *_category_hits(calls, "permission_denial", "permission_denied"),
        *_compaction_hits(compactions),
        *_mid_task_hits(facts, calls, compactions),
        *_rewinds(facts),
    )
    return tuple(sorted(_collapsed(hits), key=lambda h: (h.start_seq, h.end_seq, h.kind, h.detail)))


def _pattern(pattern: re.Pattern[str]) -> list[object]:
    return [pattern.pattern, pattern.flags]


def fingerprint_material() -> str:
    material = {
        "stretch_kinds": sorted(STRETCH_KINDS),
        "look_kinds": sorted(LOOK_KINDS),
        "progress_kinds": sorted(PROGRESS_KINDS),
        "non_agent_failure_categories": sorted(NON_AGENT_FAILURE_CATEGORIES),
        "codex_polling_tools": sorted(CODEX_POLLING_TOOLS),
        "thresholds": {
            "retry_run_min": RETRY_RUN_MIN,
            "runaway_window": RUNAWAY_WINDOW,
            "runaway_failures": RUNAWAY_FAILURES,
            "runaway_class_failures": RUNAWAY_CLASS_FAILURES,
            "runaway_class_calls": RUNAWAY_CLASS_CALLS,
            "edit_churn_edits": EDIT_CHURN_EDITS,
            "edit_churn_span": EDIT_CHURN_SPAN,
            "failure_streak_min": FAILURE_STREAK_MIN,
            "reread_min": REREAD_MIN,
            "reread_span": REREAD_SPAN,
            "default_read_limit": DEFAULT_READ_LIMIT,
            "paged_suffixes": sorted(PAGED_SUFFIXES),
            "search_loop_min": SEARCH_LOOP_MIN,
            "mid_task_before": MID_TASK_BEFORE,
            "mid_task_after": MID_TASK_AFTER,
            "mid_task_file_overlap": MID_TASK_FILE_OVERLAP,
            "context_drop_ratio": CONTEXT_DROP_RATIO,
        },
        "patterns": {
            "completion_claim": _pattern(COMPLETION_CLAIM),
            "negated_claim": _pattern(NEGATED_CLAIM),
        },
    }
    return json.dumps(material, sort_keys=True, ensure_ascii=False)
