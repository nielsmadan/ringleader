from __future__ import annotations

import math
import sqlite3
import threading
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from .. import enrichment
from ..models import with_cancel
from ..models.base import Model, ModelError
from ..usage.pricing import PRICES, model_family
from . import store, taxonomy
from .episodes import Episode
from .facts import MessageKind
from .scoping import days_clause, scope_filter

if TYPE_CHECKING:
    from .issues import Issue
    from .rollups import ProjectInfo, Scope

LABEL_PROMPT_VERSION = 1
EPISODE_CATEGORIES = ("legit_iteration", "detour", "loop", "environment", "misunderstanding")
FRICTION_TAGS = (
    "misunderstood_request",
    "wrong_approach",
    "buggy_code",
    "user_rejected_action",
    "excessive_changes",
    "none",
)
MESSAGE_KINDS: tuple[MessageKind, ...] = (
    "correction",
    "frustration",
    "redirection",
    "new_task",
    "approval",
    "other",
)
EPISODE_BATCH = 4
MESSAGE_BATCH = 75
DEFAULT_LABEL_LIMIT = 50
MESSAGE_LABEL_LIMIT = 500
ISSUE_NAME_LIMIT = 20
RECENT_DAYS = 7
MAX_ATTEMPTS = 2
ESTIMATED_PROMPT_TOKENS: dict[str, int] = {"episodes": 3000, "messages": 6000, "issue": 1500}
CAUSE_CHARS = 140
NAME_CHARS = 80
SUMMARY_CHARS = 240
MESSAGE_TEXT_CHARS = 600
NON_INTERACTIVE_NOTE = "labels skipped (non-interactive); pass --label recent to label"
JSON_NOTE = "labels skipped (--json); pass --label recent to label"
NO_MODEL_NOTE = "no model configured — run rl config set ai …; labels skipped"

type LabelScope = Literal["recent", "all", "none"]
type JobKind = Literal["episodes", "messages", "issue"]
LABEL_SCOPES: tuple[LabelScope, ...] = ("recent", "all", "none")

CATEGORY_MEANINGS: dict[str, str] = {
    "legit_iteration": "ordinary trial and error that made progress toward the request.",
    "detour": "the agent went down a side path the request did not need before getting back on track.",
    "loop": "the agent repeated the same or near-same action without new information or progress.",
    "environment": "the tools, sandbox, network, permissions or setup failed, not the agent's reasoning.",
    "misunderstanding": "the agent misread the request or the code and worked toward the wrong goal.",
}
FRICTION_MEANINGS: dict[str, str] = {
    "misunderstood_request": "the user's request was misread.",
    "wrong_approach": "the goal was right but the chosen approach was not.",
    "buggy_code": "the agent wrote code that did not work and had to fix it.",
    "user_rejected_action": "the user declined or interrupted an action the agent tried.",
    "excessive_changes": "the agent changed more than the request called for.",
    "none": "nothing here cost the user time or attention.",
}
KIND_MEANINGS: dict[str, str] = {
    "correction": "the user says the agent got something wrong and states what is right.",
    "frustration": "the user expresses annoyance or impatience with the agent.",
    "redirection": "the user changes the agent's course on the current task.",
    "new_task": "the user starts a new, unrelated piece of work.",
    "approval": "the user accepts or confirms what the agent did or proposed.",
    "other": "anything else, such as a question, extra context or a plain instruction.",
}

EPISODE_LABEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "category": {"type": "string", "enum": list(EPISODE_CATEGORIES)},
                    "friction": {"type": "string", "enum": list(FRICTION_TAGS)},
                    "cause": {"type": "string"},
                },
                "required": ["id", "category", "friction", "cause"],
            },
        }
    },
    "required": ["labels"],
}
MESSAGE_LABEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string", "enum": list(MESSAGE_KINDS)},
                },
                "required": ["id", "kind"],
            },
        }
    },
    "required": ["labels"],
}
ISSUE_NAME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "summary": {"type": "string"}},
    "required": ["name", "summary"],
}
_EPISODE_ALLOWED: dict[str, Collection[str]] = {
    "category": EPISODE_CATEGORIES,
    "friction": FRICTION_TAGS,
}
_MESSAGE_ALLOWED: dict[str, Collection[str]] = {"kind": MESSAGE_KINDS}


