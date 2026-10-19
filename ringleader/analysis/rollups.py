from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from .. import registry
from ..agents import canonical_agent
from ..usage import aggregate, transfer
from ..usage.base import UsageReader, UsageRecord, total_of
from ..usage.claude import is_claude_model
from ..usage.pricing import estimate
from . import labels as labels_module
from . import store
from .adapters import TOKENS_ONLY_SOURCES
from .facts import MAIN_THREAD, CompactionFact
from .health import HealthScore, Outcome, classify_outcome, health, known_signals
from .scoping import Filter, days_clause, placeholders, scope_filter, selected
from .signals import inferred_compactions, is_agent_failure, live_calls

type Dimension = Literal[
    "project", "folder", "skill", "plugin", "mcp_server", "tool", "model", "agent"
]
DIMENSIONS: tuple[Dimension, ...] = (
    "project",
    "folder",
    "skill",
    "plugin",
    "mcp_server",
    "tool",
    "model",
    "agent",
)
DEFAULT_DIMENSIONS: tuple[Dimension, ...] = ("project", "model", "skill", "tool")
DEFAULT_CONTEXT_WINDOW = 200_000
LARGE_CONTEXT_WINDOW = 1_000_000
MIN_CALLS_FOR_RANKING = 5
IDLE_5M = 300
IDLE_1H = 3600
SCHEMA_VERSION = 1
EVIDENCE_LIMIT = 12
PREVIEW_CHARS = 300
ERROR_PREVIEW_CHARS = 200
UNKNOWN = "(unknown)"
CALL_ONLY_DIMENSIONS: frozenset[Dimension] = frozenset({"mcp_server", "tool"})
NOTABLE_KINDS = frozenset({"correction", "frustration", "redirection"})
TOTAL_KEY = "total"


@dataclass(frozen=True, kw_only=True)
class Scope:
    since: date | None
    until: date | None
    agents: frozenset[str]
    selectors: tuple[str, ...]
    include_archived: bool


@dataclass(frozen=True)
class ProjectInfo:
    key: str
    label: str
    root: str | None


@dataclass(frozen=True)
class TokenTotals:
    input: int
    output: int
    cache_read: int
    cache_write: int
    cache_write_5m: int
    cache_write_1h: int
    reasoning: int
    total: int
    cost: float | None
    cost_partial: bool


@dataclass(frozen=True)
class CacheStats:
    hit_rate: float | None
    write_5m: int
    write_1h: int
    rebuilds: int
    rebuild_tokens: int
    rebuilds_after_5m: int
    rebuilds_after_1h: int
    rebuild_cost: float | None


@dataclass(frozen=True)
class TokenReaders:
    live: tuple[UsageReader, ...]
    archived: tuple[UsageReader, ...]


@dataclass(frozen=True)
class ContextStats:
    p50: int
    p95: int
    max: int
    fill_p95: float | None
    compactions: int


@dataclass(frozen=True)
class RollupRow:
    key: str
    sessions: int
    calls: int
    errors: int
    tokens: TokenTotals | None
    cache_hit_rate: float | None
    context: ContextStats | None
    requests: int = 0


@dataclass(frozen=True)
class SessionSummary:
    agent: str
    session_id: str
    project: str
    folder: str | None
    started_at: str | None
    ended_at: str | None
    outcome: Outcome
    health: HealthScore
    signals: dict[str, int]
    subagent_signals: dict[str, int]
    tool_calls: int
    tool_errors: int
    episode_tokens: int
    tokens: TokenTotals


def project_map(reg: Mapping[str, Any]) -> dict[tuple[str, str], ProjectInfo]:
    records = [
        rec
        for bucket in ("threads", "archived")
        for rec in reg.get(bucket) or []
        if rec.get("sessionId")
    ]
    return {
        (canonical_agent(registry.identity(rec)[0]), rec["sessionId"]): ProjectInfo(
            key=rec.get("gitProjectKey") or rec.get("project") or UNKNOWN,
            label=rec.get("gitProjectLabel") or rec.get("project") or UNKNOWN,
            root=rec.get("gitRoot"),
        )
        for rec in records
    }


def _folder_info(cwd: str | None) -> ProjectInfo:
    label = cwd or UNKNOWN
    return ProjectInfo(label, label, None)


def _info(
    projects: Mapping[tuple[str, str], ProjectInfo], agent: str, session_id: str, cwd: str | None
) -> ProjectInfo:
    return projects.get((agent, session_id)) or _folder_info(cwd)


