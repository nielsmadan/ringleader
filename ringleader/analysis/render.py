from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime
from typing import Any

from ..agents import display_agent
from .facts import CompactionFact
from .health import HealthScore
from .ingest import IngestStats
from .issues import Issue
from .labels import EpisodeLabel, LabelOutcome
from .reports import ReportMeta
from .rollups import (
    SCHEMA_VERSION,
    CacheStats,
    ContextDetail,
    ContextStats,
    Dimension,
    EpisodeDetail,
    Evidence,
    MessageSummary,
    RollupRow,
    Scope,
    SessionDetail,
    SessionSummary,
    TokenTotals,
)
from .taxonomy import printable

SOURCES = ("claude", "codex", "droid", "opencode", "pi")
ROW_LIMIT = 10
SHORT_ID = 8
TOP_PENALTIES = 2
SUMMARY_CHARS = 160


def tokens_json(t: TokenTotals) -> dict[str, Any]:
    return {**asdict(t), "cost": None if t.cost is None else round(t.cost, 4)}


def cache_json(c: CacheStats) -> dict[str, Any]:
    return {
        **asdict(c),
        "hit_rate": None if c.hit_rate is None else round(c.hit_rate, 4),
        "rebuild_cost": None if c.rebuild_cost is None else round(c.rebuild_cost, 4),
    }


def context_json(c: ContextStats) -> dict[str, Any]:
    return asdict(c)


def health_json(h: HealthScore) -> dict[str, Any]:
    return {
        "score": h.score,
        "points": h.points,
        "per_100_calls": round(h.per_100_calls, 2),
        "penalties": [asdict(p) for p in h.penalties],
    }


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def row_json(row: RollupRow) -> dict[str, Any]:
    return {
        "key": row.key,
        "sessions": row.sessions,
        "calls": row.calls,
        "errors": row.errors,
        "error_rate": _rate(row.errors, row.calls),
        "tokens": tokens_json(row.tokens) if row.tokens else None,
        "cache_hit_rate": _rounded(row.cache_hit_rate),
        "context": context_json(row.context) if row.context else None,
    }


def totals_json(row: RollupRow) -> dict[str, Any]:
    shaped = {k: v for k, v in row_json(row).items() if k != "key"}
    return {**shaped, "requests": row.requests}


def session_json(s: SessionSummary) -> dict[str, Any]:
    return {
        "agent": s.agent,
        "session_id": s.session_id,
        "project": s.project,
        "folder": s.folder,
        "started_at": s.started_at,
        "ended_at": s.ended_at,
        "outcome": s.outcome,
        "health": health_json(s.health),
        "signals": s.signals,
        "subagent_signals": s.subagent_signals,
        "tool_calls": s.tool_calls,
        "tool_errors": s.tool_errors,
        "episode_tokens": s.episode_tokens,
        "tokens": tokens_json(s.tokens),
    }


def _index_json(index: IngestStats) -> dict[str, Any]:
    return {
        "path": index.path,
        "facts_version": index.facts_version,
        "derive_version": index.derive_version,
        "transcripts": index.transcripts,
        "attempted": index.attempted,
        "changed": index.changed,
        "removed": index.removed,
        "failed": index.failed,
        "failed_now": index.failed_now,
        "rederived": index.rederived,
        "complete": index.complete,
        "stale": index.stale,
        "writable": index.writable,
        "seconds": round(index.seconds, 3),
    }


def issue_json(issue: Issue) -> dict[str, Any]:
    return {
        "issue_id": issue.issue_id,
        "name": issue.name,
        "summary": issue.summary,
        "signatures": list(issue.signatures),
        "sessions": issue.sessions,
        "occurrences": issue.occurrences,
        "agents": list(issue.agents),
        "first_seen": issue.first_seen,
        "last_seen": issue.last_seen,
        "example_sessions": [{"agent": a, "session_id": s} for a, s in issue.example_sessions],
        "example_text": issue.example_text,
    }


def embed_json(used: bool, reason: str | None) -> dict[str, Any]:
    return {"used": used, "reason": reason}


def labels_json(  # noqa: PLR0913 -- one field per documented key
    *,
    engine: str | None,
    model: str | None,
    labelled: int,
    pending: int,
    note: str | None,
    scope: str,
    outcome: LabelOutcome | None,
) -> dict[str, Any]:
    return {
        "engine": engine,
        "model": model,
        "labelled": labelled,
        "pending": pending,
        "note": note,
        "scope": scope,
        "run": None
        if outcome is None
        else {
            "episodes": outcome.episodes,
            "messages": outcome.messages,
            "issues": outcome.issues,
            "failed": outcome.failed,
            "tripped": outcome.tripped,
            "errors": list(outcome.errors),
        },
    }