@dataclass(frozen=True)
class LabelPlan:
    episodes: tuple[Episode, ...]
    messages: tuple[tuple[str, str, str], ...]
    issues: tuple[Issue, ...]
    calls: int
    est_seconds: float
    est_cost: float | None


@dataclass(frozen=True)
class LabelOutcome:
    episodes: int
    messages: int
    issues: int
    failed: int
    errors: tuple[str, ...]
    tripped: str | None


class _Halted(Exception):
    pass


@dataclass(frozen=True)
class _Job:
    kind: JobKind
    ids: tuple[str, ...]
    attempt: int


def default_label_scope(
    *,
    explicit: LabelScope | None,
    json_output: bool,
    stdin_tty: bool,
    stdout_tty: bool,
    stderr_tty: bool,
    current_session: tuple[str, str] | None,
) -> tuple[LabelScope, str | None]:
    if explicit is not None:
        return explicit, None
    if json_output:
        return "none", JSON_NOTE
    if stdin_tty and stdout_tty and stderr_tty and current_session is None:
        return "recent", None
    return "none", NON_INTERACTIVE_NOTE


def message_key(agent: str, session_id: str, dedupe_key: str) -> str:
    return f"{agent}:{session_id}:{dedupe_key}"


def _choices(values: Sequence[str], meanings: Mapping[str, str]) -> str:
    return "\n".join(f"- {v}: {meanings[v]}" for v in values)


def episode_prompt(batch: Sequence[Episode]) -> str:
    ids = ", ".join(e.episode_id for e in batch)
    blocks = "\n\n".join(
        f"id: {e.episode_id}\nsignals: {', '.join(e.kinds)}\ncalls: {e.calls}\n{e.excerpt}"
        for e in batch
    )
    return (
        "You label stretches of a coding-agent session that a detector flagged as possible "
        "trouble. Each episode below is the user's last message and the tool calls in the "
        "stretch, with their status and errors.\n\n"
        f"category, pick one:\n{_choices(EPISODE_CATEGORIES, CATEGORY_MEANINGS)}\n\n"
        f"friction, pick one:\n{_choices(FRICTION_TAGS, FRICTION_MEANINGS)}\n\n"
        f"cause: one sentence of at most {CAUSE_CHARS} characters naming the likely cause.\n\n"
        'Return JSON {"labels": [{"id", "category", "friction", "cause"}]} with one entry for '
        f"each of these ids and no others: {ids}\n\nEpisodes:\n\n{blocks}"
    )


def message_prompt(batch: Sequence[tuple[str, str]]) -> str:
    ids = ", ".join(ident for ident, _ in batch)
    blocks = "\n\n".join(f"id: {ident}\n{text[:MESSAGE_TEXT_CHARS]}" for ident, text in batch)
    return (
        "You classify messages a user typed to a coding agent mid-session.\n\n"
        f"kind, pick one:\n{_choices(MESSAGE_KINDS, KIND_MEANINGS)}\n\n"
        'Return JSON {"labels": [{"id", "kind"}]} with one entry for each of these ids and '
        f"no others: {ids}\n\nMessages:\n\n{blocks}"
    )


def issue_prompt(issue: Issue) -> str:
    signatures = "\n".join(f"- {s}" for s in issue.signatures)
    return (
        "You name a recurring tool error seen across several coding-agent sessions.\n\n"
        f"Normalized error signatures (tool|category|message):\n{signatures}\n"
        f"Seen in {issue.sessions} sessions, {issue.occurrences} times, "
        f"agents: {', '.join(issue.agents)}.\n"
        f"Example error text:\n{issue.example_text}\n\n"
        'Return JSON {"name", "summary"}: name is 3 to 6 words naming the problem, '
        "summary is one sentence saying what goes wrong."
    )