def _fetch(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    try:
        return cursor.execute(sql, params).fetchall()
    finally:
        cursor.close()


def _drain(cursor: sqlite3.Cursor) -> Iterator[sqlite3.Row]:
    try:
        yield from cursor
    finally:
        cursor.close()


def _stream(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()
) -> Iterator[sqlite3.Row]:
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    cursor.execute(sql, params)
    return _drain(cursor)


def _counted(agent: str, model: str | None) -> int:
    return int(agent != "claude-code" or is_claude_model(model))


def _windowed(scope: Scope | None) -> bool:
    return scope is not None and bool(scope.since or scope.until)


def _source(table: str, flt: Filter, scope: Scope | None) -> tuple[str, str, str, tuple[Any, ...]]:
    if table == "user_messages":
        where, params = days_clause(scope, "substr(r.ts, 1, 10)", flt)
        return "", f"{table} r", where, params
    if not _windowed(scope):
        return "", f"{table} r", flt.where, flt.params
    days, day_params = days_clause(scope, "day", Filter("1", ()))
    head = f"WITH r AS MATERIALIZED (SELECT * FROM {table} WHERE {days}) "
    return head, "r", flt.where, (*day_params, *flt.params)


def _deduped(
    conn: sqlite3.Connection,
    table: str,
    flt: Filter,
    scope: Scope | None = None,
    columns: str = "r.*",
    condition: str = "1",
) -> Iterator[sqlite3.Row]:
    head, source, where, params = _source(table, flt, scope)
    where += f" AND {condition}"
    if table == "requests":
        conn.create_function("rl_counted", 2, _counted, deterministic=True)
        where += " AND rl_counted(t.agent, r.model)"
    sql = (
        f"{head}SELECT * FROM (SELECT {columns}, t.agent AS t_agent, t.session_id AS t_session,"
        " t.cwd AS t_cwd, t.thread_id AS t_thread, ROW_NUMBER() OVER"
        " (PARTITION BY r.dedupe_key ORDER BY t.sort_key, r.line) AS rn"
        f" FROM {source} JOIN transcripts t USING (path) WHERE {where}) WHERE rn = 1"
    )
    return _stream(conn, sql, params)


def error_calls(
    conn: sqlite3.Connection,
    scope: Scope,
    projects: Mapping[tuple[str, str], ProjectInfo] | None,
) -> list[sqlite3.Row]:
    return list(
        _deduped(
            conn,
            "tool_calls",
            scope_filter(conn, scope, projects),
            scope,
            "r.signature, r.error_text, r.ts",
            "r.status = 'error' AND r.signature IS NOT NULL",
        )
    )


def _is_error(call: sqlite3.Row) -> bool:
    return is_agent_failure(call["status"], call["error_category"])


def _percentile(ordered: Sequence[float], q: float) -> float:
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


class _Tokens:
    def __init__(self) -> None:
        self.input = self.output = self.cache_read = self.cache_write = 0
        self.cache_write_5m = self.cache_write_1h = self.reasoning = self.total = 0
        self.models: dict[str | None, list[int]] = defaultdict(lambda: [0] * 5)
        self.cost = 0.0
        self.priced = self.unpriced = 0

    def add_request(self, r: sqlite3.Row) -> None:
        self.input += r["input"]
        self.output += r["output"]
        self.cache_read += r["cache_read"]
        self.cache_write += r["cache_write"]
        self.cache_write_5m += r["cache_write_5m"]
        self.cache_write_1h += r["cache_write_1h"]
        self.reasoning += r["reasoning"]
        self.total += total_of(
            input=r["input"],
            cache_read=r["cache_read"],
            cache_write=r["cache_write"],
            output=r["output"],
        )
        bucket = self.models[r["model"]]
        for i, field_name in enumerate(
            ("input", "output", "cache_read", "cache_write", "cache_write_1h")
        ):
            bucket[i] += r[field_name]

    def add_record(self, rec: UsageRecord) -> None:
        self.input += rec.input
        self.output += rec.output
        self.cache_read += rec.cache_read
        self.cache_write += rec.cache_write
        self.cache_write_1h += rec.cache_write_1h
        self.reasoning += rec.reasoning
        self.total += rec.total
        if (cost := estimate(rec)) is None:
            self.unpriced += 1
        else:
            self.cost += cost
            self.priced += 1

    def totals(self) -> TokenTotals:
        cost, priced, unpriced = self.cost, self.priced, self.unpriced
        for model, v in self.models.items():
            estimated = estimate(_synthetic(model, v[0], v[1], v[2], v[3], v[4]))
            if estimated is None:
                unpriced += 1
            else:
                cost += estimated
                priced += 1
        return TokenTotals(
            input=self.input,
            output=self.output,
            cache_read=self.cache_read,
            cache_write=self.cache_write,
            cache_write_5m=self.cache_write_5m,
            cache_write_1h=self.cache_write_1h,
            reasoning=self.reasoning,
            total=self.total,
            cost=cost if priced else None,
            cost_partial=bool(priced and unpriced),
        )


def _synthetic(
    model: str | None, input: int, output: int, cache_read: int, cache_write: int, one_hour: int
) -> UsageRecord:
    return UsageRecord(
        source="claude",
        session_id="",
        project=None,
        timestamp="",
        provider="anthropic",
        model=model,
        input=input,
        output=output,
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=0,
        total=0,
        cache_write_1h=one_hour,
    )


def _hit_rate(t: TokenTotals) -> float | None:
    denominator = t.input + t.cache_read + t.cache_write
    return t.cache_read / denominator if denominator else None


class _Group:
    def __init__(self) -> None:
        self.sessions: set[tuple[str, str]] = set()
        self.calls = self.errors = self.requests = 0
        self.tokens = _Tokens()
        self.contexts: list[int] = []
        self.fills: list[float] = []
        self.paths: set[str] = set()


def _window(agent: str, peak: int, recorded: int | None) -> int | None:
    if recorded is not None:
        return recorded if recorded > 0 else None
    if agent == "claude-code" and peak > DEFAULT_CONTEXT_WINDOW:
        return LARGE_CONTEXT_WINDOW
    return DEFAULT_CONTEXT_WINDOW


type _Windows = dict[tuple[str, str | None], int | None]


def _fill(tokens: int, window: int | None) -> float | None:
    return None if window is None else tokens / window


def _windows_for(
    conn: sqlite3.Connection, flt: Filter, scope: Scope | None = None
) -> tuple[_Windows, dict[tuple[str, str | None], int]]:
    conn.create_function("rl_counted", 2, _counted, deterministic=True)
    active, active_params = days_clause(scope, "day", Filter("1", ()))
    in_window = (
        f" AND r.path IN (SELECT path FROM requests WHERE {active})" if _windowed(scope) else ""
    )
    rows = _fetch(
        conn,
        "SELECT r.path AS path, r.model AS model, t.agent AS agent,"
        " MAX(r.context_tokens) AS peak, MAX(r.context_window) AS recorded"
        f" FROM requests r JOIN transcripts t USING (path) WHERE {flt.where}{in_window}"
        " AND rl_counted(t.agent, r.model) GROUP BY r.path, r.model",
        (*flt.params, *(active_params if in_window else ())),
    )
    path_peaks: dict[str, int] = defaultdict(int)
    for r in rows:
        path_peaks[r["path"]] = max(path_peaks[r["path"]], r["peak"])
    windows = {
        (r["path"], r["model"]): _window(r["agent"], path_peaks[r["path"]], r["recorded"])
        for r in rows
    }
    peaks = {(r["path"], r["model"]): r["peak"] for r in rows}
    return windows, peaks


def _source_filter(scope: Scope | None) -> Filter:
    if scope is None:
        return Filter("1", ())
    clauses = [
        *([] if scope.include_archived else ["t.archived = 0"]),
        *([f"t.agent IN ({placeholders(len(scope.agents))})"] if scope.agents else []),
    ]
    return Filter(" AND ".join(clauses) or "1", tuple(sorted(scope.agents)))


def latest_day(conn: sqlite3.Connection, scope: Scope | None = None) -> str | None:
    flt = _source_filter(scope)
    conn.create_function("rl_counted", 2, _counted, deterministic=True)
    row = conn.execute(
        "SELECT MAX(r.day) FROM requests r JOIN transcripts t USING (path)"
        f" WHERE {flt.where} AND rl_counted(t.agent, r.model)",
        flt.params,
    ).fetchone()
    return row[0] if row and row[0] else None


def tokens_only_names(scope: Scope) -> list[str]:
    return [
        name
        for name in TOKENS_ONLY_SOURCES
        if not scope.agents or canonical_agent(name) in scope.agents
    ]


def tokens_only_readers(
    scope: Scope,
    dirs: Mapping[str, Mapping[str, Any]] | None = None,
    archive: Path | None = None,
) -> TokenReaders:
    names = tokens_only_names(scope)
    return TokenReaders(
        live=tuple(aggregate.readers(names, dirs)),
        archived=tuple(aggregate.archive_readers(names, archive))
        if scope.include_archived and archive is not None
        else (),
    )


def tokens_only_records(scope: Scope, readers: TokenReaders) -> list[UsageRecord]:
    live = [rec for reader in readers.live for rec in reader.records(since=scope.since)]
    archived = [rec for reader in readers.archived for rec in reader.records()]
    pooled = transfer.dedupe([*live, *archived]) if archived else live
    return [rec for rec in pooled if aggregate.in_window(rec, scope.since, scope.until)]


def _extra_info(projects: Mapping[tuple[str, str], ProjectInfo], rec: UsageRecord) -> ProjectInfo:
    return _info(projects, canonical_agent(rec.source), rec.session_id, rec.project)


def _selected_extra(
    extra: Sequence[UsageRecord],
    scope: Scope,
    projects: Mapping[tuple[str, str], ProjectInfo],
) -> list[UsageRecord]:
    return [
        rec
        for rec in extra
        if selected(
            scope.selectors,
            projects.get((canonical_agent(rec.source), rec.session_id)),
            rec.project,
        )
    ]


def _plugin_of(skill: str | None) -> str | None:
    return skill.split(":", 1)[0] if skill and ":" in skill else None


type _Key[T] = Callable[[T], str | None]


def _request_key(
    dim: Dimension, projects: Mapping[tuple[str, str], ProjectInfo]
) -> _Key[sqlite3.Row]:
    keys: dict[Dimension, _Key[sqlite3.Row]] = {
        "project": lambda r: _info(projects, r["t_agent"], r["t_session"], r["t_cwd"]).label,
        "folder": lambda r: r["t_cwd"] or UNKNOWN,
        "skill": lambda r: r["skill"],
        "plugin": lambda r: r["plugin"],
        "model": lambda r: r["model"] or UNKNOWN,
        "agent": lambda r: r["t_agent"],
    }
    return keys.get(dim, lambda _r: None)


def _call_key(dim: Dimension, projects: Mapping[tuple[str, str], ProjectInfo]) -> _Key[sqlite3.Row]:
    keys: dict[Dimension, _Key[sqlite3.Row]] = {
        "project": lambda r: _info(projects, r["t_agent"], r["t_session"], r["t_cwd"]).label,
        "folder": lambda r: r["t_cwd"] or UNKNOWN,
        "skill": lambda r: r["skill"],
        "plugin": lambda r: _plugin_of(r["skill"]),
        "mcp_server": lambda r: r["mcp_server"],
        "tool": lambda r: r["tool"],
        "agent": lambda r: r["t_agent"],
    }
    return keys.get(dim, lambda _r: None)


def _extra_key(
    dim: Dimension, projects: Mapping[tuple[str, str], ProjectInfo]
) -> _Key[UsageRecord]:
    keys: dict[Dimension, _Key[UsageRecord]] = {
        "project": lambda rec: _extra_info(projects, rec).label,
        "folder": lambda rec: rec.project or UNKNOWN,
        "model": lambda rec: rec.model or UNKNOWN,
        "agent": lambda rec: canonical_agent(rec.source),
    }
    return keys.get(dim, lambda _rec: None)


_INFERRED_DAY = (
    "COALESCE((SELECT c.day FROM tool_calls c WHERE c.path = g.path AND c.seq = g.start_seq),"
    " substr(t.last_ts, 1, 10))"
)


def _compaction_events(
    conn: sqlite3.Connection, flt: Filter, scope: Scope
) -> dict[str, frozenset[tuple[Any, ...]]]:
    where, params = days_clause(scope, "substr(c.ts, 1, 10)", flt)
    recorded = _fetch(
        conn,
        "SELECT c.path AS path, c.ts AS ts, c.trigger AS trigger, c.pre_tokens AS pre,"
        f" c.post_tokens AS post FROM compactions c JOIN transcripts t USING (path) WHERE {where}",
        params,
    )
    inferred_where, inferred_params = days_clause(scope, _INFERRED_DAY, flt)
    inferred = _fetch(
        conn,
        "SELECT g.path AS path, COUNT(*) AS n FROM signals g JOIN transcripts t USING (path)"
        " WHERE g.kind = 'compaction' AND NOT EXISTS"
        f" (SELECT 1 FROM compactions c WHERE c.path = g.path) AND {inferred_where}"
        " GROUP BY g.path",
        inferred_params,
    )
    events: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
    for r in recorded:
        events[r["path"]].add((r["ts"], r["trigger"], r["pre"], r["post"]))
    for r in inferred:
        events[r["path"]].update(("inferred", r["path"], i) for i in range(r["n"]))
    return {path: frozenset(found) for path, found in events.items()}


def _context_stats(
    group: _Group, compactions: Mapping[str, frozenset[tuple[Any, ...]]]
) -> ContextStats | None:
    if not group.contexts:
        return None
    ordered = sorted(group.contexts)
    fills = sorted(group.fills)
    return ContextStats(
        p50=int(_percentile(ordered, 0.5)),
        p95=int(_percentile(ordered, 0.95)),
        max=ordered[-1],
        fill_p95=round(_percentile(fills, 0.95), 4) if fills else None,
        compactions=len(frozenset().union(*(compactions.get(p, frozenset()) for p in group.paths))),
    )


def _row(
    key: str,
    group: _Group,
    with_tokens: bool,
    compactions: Mapping[str, frozenset[tuple[Any, ...]]],
) -> RollupRow:
    tokens = group.tokens.totals() if with_tokens else None
    return RollupRow(
        key=key,
        sessions=len(group.sessions),
        calls=group.calls,
        errors=group.errors,
        tokens=tokens,
        cache_hit_rate=_hit_rate(tokens) if tokens else None,
        context=_context_stats(group, compactions) if with_tokens else None,
        requests=group.requests,
    )


_REQUEST_COLUMNS = (
    "r.path, r.model, r.skill, r.plugin, r.input, r.output, r.cache_read, r.cache_write,"
    " r.cache_write_5m, r.cache_write_1h, r.reasoning, r.context_tokens, r.rebuild,"
    " r.gap_seconds"
)
_CALL_COLUMNS = "r.skill, r.mcp_server, r.tool, r.status, r.error_category"


@dataclass(frozen=True)
class _Scan:
    flt: Filter
    requests: Iterable[sqlite3.Row]
    calls: Iterable[sqlite3.Row]
    windows: _Windows
    compactions: dict[str, frozenset[tuple[Any, ...]]]


@dataclass(frozen=True)
class _Spec:
    request_key: _Key[sqlite3.Row]
    call_key: _Key[sqlite3.Row]
    extra_key: _Key[UsageRecord]
    with_tokens: bool


def _scan(
    conn: sqlite3.Connection,
    scope: Scope,
    projects: Mapping[tuple[str, str], ProjectInfo],
    with_tokens: bool = True,
) -> _Scan:
    flt = scope_filter(conn, scope, projects)
    windows = _windows_for(conn, flt, scope)[0] if with_tokens else {}
    compactions = _compaction_events(conn, flt, scope) if with_tokens else {}
    requests = _deduped(conn, "requests", flt, scope, _REQUEST_COLUMNS) if with_tokens else ()
    return _Scan(
        flt=flt,
        requests=requests,
        calls=_deduped(conn, "tool_calls", flt, scope, _CALL_COLUMNS),
        windows=windows,
        compactions=compactions,
    )


def _spec_for(dim: Dimension, projects: Mapping[tuple[str, str], ProjectInfo]) -> _Spec:
    return _Spec(
        _request_key(dim, projects),
        _call_key(dim, projects),
        _extra_key(dim, projects),
        dim not in CALL_ONLY_DIMENSIONS,
    )


def _whole_spec() -> _Spec:
    def whole(_item: Any) -> str:
        return TOTAL_KEY

    return _Spec(whole, whole, whole, True)


class _Cache:
    def __init__(self) -> None:
        self.tokens = _Tokens()
        self.rebuilds: list[tuple[str | None, int, int, float]] = []

    def add(self, r: sqlite3.Row) -> None:
        self.tokens.add_request(r)
        if r["rebuild"]:
            self.rebuilds.append(
                (r["model"], r["cache_write"], r["cache_write_1h"], r["gap_seconds"] or 0)
            )

    def stats(self) -> CacheStats:
        rebuild_models: dict[str | None, list[int]] = defaultdict(lambda: [0, 0])
        for model, written, one_hour, _gap in self.rebuilds:
            rebuild_models[model][0] += written
            rebuild_models[model][1] += one_hour
        costs = [
            c
            for model, (written, one_hour) in rebuild_models.items()
            if (c := estimate(_synthetic(model, 0, 0, 0, written, one_hour))) is not None
        ]
        totals_ = self.tokens.totals()
        return CacheStats(
            hit_rate=_hit_rate(totals_),
            write_5m=totals_.cache_write_5m,
            write_1h=totals_.cache_write_1h,
            rebuilds=len(self.rebuilds),
            rebuild_tokens=sum(written for _, written, _, _ in self.rebuilds),
            rebuilds_after_5m=sum(1 for *_, gap in self.rebuilds if gap > IDLE_5M),
            rebuilds_after_1h=sum(1 for *_, gap in self.rebuilds if gap > IDLE_1H),
            rebuild_cost=sum(costs) if costs else None,
        )


def _collect_all(
    scan: _Scan,
    scope: Scope,
    projects: Mapping[tuple[str, str], ProjectInfo],
    extra: Sequence[UsageRecord],
    specs: Mapping[str, _Spec],
    cache: _Cache | None = None,
) -> dict[str, list[RollupRow]]:
    groups: dict[str, dict[str, _Group]] = {name: defaultdict(_Group) for name in specs}
    token_specs = [(groups[name], spec) for name, spec in specs.items() if spec.with_tokens]
    call_specs = [(groups[name], spec) for name, spec in specs.items()]
    for r in scan.requests:
        if cache is not None:
            cache.add(r)
        for found, spec in token_specs:
            if (key := spec.request_key(r)) is None:
                continue
            g = found[key]
            g.sessions.add((r["t_agent"], r["t_session"]))
            g.requests += 1
            g.tokens.add_request(r)
            g.contexts.append(r["context_tokens"])
            if (
                fill := _fill(r["context_tokens"], scan.windows[(r["path"], r["model"])])
            ) is not None:
                g.fills.append(fill)
            g.paths.add(r["path"])
    for c in scan.calls:
        failed = int(_is_error(c))
        for found, spec in call_specs:
            if (key := spec.call_key(c)) is None:
                continue
            g = found[key]
            g.sessions.add((c["t_agent"], c["t_session"]))
            g.calls += 1
            g.errors += failed
    for rec in _selected_extra(extra, scope, projects):
        for found, spec in token_specs:
            if (key := spec.extra_key(rec)) is None:
                continue
            g = found[key]
            g.sessions.add((canonical_agent(rec.source), rec.session_id))
            g.requests += 1
            g.tokens.add_record(rec)
    return {
        name: sorted(
            (_row(key, g, specs[name].with_tokens, scan.compactions) for key, g in found.items()),
            key=lambda r: (-(r.tokens.total if r.tokens else 0), -r.calls, r.key),
        )
        for name, found in groups.items()
    }


@dataclass(frozen=True)
class CorpusRollups:
    totals: RollupRow
    cache: CacheStats
    dimensions: dict[Dimension, list[RollupRow]]


def corpus_rollups(
    conn: sqlite3.Connection,
    scope: Scope,
    projects: Mapping[tuple[str, str], ProjectInfo],
    extra: Sequence[UsageRecord],
    dims: Sequence[Dimension],
) -> CorpusRollups:
    scan = _scan(conn, scope, projects)
    specs: dict[str, _Spec] = {
        TOTAL_KEY: _whole_spec(),
        **{d: _spec_for(d, projects) for d in dims},
    }
    cache = _Cache()
    found = _collect_all(scan, scope, projects, extra, specs, cache)
    whole = found[TOTAL_KEY]
    return CorpusRollups(
        totals=whole[0] if whole else _empty_row(),
        cache=cache.stats(),
        dimensions={d: found[d] for d in dims},
    )


def _empty_row() -> RollupRow:
    return RollupRow(
        key=TOTAL_KEY,
        sessions=0,
        calls=0,
        errors=0,
        tokens=_Tokens().totals(),
        cache_hit_rate=None,
        context=None,
    )


def _cache_from(rows: Iterable[sqlite3.Row]) -> CacheStats:
    cache = _Cache()
    for r in rows:
        cache.add(r)
    return cache.stats()


type _SignalsByPath = Mapping[str, Counter[str]]


def _signals_by_path(rows: Iterable[sqlite3.Row]) -> dict[str, Counter[str]]:
    found: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        found[r["path"]][r["kind"]] += r["n"]
    return found


def _kind_counts(by_path: _SignalsByPath, paths: Iterable[str]) -> dict[str, int]:
    counts = sum((by_path.get(p, Counter()) for p in paths), Counter[str]())
    return dict(sorted(counts.items()))


def _signal_rows(conn: sqlite3.Connection, flt: Filter) -> Iterator[sqlite3.Row]:
    return _stream(
        conn,
        "SELECT g.path AS path, g.kind AS kind, COUNT(*) AS n FROM signals g"
        f" JOIN transcripts t USING (path) WHERE {flt.where} GROUP BY g.path, g.kind",
        flt.params,
    )


def _overlaps(scope: Scope) -> tuple[str, tuple[Any, ...]]:
    clauses: list[str] = []
    params: list[Any] = []
    if scope.since:
        clauses.append("(t.last_ts IS NULL OR substr(t.last_ts, 1, 10) >= ?)")
        params.append(scope.since.isoformat())
    if scope.until:
        clauses.append("(t.first_ts IS NULL OR substr(t.first_ts, 1, 10) <= ?)")
        params.append(scope.until.isoformat())
    return "".join(f" AND {c}" for c in clauses), tuple(params)


def _is_running(
    running: frozenset[tuple[str, str]] | None, agent: str, session_id: str
) -> bool | None:
    return None if running is None else (agent, session_id) in running


@dataclass(frozen=True)
class _Health:
    outcome: Outcome
    score: HealthScore
    signals: dict[str, int]
    subagent_signals: dict[str, int]
    calls: int
    errors: int
    started_at: str | None
    ended_at: str | None


def _session_health(
    key: tuple[str, str],
    main: sqlite3.Row,
    members: Sequence[sqlite3.Row],
    signals: _SignalsByPath,
    now: datetime,
    running: frozenset[tuple[str, str]] | None,
) -> _Health:
    main_paths = {t["path"] for t in members if t["thread_id"] == MAIN_THREAD}
    sub_paths = {t["path"] for t in members} - main_paths
    own = _kind_counts(signals, main_paths)
    nested = _kind_counts(signals, sub_paths)
    outcome = classify_outcome(main["last_event"], main["last_ts"], now, _is_running(running, *key))
    calls = sum(t["s_calls"] for t in members)
    errors = sum(t["s_errors"] for t in members)
    return _Health(
        outcome=outcome,
        score=health(known_signals(Counter(own) + Counter(nested)), calls, errors, outcome),
        signals=own,
        subagent_signals=nested,
        calls=calls,
        errors=errors,
        started_at=min((t["first_ts"] for t in members if t["first_ts"]), default=None),
        ended_at=max((t["last_ts"] for t in members if t["last_ts"]), default=None),
    )


def _main_thread(members: Sequence[sqlite3.Row]) -> sqlite3.Row | None:
    return next((t for t in members if t["thread_id"] == MAIN_THREAD), None)


@dataclass(frozen=True)
class _SessionContext:
    projects: Mapping[tuple[str, str], ProjectInfo]
    now: datetime
    running: frozenset[tuple[str, str]] | None
    signals: _SignalsByPath
    tokens: Mapping[tuple[str, str], _Tokens]


def _summarize(
    key: tuple[str, str],
    members: Sequence[sqlite3.Row],
    ctx: _SessionContext,
) -> SessionSummary | None:
    main = _main_thread(members)
    if main is None:
        return None
    found = _session_health(key, main, members, ctx.signals, ctx.now, ctx.running)
    if found.calls < MIN_CALLS_FOR_RANKING:
        return None
    agent, session_id = key
    return SessionSummary(
        agent=agent,
        session_id=session_id,
        project=_info(ctx.projects, agent, session_id, main["cwd"]).label,
        folder=main["cwd"],
        started_at=found.started_at,
        ended_at=found.ended_at,
        outcome=found.outcome,
        health=found.score,
        signals=found.signals,
        subagent_signals=found.subagent_signals,
        tool_calls=found.calls,
        tool_errors=found.errors,
        episode_tokens=sum(t["s_episode_tokens"] for t in members),
        tokens=ctx.tokens[key].totals() if key in ctx.tokens else _Tokens().totals(),
    )


def _session_tokens(
    conn: sqlite3.Connection, flt: Filter, scope: Scope
) -> dict[tuple[str, str], _Tokens]:
    out: dict[tuple[str, str], _Tokens] = defaultdict(_Tokens)
    for r in _deduped(conn, "requests", flt, scope, _REQUEST_COLUMNS):
        out[(r["t_agent"], r["t_session"])].add_request(r)
    return out


_MEMBER_COLUMNS = (
    "t.path, t.sort_key, t.agent, t.session_id, t.thread_id, t.subagent_type,"
    " t.subagent_description, t.cwd, t.git_branch, t.first_ts, t.last_ts, t.last_event,"
    " COALESCE(s.calls, 0) AS s_calls, COALESCE(s.errors, 0) AS s_errors,"
    " COALESCE(s.episode_tokens, 0) AS s_episode_tokens"
)


def worst_sessions(
    conn: sqlite3.Connection,
    scope: Scope,
    projects: Mapping[tuple[str, str], ProjectInfo],
    now: datetime,
    running: frozenset[tuple[str, str]] | None,
    limit: int,
) -> list[SessionSummary]:
    flt = scope_filter(conn, scope, projects)
    overlap_sql, overlap_params = _overlaps(scope)
    active = {
        (r["agent"], r["session_id"])
        for r in _stream(
            conn,
            "SELECT DISTINCT t.agent AS agent, t.session_id AS session_id FROM transcripts t"
            f" JOIN thread_stats s USING (path) WHERE {flt.where}{overlap_sql}",
            (*flt.params, *overlap_params),
        )
    }
    members: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for r in _stream(
        conn,
        f"SELECT {_MEMBER_COLUMNS} FROM transcripts t LEFT JOIN thread_stats s USING (path)"
        " ORDER BY t.sort_key",
    ):
        if (key := (r["agent"], r["session_id"])) in active:
            members[key].append(r)
    signals = _signals_by_path(_signal_rows(conn, Filter("1", ())))
    ctx = _SessionContext(projects, now, running, signals, _session_tokens(conn, flt, scope))
    summaries = [s for key, group in members.items() if (s := _summarize(key, group, ctx))]
    ranked = sorted(
        summaries,
        key=lambda s: (-s.health.per_100_calls, -s.episode_tokens, s.agent, s.session_id),
    )
    return ranked[:limit]


def resolve_session(conn: sqlite3.Connection, prefix: str) -> list[tuple[str, str]]:
    if not prefix:
        return []
    size = len(prefix)
    rows = conn.execute(
        "SELECT DISTINCT agent, session_id FROM transcripts"
        " WHERE substr(session_id, 1, ?) = ? OR (thread_id != ? AND substr(thread_id, 1, ?) = ?)"
        " ORDER BY agent, session_id",
        (size, prefix, MAIN_THREAD, size, prefix),
    ).fetchall()
    return [(agent, session_id) for agent, session_id in rows]


@dataclass(frozen=True)
class TranscriptInfo:
    path: str
    thread_id: str
    subagent_type: str | None
    description: str | None


@dataclass(frozen=True)
class Evidence:
    line: int
    tool: str
    input_preview: str
    status: str
    error_category: str | None
    error_preview: str | None


@dataclass(frozen=True)
class EpisodeDetail:
    episode_id: str
    kinds: tuple[str, ...]
    path: str
    thread_id: str
    start_line: int
    end_line: int
    start_ts: str
    end_ts: str
    calls: int
    severity: int
    tokens: int
    evidence: tuple[Evidence, ...]
    label: labels_module.EpisodeLabel | None


@dataclass(frozen=True)
class NotableMessage:
    line: int
    ts: str
    kind: str | None
    text_preview: str


@dataclass(frozen=True)
class MessageSummary:
    counts: dict[str, int]
    unlabelled: int
    notable: tuple[NotableMessage, ...]


@dataclass(frozen=True)
class ContextPoint:
    line: int
    ts: str
    tokens: int


@dataclass(frozen=True)
class ThreadContext:
    thread_id: str
    model: str | None
    window: int | None
    max: int
    fill_max: float | None


@dataclass(frozen=True)
class ContextDetail:
    window: int | None
    series: tuple[ContextPoint, ...]
    threads: tuple[ThreadContext, ...]
    compactions: tuple[CompactionFact, ...]


@dataclass(frozen=True)
class RewindInfo:
    line: int
    ts: str
    abandoned_calls: int


@dataclass(frozen=True)
class SubagentInfo:
    thread_id: str
    subagent_type: str | None
    description: str | None
    tokens: TokenTotals
    tool_calls: int
    errors: int
    signals: dict[str, int]


@dataclass(frozen=True)
class ToolUse:
    tool: str
    calls: int
    errors: int


@dataclass(frozen=True)
class SkillUse:
    skill: str
    calls: int
    tokens: int


@dataclass(frozen=True)
class SessionDetail:
    agent: str
    session_id: str
    project: str
    folder: str | None
    git_branch: str | None
    transcripts: tuple[TranscriptInfo, ...]
    started_at: str | None
    ended_at: str | None
    outcome: Outcome
    health: HealthScore
    tokens: TokenTotals
    cache: CacheStats
    context: ContextDetail
    rewinds: tuple[RewindInfo, ...]
    subagents: tuple[SubagentInfo, ...]
    tool_usage: tuple[ToolUse, ...]
    skills: tuple[SkillUse, ...]
    signals: dict[str, int]
    subagent_signals: dict[str, int]
    episodes: tuple[EpisodeDetail, ...]
    user_messages: MessageSummary
    issues: tuple[str, ...]


def _evidence(conn: sqlite3.Connection, path: str, start: int, end: int) -> tuple[Evidence, ...]:
    calls = _fetch(
        conn,
        "SELECT line, tool, input_preview, status, error_category, error_text FROM tool_calls"
        " WHERE path = ? AND seq BETWEEN ? AND ? ORDER BY seq",
        (path, start, end),
    )
    ordered = sorted(calls, key=lambda c: c["status"] != "error")[:EVIDENCE_LIMIT]
    return tuple(
        Evidence(
            line=c["line"],
            tool=c["tool"],
            input_preview=c["input_preview"],
            status=c["status"],
            error_category=c["error_category"],
            error_preview=(c["error_text"] or "")[:ERROR_PREVIEW_CHARS] or None,
        )
        for c in ordered
    )


def _episodes(
    conn: sqlite3.Connection,
    agent: str,
    session_id: str,
    threads: Mapping[str, str],
    labels: sqlite3.Connection | None,
) -> tuple[EpisodeDetail, ...]:
    rows = _fetch(
        conn,
        "SELECT * FROM episodes WHERE agent = ? AND session_id = ?"
        " ORDER BY severity DESC, start_ts, episode_id",
        (agent, session_id),
    )
    found = (
        labels_module.episode_labels(labels, [(e["episode_id"], e["content_hash"]) for e in rows])
        if labels is not None
        else {}
    )
    return tuple(
        EpisodeDetail(
            episode_id=e["episode_id"],
            kinds=tuple(e["kinds"].split(",")),
            path=e["path"],
            thread_id=threads.get(e["path"], e["thread_id"]),
            start_line=e["start_line"],
            end_line=e["end_line"],
            start_ts=e["start_ts"],
            end_ts=e["end_ts"],
            calls=e["calls"],
            severity=e["severity"],
            tokens=e["tokens"],
            evidence=_evidence(conn, e["path"], e["start_seq"], e["end_seq"]),
            label=found.get(e["episode_id"]),
        )
        for e in rows
    )


def _message_key(m: sqlite3.Row) -> str:
    return labels_module.message_key(m["t_agent"], m["t_session"], m["dedupe_key"])


def _messages(rows: Sequence[sqlite3.Row], labels: sqlite3.Connection | None) -> MessageSummary:
    human = sorted((m for m in rows if m["origin"] == "human"), key=lambda m: (m["ts"], m["line"]))
    open_ = [m for m in human if not m["rule_kind"] and not m["is_interrupt"]]
    labelled = (
        labels_module.message_kinds(labels, [(_message_key(m), m["content_hash"]) for m in open_])
        if labels is not None
        else {}
    )
    kinds = [(m, m["rule_kind"] or labelled.get(_message_key(m))) for m in human]
    return MessageSummary(
        counts=dict(sorted(Counter(kind for _, kind in kinds if kind).items())),
        unlabelled=sum(1 for m in open_ if _message_key(m) not in labelled),
        notable=tuple(
            NotableMessage(
                line=m["line"],
                ts=m["ts"],
                kind="interrupt" if m["is_interrupt"] else kind,
                text_preview=m["text"][:PREVIEW_CHARS],
            )
            for m, kind in kinds
            if m["is_interrupt"] or kind in NOTABLE_KINDS
        ),
    )


def _compactions(conn: sqlite3.Connection, path: str) -> tuple[CompactionFact, ...]:
    facts = store.facts_from_rows(conn, path)
    return facts.compactions or inferred_compactions(
        tuple(r for r in facts.requests if r.on_main_branch), live_calls(facts)
    )


def _context_detail(
    conn: sqlite3.Connection,
    main: sqlite3.Row,
    transcripts: Mapping[str, sqlite3.Row],
    requests: Sequence[sqlite3.Row],
    flt: Filter,
) -> ContextDetail:
    windows, peaks = _windows_for(conn, flt)
    main_windows = [w for (path, _model), w in windows.items() if path == main["path"]]
    known = [w for w in main_windows if w is not None]
    return ContextDetail(
        window=max(known, default=None if main_windows else DEFAULT_CONTEXT_WINDOW),
        series=tuple(
            ContextPoint(line=r["line"], ts=r["ts"], tokens=r["context_tokens"])
            for r in sorted(requests, key=lambda r: r["line"])
            if r["path"] == main["path"]
        ),
        threads=tuple(
            ThreadContext(
                thread_id=transcripts[path]["thread_id"],
                model=model,
                window=window,
                max=peaks[(path, model)],
                fill_max=_rounded_fill(peaks[(path, model)], window),
            )
            for (path, model), window in sorted(
                windows.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")
            )
        ),
        compactions=_compactions(conn, main["path"]),
    )


def _rounded_fill(tokens: int, window: int | None) -> float | None:
    fill = _fill(tokens, window)
    return None if fill is None else round(fill, 4)


def _skills_detail(
    requests: Sequence[sqlite3.Row], calls: Sequence[sqlite3.Row]
) -> tuple[SkillUse, ...]:
    tokens: dict[str, _Tokens] = defaultdict(_Tokens)
    for r in requests:
        if r["skill"]:
            tokens[r["skill"]].add_request(r)
    uses = Counter(c["skill"] for c in calls if c["skill"])
    rows = [
        SkillUse(skill, uses.get(skill, 0), tokens[skill].total if skill in tokens else 0)
        for skill in sorted({*tokens, *uses})
    ]
    return tuple(sorted(rows, key=lambda r: (-r.tokens, -r.calls, r.skill)))


def _tools_detail(calls: Sequence[sqlite3.Row]) -> tuple[ToolUse, ...]:
    used: Counter[str] = Counter(c["tool"] for c in calls)
    failed: Counter[str] = Counter(c["tool"] for c in calls if _is_error(c))
    return tuple(
        ToolUse(tool, n, failed.get(tool, 0))
        for tool, n in sorted(used.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def _tokens_by_path(requests: Iterable[sqlite3.Row]) -> dict[str, _Tokens]:
    found: dict[str, _Tokens] = defaultdict(_Tokens)
    for r in requests:
        found[r["path"]].add_request(r)
    return found


def _subagents_detail(
    members: Sequence[sqlite3.Row],
    requests: Sequence[sqlite3.Row],
    signals: _SignalsByPath,
) -> tuple[SubagentInfo, ...]:
    tokens = _tokens_by_path(requests)
    return tuple(
        SubagentInfo(
            thread_id=t["thread_id"],
            subagent_type=t["subagent_type"],
            description=t["subagent_description"],
            tokens=(tokens.get(t["path"]) or _Tokens()).totals(),
            tool_calls=t["s_calls"],
            errors=t["s_errors"],
            signals=_kind_counts(signals, [t["path"]]),
        )
        for t in members
        if t["thread_id"] != MAIN_THREAD
    )


def session_signatures(conn: sqlite3.Connection, agent: str, session_id: str) -> frozenset[str]:
    rows = conn.execute(
        "SELECT DISTINCT c.signature FROM tool_calls c JOIN transcripts t USING (path)"
        " WHERE t.agent = ? AND t.session_id = ? AND c.status = 'error'"
        " AND c.signature IS NOT NULL",
        (agent, session_id),
    ).fetchall()
    return frozenset(r[0] for r in rows)


def session_detail(  # noqa: PLR0913, PLR0917 -- the shared interface fixes this signature
    conn: sqlite3.Connection,
    agent: str,
    session_id: str,
    projects: Mapping[tuple[str, str], ProjectInfo],
    now: datetime,
    running: frozenset[tuple[str, str]] | None,
    labels: sqlite3.Connection | None,
    *,
    issues: Sequence[str] = (),
) -> SessionDetail:
    flt = Filter("t.agent = ? AND t.session_id = ?", (agent, session_id))
    members = _fetch(
        conn,
        f"SELECT {_MEMBER_COLUMNS} FROM transcripts t LEFT JOIN thread_stats s USING (path)"
        f" WHERE {flt.where} ORDER BY t.sort_key",
        flt.params,
    )
    if not members:
        raise KeyError(session_id)
    main = _main_thread(members) or members[0]
    requests = list(_deduped(conn, "requests", flt))
    calls = list(_deduped(conn, "tool_calls", flt))
    signals = _signals_by_path(_signal_rows(conn, flt))
    found = _session_health((agent, session_id), main, members, signals, now, running)
    tokens = _Tokens()
    for r in requests:
        tokens.add_request(r)
    by_path = {t["path"]: t for t in members}
    return SessionDetail(
        agent=agent,
        session_id=session_id,
        project=_info(projects, agent, session_id, main["cwd"]).label,
        folder=main["cwd"],
        git_branch=main["git_branch"],
        transcripts=tuple(
            TranscriptInfo(t["path"], t["thread_id"], t["subagent_type"], t["subagent_description"])
            for t in members
        ),
        started_at=found.started_at,
        ended_at=found.ended_at,
        outcome=found.outcome,
        health=found.score,
        tokens=tokens.totals(),
        cache=_cache_from(requests),
        context=_context_detail(conn, main, by_path, requests, flt),
        rewinds=tuple(
            RewindInfo(r["line"], r["ts"], r["abandoned_calls"])
            for r in _fetch(
                conn,
                "SELECT w.* FROM rewinds w JOIN transcripts t USING (path)"
                f" WHERE {flt.where} ORDER BY w.ts, w.line",
                flt.params,
            )
        ),
        subagents=_subagents_detail(members, requests, signals),
        tool_usage=_tools_detail(calls),
        skills=_skills_detail(requests, calls),
        signals=found.signals,
        subagent_signals=found.subagent_signals,
        episodes=_episodes(
            conn, agent, session_id, {p: t["thread_id"] for p, t in by_path.items()}, labels
        ),
        user_messages=_messages(list(_deduped(conn, "user_messages", flt)), labels),
        issues=tuple(issues),
    )