def reports_json(found: Sequence[ReportMeta]) -> dict[str, Any]:
    return {"schema": SCHEMA_VERSION, "reports": [report_json(m) for m in found]}


def saved_report_json(meta: ReportMeta) -> dict[str, Any]:
    return {"schema": SCHEMA_VERSION, "report_id": meta.report_id, "path": str(meta.path)}


def report_json(meta: ReportMeta) -> dict[str, Any]:
    return {
        "report_id": meta.report_id,
        "kind": meta.kind,
        "created": meta.created,
        "title": meta.title,
        "window": meta.window,
        "based_on": list(meta.based_on),
        "path": str(meta.path),
    }


def _label_json(label: EpisodeLabel | None) -> dict[str, str] | None:
    return None if label is None else asdict(label)


def _evidence_json(e: Evidence) -> dict[str, Any]:
    return asdict(e)


def _episode_json(e: EpisodeDetail) -> dict[str, Any]:
    return {
        "episode_id": e.episode_id,
        "kinds": list(e.kinds),
        "path": e.path,
        "thread_id": e.thread_id,
        "start_line": e.start_line,
        "end_line": e.end_line,
        "start_ts": e.start_ts,
        "end_ts": e.end_ts,
        "calls": e.calls,
        "severity": e.severity,
        "tokens": e.tokens,
        "evidence": [_evidence_json(x) for x in e.evidence],
        "label": _label_json(e.label),
    }


def _compaction_json(c: CompactionFact) -> dict[str, Any]:
    return {
        "ts": c.timestamp,
        "trigger": c.trigger,
        "pre_tokens": c.pre_tokens,
        "post_tokens": c.post_tokens,
    }


def _context_detail_json(c: ContextDetail) -> dict[str, Any]:
    return {
        "window": c.window,
        "series": [asdict(p) for p in c.series],
        "threads": [asdict(t) for t in c.threads],
        "compactions": [_compaction_json(x) for x in c.compactions],
    }


def _messages_json(m: MessageSummary) -> dict[str, Any]:
    return {
        "counts": dict(m.counts),
        "unlabelled": m.unlabelled,
        "notable": [asdict(n) for n in m.notable],
    }


def session_detail_json(
    d: SessionDetail, labels: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "agent": d.agent,
        "session_id": d.session_id,
        "project": d.project,
        "folder": d.folder,
        "git_branch": d.git_branch,
        "transcripts": [
            {
                "path": t.path,
                "thread_id": t.thread_id,
                "subagent_type": t.subagent_type,
                "description": t.description,
            }
            for t in d.transcripts
        ],
        "started_at": d.started_at,
        "ended_at": d.ended_at,
        "outcome": d.outcome,
        "health": health_json(d.health),
        "tokens": tokens_json(d.tokens),
        "cache": cache_json(d.cache),
        "context": _context_detail_json(d.context),
        "rewinds": [
            {"line": r.line, "ts": r.ts, "abandoned_calls": r.abandoned_calls} for r in d.rewinds
        ],
        "subagents": [
            {
                "thread_id": s.thread_id,
                "subagent_type": s.subagent_type,
                "description": s.description,
                "tokens": tokens_json(s.tokens),
                "tool_calls": s.tool_calls,
                "errors": s.errors,
                "signals": dict(s.signals),
            }
            for s in d.subagents
        ],
        "tool_usage": [asdict(t) for t in d.tool_usage],
        "skills": [asdict(k) for k in d.skills],
        "signals": dict(d.signals),
        "subagent_signals": dict(d.subagent_signals),
        "episodes": [_episode_json(e) for e in d.episodes],
        "user_messages": _messages_json(d.user_messages),
        "issues": list(d.issues),
    }
    if labels is not None:
        doc["labels"] = dict(labels)
    return doc


def issues_json(
    *, scope: Scope, index: IngestStats, embed: Mapping[str, Any], issues: Sequence[Issue]
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "since": scope.since.isoformat() if scope.since else None,
        "until": scope.until.isoformat() if scope.until else None,
        "index": _index_json(index),
        "embed": dict(embed),
        "issues": [issue_json(i) for i in issues],
    }