def _plain(text: str, limit: int) -> str:
    return taxonomy.printable(text)[:limit]


def parse_batch(
    raw: object, wanted: Collection[str], allowed: Mapping[str, Collection[str]]
) -> dict[str, dict[str, str]]:
    items = raw.get("labels") if isinstance(raw, Mapping) else None
    found: dict[str, dict[str, str]] = {}
    for item in items if isinstance(items, list) else []:
        ident = item.get("id") if isinstance(item, dict) else None
        if not isinstance(ident, str) or ident not in wanted or ident in found:
            continue
        values = {k: v.strip() for k, v in item.items() if k != "id" and isinstance(v, str)}
        if all(values.get(field) in options for field, options in allowed.items()):
            found[ident] = values
    return found


def _issue_name(raw: object) -> tuple[str, str] | None:
    if not isinstance(raw, Mapping):
        return None
    name, summary = raw.get("name"), raw.get("summary")
    if isinstance(name, str) and isinstance(summary, str):
        clean = (_plain(name, NAME_CHARS), _plain(summary, SUMMARY_CHARS))
        return clean if all(clean) else None
    return None


def _fetch(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    try:
        return cursor.execute(sql, params).fetchall()
    finally:
        cursor.close()


def _current_keys(labels: sqlite3.Connection, table: str, key: str) -> set[tuple[str, str]]:
    rows = _fetch(
        labels,
        f"SELECT {key}, content_hash FROM {table} WHERE prompt_version = ?",
        (LABEL_PROMPT_VERSION,),
    )
    return {(r[0], r[1]) for r in rows}


def _episode_where(
    index: sqlite3.Connection,
    scope: Scope,
    projects: Mapping[tuple[str, str], ProjectInfo] | None,
    session: tuple[str, str] | None,
) -> tuple[str, tuple[Any, ...]]:
    if session is not None:
        return "e.agent = ? AND e.session_id = ?", session
    return days_clause(scope, "e.day", scope_filter(index, scope, projects))


def _episode_rows(
    index: sqlite3.Connection,
    scope: Scope,
    projects: Mapping[tuple[str, str], ProjectInfo] | None,
    session: tuple[str, str] | None,
) -> list[sqlite3.Row]:
    where, params = _episode_where(index, scope, projects, session)
    return _fetch(
        index,
        "SELECT e.* FROM episodes e JOIN transcripts t USING (path)"
        f" WHERE {where} ORDER BY e.severity DESC, e.start_ts DESC, e.episode_id",
        params,
    )


def _recent(rows: Sequence[sqlite3.Row], scope: Scope, now: datetime) -> list[sqlite3.Row]:
    if not rows:
        return []
    latest = min(scope.until or now.date(), max(date.fromisoformat(r["day"]) for r in rows))
    first = (latest - timedelta(days=RECENT_DAYS - 1)).isoformat()
    return [r for r in rows if r["day"] >= first]


def _messages(
    index: sqlite3.Connection,
    owners: Collection[tuple[str, str]],
    done: Collection[tuple[str, str]],
    limit: int | None,
) -> tuple[tuple[str, str, str], ...]:
    if not owners:
        return ()

    def owned(agent: str, session_id: str) -> int:
        return int((agent, session_id) in owners)

    name = f"rl_owner_{id(owned)}"
    index.create_function(name, 2, owned, deterministic=True)
    rows = _fetch(
        index,
        "SELECT * FROM (SELECT t.agent, t.session_id, m.dedupe_key, m.content_hash, m.text,"
        " m.ts, ROW_NUMBER() OVER (PARTITION BY t.agent, t.session_id, m.dedupe_key"
        " ORDER BY t.sort_key, m.line) AS rn FROM user_messages m JOIN transcripts t"
        " USING (path) WHERE m.origin = 'human' AND m.rule_kind IS NULL"
        f" AND m.is_interrupt = 0 AND {name}(t.agent, t.session_id))"
        " WHERE rn = 1 ORDER BY ts DESC, dedupe_key",
    )
    keyed = (
        (message_key(r["agent"], r["session_id"], r["dedupe_key"]), r["content_hash"], r["text"])
        for r in rows
    )
    fresh = tuple(m for m in keyed if (m[0], m[1]) not in done)
    return fresh if limit is None else fresh[:limit]


def _estimate_cost(calls: Mapping[str, int], model_name: str | None) -> float | None:
    family = model_family(model_name)
    if family is None:
        return None
    tokens = sum(ESTIMATED_PROMPT_TOKENS[kind] * n for kind, n in calls.items())
    return tokens * PRICES[family][0]


def plan_labels(  # noqa: PLR0913 -- the shared interface fixes these inputs
    index: sqlite3.Connection,
    labels: sqlite3.Connection,
    scope: Scope,
    *,
    choice: LabelScope,
    issues: Sequence[Issue],
    now: datetime,
    engine: str | None,
    model_name: str | None,
    concurrency: int,
    projects: Mapping[tuple[str, str], ProjectInfo] | None = None,
    session: tuple[str, str] | None = None,
) -> LabelPlan:
    capped = choice == "recent" and session is None
    found = [] if choice == "none" else _episode_rows(index, scope, projects, session)
    rows = _recent(found, scope, now) if capped else found
    labelled = _current_keys(labels, "episode_labels", "episode_id")
    open_rows = [r for r in rows if (r["episode_id"], r["content_hash"]) not in labelled]
    chosen_rows = open_rows[:DEFAULT_LABEL_LIMIT] if capped else open_rows
    planned = tuple(store.episode_from_row(r) for r in chosen_rows)
    owners = {(e.agent, e.session_id) for e in planned} | {
        (r["agent"], r["session_id"])
        for r in rows
        if (r["episode_id"], r["content_hash"]) in labelled
    }
    uncapped = session is not None or choice == "all"
    messages = _messages(
        index,
        owners,
        _current_keys(labels, "message_labels", "message_key"),
        None if uncapped else MESSAGE_LABEL_LIMIT,
    )
    named = {issue_id for issue_id, _ in _current_keys(labels, "issue_names", "issue_id")}
    unnamed = tuple(i for i in issues if i.issue_id not in named)
    chosen = () if choice == "none" else unnamed if uncapped else unnamed[:ISSUE_NAME_LIMIT]
    calls = {
        "episodes": math.ceil(len(planned) / EPISODE_BATCH),
        "messages": math.ceil(len(messages) / MESSAGE_BATCH),
        "issue": len(chosen),
    }
    total = sum(calls.values())
    return LabelPlan(
        episodes=planned,
        messages=messages,
        issues=chosen,
        calls=total,
        est_seconds=enrichment.estimate_seconds(total, engine, concurrency),
        est_cost=_estimate_cost(calls, model_name),
    )


def _chunks(ids: Sequence[str], size: int) -> list[tuple[str, ...]]:
    return [tuple(ids[i : i + size]) for i in range(0, len(ids), size)]


@dataclass(frozen=True)
class _Work:
    episodes: Mapping[str, Episode]
    messages: Mapping[str, tuple[str, str, str]]
    issues: Mapping[str, Issue]


def _request(job: _Job, work: _Work) -> tuple[str, dict[str, Any]]:
    if job.kind == "episodes":
        return episode_prompt([work.episodes[i] for i in job.ids]), EPISODE_LABEL_SCHEMA
    if job.kind == "messages":
        return message_prompt([(i, work.messages[i][2]) for i in job.ids]), MESSAGE_LABEL_SCHEMA
    return issue_prompt(work.issues[job.ids[0]]), ISSUE_NAME_SCHEMA


def _parsed(job: _Job, raw: object) -> dict[str, dict[str, str]]:
    if job.kind == "episodes":
        return parse_batch(raw, job.ids, _EPISODE_ALLOWED)
    if job.kind == "messages":
        return parse_batch(raw, job.ids, _MESSAGE_ALLOWED)
    named = _issue_name(raw)
    return {} if named is None else {job.ids[0]: {"name": named[0], "summary": named[1]}}


@dataclass(frozen=True)
class _Stamp:
    engine: str
    model: str | None
    at: str


def _save(
    labels: sqlite3.Connection,
    job: _Job,
    found: Mapping[str, Mapping[str, str]],
    work: _Work,
    stamp: _Stamp,
) -> None:
    tail = (stamp.engine, stamp.model, stamp.at)
    with store.transaction(labels):
        if job.kind == "episodes":
            labels.executemany(
                "INSERT OR REPLACE INTO episode_labels (episode_id, content_hash, prompt_version,"
                " category, friction, cause, engine, model, labelled_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        ident,
                        work.episodes[ident].content_hash,
                        LABEL_PROMPT_VERSION,
                        v["category"],
                        v["friction"],
                        _plain(v.get("cause", ""), CAUSE_CHARS),
                        *tail,
                    )
                    for ident, v in found.items()
                ],
            )
        elif job.kind == "messages":
            labels.executemany(
                "INSERT OR REPLACE INTO message_labels (message_key, content_hash, prompt_version,"
                " kind, engine, model, labelled_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        work.messages[ident][0],
                        work.messages[ident][1],
                        LABEL_PROMPT_VERSION,
                        v["kind"],
                        *tail,
                    )
                    for ident, v in found.items()
                ],
            )
        else:
            labels.executemany(
                "INSERT OR REPLACE INTO issue_names (issue_id, content_hash, prompt_version, name,"
                " summary, engine, model, named_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        ident,
                        work.issues[ident].content_hash,
                        LABEL_PROMPT_VERSION,
                        v["name"],
                        v["summary"],
                        *tail,
                    )
                    for ident, v in found.items()
                ],
            )