def corpus_json(  # noqa: PLR0913 -- the shared interface fixes this signature
    *,
    scope: Scope,
    generated_at: datetime,
    coverage: Mapping[str, str],
    index: IngestStats,
    totals: RollupRow,
    cache: CacheStats,
    rollups: Mapping[Dimension, Sequence[RollupRow]],
    worst: Sequence[SessionSummary],
    issues: Sequence[Mapping[str, Any]],
    labels: Mapping[str, Any] | None,
    embed: Mapping[str, Any] | None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "since": scope.since.isoformat() if scope.since else None,
        "until": scope.until.isoformat() if scope.until else None,
        "sources": sorted(display_agent(a) for a in scope.agents) or list(SOURCES),
        "selectors": list(scope.selectors),
        "coverage": dict(coverage),
        "index": _index_json(index),
        "totals": totals_json(totals),
        "cache": cache_json(cache),
        "rollups": {dim: [row_json(r) for r in rows] for dim, rows in rollups.items()},
        "worst_sessions": [session_json(s) for s in worst],
        "issues": [dict(i) for i in issues],
        "embed": None if embed is None else dict(embed),
    }
    if labels is not None:
        doc["labels"] = dict(labels)
    return doc


def _count(n: int) -> str:
    return f"{n:,}"


def _window(tokens: int | None) -> str:
    return "unknown" if tokens is None else _count(tokens)


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _cost(tokens: Mapping[str, Any] | None) -> str:
    if not tokens or tokens["cost"] is None:
        return ""
    return f"~${tokens['cost']:,.2f}" + ("*" if tokens["cost_partial"] else "")


def _row_line(row: Mapping[str, Any]) -> str:
    tokens = row["tokens"]
    cells = [
        f"{_count(tokens['total']):>14}" if tokens else f"{'':>14}",
        f"{_count(row['calls']):>8}",
        f"{_count(row['errors']):>6}",
        f"{_percent(row['cache_hit_rate']):>5}",
        f"{_cost(tokens):>10}",
    ]
    return f"    {'  '.join(cells)}  {printable(row['key'])}"