def _jobs(work: _Work) -> list[_Job]:
    return [
        *(_Job("episodes", ids, 1) for ids in _chunks(list(work.episodes), EPISODE_BATCH)),
        *(_Job("messages", ids, 1) for ids in _chunks(list(work.messages), MESSAGE_BATCH)),
        *(_Job("issue", (ident,), 1) for ident in work.issues),
    ]


def run_labels(
    model: Model,
    plan: LabelPlan,
    labels: sqlite3.Connection,
    now: datetime,
    stop: Callable[[], bool],
    progress: Callable[[str, int, int], None] | None = None,
) -> LabelOutcome:
    work = _Work(
        episodes={e.episode_id: e for e in plan.episodes},
        messages={f"m{i}": m for i, m in enumerate(plan.messages)},
        issues={i.issue_id: i for i in plan.issues},
    )
    stamp = _Stamp(model.name, model.model, now.isoformat())
    total = len(work.episodes) + len(work.messages) + len(work.issues)
    breaker = enrichment.Breaker()
    aborted = threading.Event()

    def halted() -> bool:
        return breaker.tripped.is_set() or aborted.is_set() or stop()

    cancellable = with_cancel(model, halted)

    def call(job: _Job) -> dict[str, Any]:
        if halted():
            raise _Halted
        prompt, schema = _request(job, work)
        try:
            raw = cancellable.complete_json(prompt, schema)
        except ModelError as error:
            if halted():
                raise _Halted from error
            breaker.record_failure(error)
            raise
        breaker.record_success()
        return raw

    stored: Counter[str] = Counter()
    errors: list[str] = []
    failed = 0
    executor = ThreadPoolExecutor(max_workers=enrichment.default_concurrency(model))
    pending: dict[Future[dict[str, Any]], _Job] = {}

    def submit(job: _Job) -> None:
        pending[executor.submit(call, job)] = job

    try:
        for job in _jobs(work):
            submit(job)
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                job = pending.pop(future)
                error = None if future.cancelled() else future.exception()
                if future.cancelled() or isinstance(error, _Halted):
                    continue
                if isinstance(error, ModelError):
                    failed += len(job.ids)
                    errors.append(f"{job.kind}: {error}")
                    continue
                if error is not None:
                    raise error
                found = _parsed(job, future.result())
                if found:
                    _save(labels, job, found, work, stamp)
                    stored[job.kind] += len(found)
                missing = tuple(i for i in job.ids if i not in found)
                if missing and job.attempt < MAX_ATTEMPTS and not halted():
                    submit(_Job(job.kind, missing, job.attempt + 1))
                elif missing:
                    failed += len(missing)
                    errors.append(f"{job.kind}: no valid label for {len(missing)}")
                if progress is not None:
                    progress(job.kind, sum(stored.values()) + failed, total)
            if halted():
                executor.shutdown(wait=False, cancel_futures=True)
                for future in [f for f in pending if f.cancelled()]:
                    del pending[future]
    except BaseException:
        aborted.set()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    executor.shutdown(wait=True)
    return LabelOutcome(
        episodes=stored["episodes"],
        messages=stored["messages"],
        issues=stored["issue"],
        failed=failed,
        errors=tuple(errors),
        tripped=breaker.reason,
    )