def _rollup_block(dim: str, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    head = f"    {'tokens':>14}  {'calls':>8}  {'errors':>6}  {'cache':>5}  {'cost':>10}  {dim}"
    lines = [f"  by {dim}", head, *(_row_line(r) for r in rows[:ROW_LIMIT])]
    if len(rows) > ROW_LIMIT:
        lines.append(f"    ... {len(rows) - ROW_LIMIT} more")
    return lines


def _top_penalties(health: Mapping[str, Any]) -> str:
    return ", ".join(f"{p['signal']} -{p['points']}" for p in health["penalties"][:TOP_PENALTIES])


def _worst_line(s: Mapping[str, Any]) -> str:
    h = s["health"]
    return (
        f"    {h['score']:>3}  {h['per_100_calls']:>9.1f}  {s['outcome']:<9}"
        f"  {s['agent']}:{printable(s['session_id'][:SHORT_ID])}  {printable(s['project'])}"
        f"  {_top_penalties(h)}"
    ).rstrip()


def _index_note(index: Mapping[str, Any]) -> str:
    notes = [
        f"{_count(index['transcripts'])} transcripts",
        f"{index['changed']} changed",
        f"{index['failed']} failed",
    ]
    if index["stale"]:
        notes.append("stale")
    if not index["writable"]:
        notes.append("not writable")
    if not index["complete"]:
        notes.append("incomplete")
    return ", ".join(notes)


def corpus_text(doc: Mapping[str, Any]) -> str:
    totals, cache = doc["totals"], doc["cache"]
    window = f"{doc['since'] or 'start'} .. {doc['until'] or 'latest'}"
    lines = [
        f"analysis - {', '.join(doc['sources'])} - {window}",
        f"  index: {_index_note(doc['index'])}",
        (
            f"  {_count(totals['sessions'])} sessions, {_count(totals['requests'])} requests, "
            f"{_count(totals['calls'])} tool calls, {_count(totals['errors'])} errors "
            f"({_percent(totals['error_rate'])})"
        ),
    ]
    if totals["tokens"]:
        lines.append(f"  {_count(totals['tokens']['total'])} tokens  {_cost(totals['tokens'])}")
    lines.append(
        f"  cache hit {_percent(cache['hit_rate'])}, {_count(cache['rebuilds'])} rebuilds "
        f"({_count(cache['rebuilds_after_5m'])} after 5m idle, "
        f"{_count(cache['rebuilds_after_1h'])} after 1h)"
    )
    if totals["context"]:
        c = totals["context"]
        lines.append(
            f"  context p50 {_count(c['p50'])}  p95 {_count(c['p95'])}  max {_count(c['max'])}"
            f"  compactions {_count(c['compactions'])}"
        )
    for dim, rows in doc["rollups"].items():
        lines += ["", *_rollup_block(dim, rows)]
    if doc["worst_sessions"]:
        lines += ["", "  worst sessions", f"    {'score':>3}  {'pts/100':>9}  outcome"]
        lines += [_worst_line(s) for s in doc["worst_sessions"]]
    if doc.get("issues"):
        lines += ["", *_issue_lines(doc["issues"])]
    if doc.get("labels"):
        lines += ["", *_label_lines(doc["labels"])]
    return "\n".join(lines)


def _label_lines(summary: Mapping[str, Any]) -> list[str]:
    lines = [f"  labels: {summary['labelled']} episodes labelled, {summary['pending']} not"]
    if summary["note"]:
        lines.append(f"    {printable(summary['note'])}")
    return lines


def _penalty_lines(health: Mapping[str, Any]) -> list[str]:
    return [f"    -{p['points']:<3} {p['signal']} x{p['count']}" for p in health["penalties"]]


def _episode_lines(episode: Mapping[str, Any]) -> list[str]:
    label = episode["label"]
    tag = f"  [{printable(label['category'])}: {printable(label['cause'])}]" if label else ""
    head = (
        f"    {episode['episode_id'][:SHORT_ID]}  {','.join(episode['kinds'])}  "
        f"lines {episode['start_line']}-{episode['end_line']}  {episode['calls']} calls  "
        f"{_count(episode['tokens'])} tokens{tag}"
    )
    evidence = [
        f"      {e['line']:>6}  {e['status']:<7} {printable(e['tool'])}"
        f"  {printable(e['input_preview'])[:80]}"
        for e in episode["evidence"][: TOP_PENALTIES + 1]
    ]
    return [head, *evidence]


def session_text(doc: Mapping[str, Any]) -> str:
    health = doc["health"]
    tokens, context = doc["tokens"], doc["context"]
    lines = [
        f"session {doc['agent']}:{printable(doc['session_id'])}",
        (
            f"  {printable(doc['project'])}  {printable(doc['folder'] or '')}"
            f"  {printable(doc['git_branch'] or '')}"
        ).rstrip(),
        f"  {doc['started_at']} .. {doc['ended_at']}  outcome {doc['outcome']}",
        f"  health {health['score']}/100  {health['per_100_calls']:.1f} points per 100 calls",
        *_penalty_lines(health),
        (
            f"  {_count(tokens['total'])} tokens  {_cost(tokens)}  cache hit "
            f"{_percent(doc['cache']['hit_rate'])}"
        ),
        (
            f"  context window {_window(context['window'])}, "
            f"{len(context['compactions'])} compactions, {len(doc['rewinds'])} rewinds"
        ),
    ]
    if doc["subagents"]:
        lines += ["", "  subagents"]
        lines += [
            f"    {printable(s['subagent_type'] or s['thread_id'])}"
            f"  {_count(s['tokens']['total'])} tokens"
            f"  {s['tool_calls']} calls  {s['errors']} errors"
            for s in doc["subagents"]
        ]
    if doc["tool_usage"]:
        lines += ["", "  tools"]
        lines += [
            f"    {printable(t['tool']):<20} {_count(t['calls']):>8} calls"
            f"  {_count(t['errors']):>6} errors"
            for t in doc["tool_usage"][:ROW_LIMIT]
        ]
    if doc["episodes"]:
        lines += ["", "  episodes"]
        lines += [line for e in doc["episodes"] for line in _episode_lines(e)]
    messages = doc["user_messages"]
    if messages["notable"]:
        lines += ["", "  notable messages"]
        lines += [
            f"    {m['line']:>6}  {m['kind']:<12} {printable(m['text_preview'])[:SUMMARY_CHARS]}"
            for m in messages["notable"]
        ]
    if doc.get("labels"):
        lines += ["", *_label_lines(doc["labels"])]
    return "\n".join(lines)


def _issue_lines(issues: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = ["  recurring issues"]
    for issue in issues:
        title = printable(issue.get("name") or issue["signatures"][0])
        lines.append(
            f"    {issue['sessions']:>4} sessions  {issue['occurrences']:>5} occurrences  {title}"
        )
        if issue.get("summary"):
            lines.append(f"        {printable(issue['summary'])}")
    return lines


def issues_text(doc: Mapping[str, Any]) -> str:
    issues = doc.get("issues") or []
    lines = [line.removeprefix("  ") for line in _issue_lines(issues)] if issues else []
    body = "\n".join(lines) if lines else "no recurring issues"
    reason = (doc.get("embed") or {}).get("reason")
    return f"{body}\nexact signatures only: {printable(reason)}" if reason else body