@dataclass(frozen=True)
class EpisodeLabel:
    category: str
    friction: str
    cause: str
    model: str


def _episode_label(row: sqlite3.Row) -> EpisodeLabel:
    return EpisodeLabel(
        category=row["category"],
        friction=row["friction"],
        cause=row["cause"],
        model=row["model"] or row["engine"],
    )


def episode_labels(
    labels: sqlite3.Connection, keys: Iterable[tuple[str, str]]
) -> dict[str, EpisodeLabel]:
    found = (
        _fetch(
            labels,
            "SELECT * FROM episode_labels"
            " WHERE episode_id = ? AND content_hash = ? AND prompt_version = ?",
            (episode_id, content_hash, LABEL_PROMPT_VERSION),
        )
        for episode_id, content_hash in keys
    )
    return {rows[0]["episode_id"]: _episode_label(rows[0]) for rows in found if rows}


def message_kinds(labels: sqlite3.Connection, keys: Iterable[tuple[str, str]]) -> dict[str, str]:
    found = (
        _fetch(
            labels,
            "SELECT message_key, kind FROM message_labels"
            " WHERE message_key = ? AND content_hash = ? AND prompt_version = ?",
            (key, content_hash, LABEL_PROMPT_VERSION),
        )
        for key, content_hash in keys
    )
    return {rows[0]["message_key"]: rows[0]["kind"] for rows in found if rows}


def issue_names(labels: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    rows = _fetch(
        labels,
        "SELECT issue_id, name, summary FROM issue_names WHERE prompt_version = ?"
        " ORDER BY named_at, issue_id",
        (LABEL_PROMPT_VERSION,),
    )
    return {r["issue_id"]: (r["name"], r["summary"]) for r in rows}


def episode_coverage(
    index: sqlite3.Connection,
    labels: sqlite3.Connection | None,
    scope: Scope,
    *,
    projects: Mapping[tuple[str, str], ProjectInfo] | None = None,
    session: tuple[str, str] | None = None,
) -> tuple[int, int]:
    where, params = _episode_where(index, scope, projects, session)
    rows = index.execute(
        "SELECT e.episode_id, e.content_hash FROM episodes e JOIN transcripts t USING (path)"
        f" WHERE {where}",
        params,
    ).fetchall()
    done = _current_keys(labels, "episode_labels", "episode_id") if labels is not None else set()
    labelled = sum(1 for episode_id, content_hash in rows if (episode_id, content_hash) in done)
    return labelled, len(rows) - labelled
