from __future__ import annotations

import argparse
import difflib
import json
import sqlite3
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import enrichment, paths, registry, session_env
from ..agents import CLAUDE_CODE, canonical_agent, display_agent
from ..analysis import adapters, embed, ingest, issues, labels, render, reports, rollups, store
from ..analysis.taxonomy import printable
from ..config import load_config
from ..models import Model, active_model, describe, proc
from ..paths import default_registry
from ..presence import PresenceClient
from ..progress import never_cancelled
from ..projects import matches
from ..usage import aggregate

DEFAULT_ISSUES = 5
DEFAULT_ISSUE_LIMIT = 20
DEFAULT_DAYS = 7
_AGENTS = (CLAUDE_CODE, "codex", "droid", "opencode", "pi")
DECLINED_NOTE = "labels skipped (declined)"
STALE_NOTICE = (
    "analysis: another rl process is updating the index; results may be partially refreshed"
)
_YES = frozenset({"\n", "y\n", "yes\n", "y", "yes"})


@dataclass(frozen=True)
class _LabelStore:
    choice: labels.LabelScope
    model: Model | None
    conn: sqlite3.Connection | None
    note: str | None


@dataclass(frozen=True)
class _Labelled:
    outcome: labels.LabelOutcome | None
    note: str | None
    failed: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


def _running() -> frozenset[tuple[str, str]] | None:
    result = PresenceClient().sessions()
    if not result.available:
        return None
    return frozenset((s.agent, s.session_id) for s in result.sessions if s.running)


def _coverage() -> dict[str, str]:
    return {agent: "full" if agent in adapters.BY_AGENT else "tokens" for agent in _AGENTS}


def _stderr_notice(message: str) -> None:
    print(message, file=sys.stderr)


def _line_notice(message: str) -> None:
    print(f"\r\x1b[K{message}", file=sys.stderr)


def _progress(done: int, total: int) -> None:
    end = "\n" if done >= total else ""
    print(f"\ranalysis: indexed {done} of {total} transcripts", end=end, file=sys.stderr)


def _index_error(error: store.IndexUnavailable, index_path: Path, as_json: bool) -> int:
    print(str(error), file=sys.stderr)
    if as_json:
        doc = {
            "schema": rollups.SCHEMA_VERSION,
            "index": {
                "path": str(index_path),
                "facts_version": store.FACTS_VERSION,
                "reason": error.reason,
            },
            "error": str(error),
        }
        print(json.dumps(doc, indent=2))
    return error.exit_code


def _read_notices(stats: ingest.IngestStats, index: str) -> list[str]:
    return [
        *([STALE_NOTICE] if stats.stale else []),
        *(
            [f"analysis: cannot write the analysis index in {index}; showing it as last built"]
            if not stats.writable
            else []
        ),
    ]


def _prepare(args: argparse.Namespace, now: datetime) -> ingest.IngestStats:
    show_progress = not args.json and sys.stderr.isatty()
    stats = ingest.prepare_index(
        Path(args.index),
        refresh=not args.no_refresh,
        rebuild=args.rebuild,
        roots=ingest.default_roots(),
        now=now,
        notice=_line_notice if show_progress else _stderr_notice,
        progress=_progress if show_progress else None,
    )
    for notice in _read_notices(stats, args.index):
        _stderr_notice(notice)
    return stats


def _failure_note(stats: ingest.IngestStats) -> str | None:
    if (
        stats.attempted >= ingest.FAILURE_RATIO_MIN_ATTEMPTED
        and stats.failed_now / stats.attempted > ingest.FAILURE_RATIO_LIMIT
    ):
        return (
            f"analysis: {stats.failed_now} of {stats.attempted} changed transcripts "
            "failed to parse; see stderr"
        )
    return None


def _index_path(args: argparse.Namespace) -> Path:
    return paths.analysis_index_path(store.FACTS_VERSION, Path(args.index))


def _window(
    args: argparse.Namespace,
    conn: sqlite3.Connection,
    scope: rollups.Scope,
    readers: rollups.TokenReaders,
) -> tuple[date | None, date | None]:
    if args.all:
        return None, None
    if args.since or args.until:
        return args.since, args.until
    found = (*readers.live, *readers.archived)
    latest = max(
        filter(None, (rollups.latest_day(conn, scope), aggregate.latest_day(found))),
        default=None,
    )
    if latest is None:
        return None, None
    days = args.days or DEFAULT_DAYS
    return date.fromisoformat(latest) - timedelta(days=days - 1), None


def _unwindowed(args: argparse.Namespace) -> rollups.Scope:
    return rollups.Scope(
        since=None,
        until=None,
        agents=_agents(args),
        selectors=(*([args.selector] if args.selector else []), *args.project),
        include_archived=args.archived,
    )


def _token_readers(args: argparse.Namespace, scope: rollups.Scope) -> rollups.TokenReaders:
    return rollups.tokens_only_readers(scope, archive=_archive(args))


def _scope(
    args: argparse.Namespace,
    conn: sqlite3.Connection,
    unwindowed: rollups.Scope,
    readers: rollups.TokenReaders,
) -> rollups.Scope:
    since, until = _window(args, conn, unwindowed, readers)
    return replace(unwindowed, since=since, until=until)


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _count(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return value


def _agents(args: argparse.Namespace) -> frozenset[str]:
    return frozenset(canonical_agent(name) for name in args.source or ())


def _source_name(text: str) -> str:
    return display_agent(canonical_agent(text))


def _path_like(selector: str) -> bool:
    return "/" in selector


def _known(selector: str, records: Sequence[Mapping[str, Any]]) -> bool:
    return _path_like(selector) or any(matches(dict(rec), selector) for rec in records)


def _registry_records(reg: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [rec for bucket in ("threads", "archived") for rec in reg.get(bucket) or []]


def _selector_error(selector: str, verbs: Sequence[str]) -> str:
    close = difflib.get_close_matches(selector, verbs, n=1)
    if close:
        suggestion = "reports save" if close[0] == "save" else close[0]
        return f"unknown subcommand {selector!r}; did you mean {suggestion!r}?"
    return f"no project or path matches {selector!r}; pass a git-project label or a path"


def _check_selectors(
    ap: argparse.ArgumentParser,
    args: argparse.Namespace,
    reg: Mapping[str, Any],
    verbs: Sequence[str],
) -> None:
    records = _registry_records(reg)
    if args.selector and not _known(args.selector, records):
        ap.error(_selector_error(args.selector, verbs))
    unknown = [p for p in args.project if not _known(p, records)]
    if unknown:
        ap.error(_selector_error(unknown[0], ()))


def _check_refresh(ap: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.rebuild and args.no_refresh:
        ap.error("--rebuild cannot be combined with --no-refresh")


def _check_window(ap: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    _check_refresh(ap, args)
    if args.all and (args.since or args.until):
        ap.error("--all cannot be combined with --since or --until")
    if args.days is not None and (args.all or args.since or args.until):
        ap.error("--days cannot be combined with --all, --since or --until")
    if args.since and args.until and args.since > args.until:
        ap.error("--since cannot be later than --until")


def _embed_doc(loaded: embed.EmbedLoad, found: issues.IssueFindings) -> dict[str, Any]:
    used = loaded.embedder is not None and found.embed_failure is None
    return render.embed_json(used, loaded.reason or found.embed_failure)


def _issues_section(
    conn: sqlite3.Connection,
    scope: rollups.Scope,
    projects: Mapping[tuple[str, str], rollups.ProjectInfo],
    limit: int,
) -> tuple[list[issues.Issue], dict[str, Any] | None]:
    if limit < 1:
        return [], None
    loaded = embed.load_embedder(allow_download=False)
    found = issues.find_issues(
        conn,
        scope,
        embedder=loaded.embedder,
        names={},
        min_sessions=issues.MIN_ISSUE_SESSIONS,
        limit=limit,
        projects=projects,
    )
    return found.issues, _embed_doc(loaded, found)


def _labels_path(args: argparse.Namespace) -> Path:
    return paths.analysis_labels_path(Path(args.index))


def _read_labels(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        conn = store.connect(path, read_only=True)
    except sqlite3.Error:
        return None
    try:
        conn.execute("SELECT 1 FROM episode_labels LIMIT 1").fetchall()
    except sqlite3.Error:
        conn.close()
        return None
    return conn


def _write_labels(path: Path) -> tuple[sqlite3.Connection | None, str | None]:
    try:
        return store.open_labels(path), None
    except store.LabelsTooNew as error:
        return _read_labels(path), f"labels skipped: {error}"
    except (OSError, sqlite3.Error) as error:
        return _read_labels(path), f"labels skipped: cannot write {path}: {error}"


def _interactive() -> bool:
    return (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and sys.stderr.isatty()
        and session_env.current_session() is None
    )


def _wanted(args: argparse.Namespace) -> tuple[labels.LabelScope, str | None]:
    return labels.default_label_scope(
        explicit=args.label,
        json_output=args.json,
        stdin_tty=sys.stdin.isatty(),
        stdout_tty=sys.stdout.isatty(),
        stderr_tty=sys.stderr.isatty(),
        current_session=session_env.current_session(),
    )


def _explicit(args: argparse.Namespace) -> bool:
    return args.label in {"recent", "all"}


def _resolve_labeller(
    args: argparse.Namespace, stats: ingest.IngestStats
) -> tuple[labels.LabelScope, Model | None, str | None]:
    choice, note = _wanted(args)
    if choice == "none":
        return choice, None, note
    model = active_model(load_config(), "analyze")
    if model is None:
        return "none", None, labels.NO_MODEL_NOTE
    if not stats.writable:
        return "none", None, f"labels skipped: the analysis index dir {args.index} is not writable"
    return choice, model, None


@contextmanager
def _label_store(args: argparse.Namespace, stats: ingest.IngestStats) -> Iterator[_LabelStore]:
    choice, model, note = _resolve_labeller(args, stats)
    path = _labels_path(args)
    if model is None:
        conn, failure = _read_labels(path), None
    else:
        conn, failure = _write_labels(path)
    ready = failure is None and model is not None
    found = _LabelStore(
        choice if ready else "none", model if ready else None, conn, note or failure
    )
    try:
        yield found
    finally:
        if conn is not None:
            conn.close()


def _estimate_line(plan: labels.LabelPlan, model: Model) -> str:
    cost = "" if plan.est_cost is None else f", ~${plan.est_cost:.2f}"
    return (
        f"labelling {len(plan.episodes)} episodes, {len(plan.messages)} messages, "
        f"{len(plan.issues)} issues: ~{plan.calls} calls, ~{plan.est_seconds:.0f}s{cost}"
        f" via {describe(model)}"
    )


def _confirmed() -> bool:
    print("continue? [Y/n] ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().lower() in _YES


def _label_progress(kind: str, done: int, total: int) -> None:
    print(f"\rlabelling {kind}: {done} of {total}", end="", file=sys.stderr)


def _report(outcome: labels.LabelOutcome) -> None:
    print(
        f"labelled {outcome.episodes} episodes, {outcome.messages} messages, "
        f"{outcome.issues} issues; {outcome.failed} failed",
        file=sys.stderr,
    )
    for error in outcome.errors:
        print(f"  {printable(error)}", file=sys.stderr)
    if outcome.tripped:
        print(f"stopped early: {printable(outcome.tripped)}", file=sys.stderr)


def _unfinished(outcome: labels.LabelOutcome) -> bool:
    stored = outcome.episodes + outcome.messages + outcome.issues
    return outcome.tripped is not None or (outcome.failed > 0 and stored == 0)


def _run_labels(  # noqa: PLR0913 -- one labelling pass needs its scope and its store
    args: argparse.Namespace,
    store_: _LabelStore,
    index: sqlite3.Connection,
    scope: rollups.Scope,
    found: Sequence[issues.Issue],
    now: datetime,
    *,
    projects: Mapping[tuple[str, str], rollups.ProjectInfo],
    session: tuple[str, str] | None = None,
) -> _Labelled:
    model, conn = store_.model, store_.conn
    if model is None or conn is None:
        return _Labelled(None, store_.note, failed=_explicit(args))
    plan = labels.plan_labels(
        index,
        conn,
        scope,
        choice=store_.choice,
        issues=found,
        now=now,
        engine=model.name,
        model_name=model.model,
        concurrency=enrichment.default_concurrency(model),
        projects=projects,
        session=session,
    )
    if plan.calls == 0:
        return _Labelled(None, store_.note)
    if not args.json:
        print(_estimate_line(plan, model), file=sys.stderr)
        if _interactive() and not args.yes and not _confirmed():
            return _Labelled(None, DECLINED_NOTE)
    shown = not args.json and sys.stderr.isatty()
    try:
        outcome = labels.run_labels(
            model, plan, conn, now, never_cancelled, _label_progress if shown else None
        )
    except KeyboardInterrupt:
        proc.kill_all_children()
        raise
    if shown:
        print(file=sys.stderr)
    _report(outcome)
    return _Labelled(outcome, store_.note, failed=_explicit(args) and _unfinished(outcome))


def _labels_doc(
    store_: _LabelStore,
    index: sqlite3.Connection,
    scope: rollups.Scope,
    ran: _Labelled,
    *,
    projects: Mapping[tuple[str, str], rollups.ProjectInfo],
    session: tuple[str, str] | None = None,
) -> dict[str, Any]:
    labelled, pending = labels.episode_coverage(
        index, store_.conn, scope, projects=projects, session=session
    )
    return render.labels_json(
        engine=store_.model.name if store_.model else None,
        model=store_.model.model if store_.model else None,
        labelled=labelled,
        pending=pending,
        note=ran.note,
        scope=store_.choice,
        outcome=ran.outcome,
    )


def _named(found: Sequence[issues.Issue], conn: sqlite3.Connection | None) -> list[issues.Issue]:
    names = labels.issue_names(conn) if conn is not None else {}
    return [
        replace(i, name=names[i.issue_id][0], summary=names[i.issue_id][1])
        if i.issue_id in names
        else i
        for i in found
    ]


def _corpus(
    args: argparse.Namespace,
    stats: ingest.IngestStats,
    now: datetime,
    running: frozenset[tuple[str, str]] | None,
    projects: Mapping[tuple[str, str], rollups.ProjectInfo],
) -> tuple[dict[str, Any], _Labelled]:
    unwindowed = _unwindowed(args)
    readers = _token_readers(args, unwindowed)
    with (
        closing(store.connect(Path(stats.path), read_only=True)) as conn,
        _label_store(args, stats) as label_store,
    ):
        scope = _scope(args, conn, unwindowed, readers)
        extra = rollups.tokens_only_records(scope, readers)
        found_issues, embed_doc = _issues_section(conn, scope, projects, args.issues)
        ran = _run_labels(args, label_store, conn, scope, found_issues, now, projects=projects)
        named = _named(found_issues, label_store.conn)
        dims = tuple(dict.fromkeys(args.by or rollups.DEFAULT_DIMENSIONS))
        found = rollups.corpus_rollups(conn, scope, projects, extra, dims)
        doc = render.corpus_json(
            scope=scope,
            generated_at=now,
            coverage=_coverage(),
            index=stats,
            totals=found.totals,
            cache=found.cache,
            rollups=found.dimensions,
            worst=rollups.worst_sessions(conn, scope, projects, now, running, args.worst),
            issues=[render.issue_json(i) for i in named],
            labels=_labels_doc(label_store, conn, scope, ran, projects=projects),
            embed=embed_doc,
        )
        return doc, ran


def _archive(args: argparse.Namespace) -> Path | None:
    return paths.archive_root() if args.archived else None


def _emit(doc: dict[str, Any], text: Callable[[dict[str, Any]], str], as_json: bool) -> None:
    print(json.dumps(doc, indent=2) if as_json else text(doc))


def _finish(stats: ingest.IngestStats, ran: _Labelled | None = None) -> int:
    note = _failure_note(stats)
    if note is not None:
        print(note, file=sys.stderr)
    if ran is not None and ran.failed:
        print("analysis: the requested labelling did not complete", file=sys.stderr)
    return 1 if note is not None or (ran is not None and ran.failed) else 0


def _shared(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--no-refresh", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--index", default=str(paths.analysis_dir()))
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--json", action="store_true")


def _label_flags(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--label", choices=labels.LABEL_SCOPES)
    ap.add_argument("--yes", action="store_true")


def _scope_flags(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("selector", nargs="?")
    ap.add_argument("--project", action="append", default=[])
    ap.add_argument(
        "--source", action="append", type=_source_name, choices=sorted(aggregate.READERS)
    )
    ap.add_argument("--days", type=_positive_int, help=f"default {DEFAULT_DAYS}")
    ap.add_argument("--since", type=aggregate.parse_day)
    ap.add_argument("--until", type=aggregate.parse_day, help="alone, all history up to this day")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--archived", action="store_true")


def _session_miss(message: str, matches: Sequence[tuple[str, str]], as_json: bool) -> int:
    if as_json:
        doc = {
            "schema": rollups.SCHEMA_VERSION,
            "error": message,
            "matches": [{"agent": agent, "session_id": sid} for agent, sid in matches],
        }
        print(json.dumps(doc, indent=2))
    else:
        print(message, file=sys.stderr)
    return 1


def _no_data(session_id: str) -> str:
    *rest, last = adapters.TOKENS_ONLY_SOURCES
    return (
        f"no analysis data for session {session_id}; {', '.join(rest)} and {last} sessions"
        f" are counted for tokens only, see rl usage session {session_id}"
    )


def _session_issues(conn: sqlite3.Connection, agent: str, session_id: str) -> tuple[str, ...]:
    signatures = rollups.session_signatures(conn, agent, session_id)
    if not signatures:
        return ()
    everything = rollups.Scope(
        since=None, until=None, agents=frozenset(), selectors=(), include_archived=True
    )
    found = issues.find_issues(
        conn,
        everything,
        embedder=None,
        names={},
        min_sessions=issues.MIN_ISSUE_SESSIONS,
        limit=sys.maxsize,
    )
    return issues.issues_with_signatures(found.issues, signatures)


def _cmd_session(argv: list[str], prog: str) -> int:
    ap = argparse.ArgumentParser(prog=f"{prog} session")
    ap.add_argument("id")
    _label_flags(ap)
    _shared(ap)
    args = ap.parse_args(argv)
    _check_refresh(ap, args)

    now = _now()
    try:
        stats = _prepare(args, now)
    except store.IndexUnavailable as error:
        return _index_error(error, _index_path(args), args.json)

    with closing(store.connect(Path(stats.path), read_only=True)) as conn:
        matches_ = rollups.resolve_session(conn, args.id)
        if not matches_:
            return _session_miss(_no_data(args.id), [], args.json)
        if len(matches_) > 1:
            if args.json:
                return _session_miss(
                    f"ambiguous session id {args.id!r}",
                    matches_[: aggregate.AMBIGUOUS_LIMIT],
                    True,
                )
            aggregate.print_ambiguous(args.id, matches_, 11, file=sys.stderr)
            return 1
        agent, session_id = matches_[0]
        projects = rollups.project_map(registry.load_registry(args.registry))
        scope = rollups.Scope(
            since=None, until=None, agents=frozenset(), selectors=(), include_archived=True
        )
        key = (agent, session_id)
        with _label_store(args, stats) as label_store:
            ran = _run_labels(
                args, label_store, conn, scope, (), now, projects=projects, session=key
            )
            detail = rollups.session_detail(
                conn,
                agent,
                session_id,
                projects,
                now,
                _running(),
                label_store.conn,
                issues=_session_issues(conn, agent, session_id),
            )
            doc = render.session_detail_json(
                detail,
                _labels_doc(label_store, conn, scope, ran, projects=projects, session=key),
            )
    _emit(doc, render.session_text, args.json)
    return _finish(stats, ran)


def _cmd_issues(argv: list[str], prog: str) -> int:
    ap = argparse.ArgumentParser(prog=f"{prog} issues")
    _scope_flags(ap)
    ap.add_argument("--min-sessions", type=_positive_int, default=issues.MIN_ISSUE_SESSIONS)
    ap.add_argument("--limit", type=_positive_int, default=DEFAULT_ISSUE_LIMIT)
    ap.add_argument("--download-embed-model", action="store_true")
    _shared(ap)
    args = ap.parse_args(argv)
    _check_window(ap, args)
    reg = registry.load_registry(args.registry)
    _check_selectors(ap, args, reg, ())

    now = _now()
    try:
        stats = _prepare(args, now)
    except store.IndexUnavailable as error:
        return _index_error(error, _index_path(args), args.json)

    projects = rollups.project_map(reg)
    unwindowed = _unwindowed(args)
    loaded = embed.load_embedder(allow_download=args.download_embed_model)
    label_conn = _read_labels(_labels_path(args))
    with (
        closing(store.connect(Path(stats.path), read_only=True)) as conn,
        closing(label_conn) if label_conn is not None else nullcontext(),
    ):
        scope = _scope(args, conn, unwindowed, _token_readers(args, unwindowed))
        found = issues.find_issues(
            conn,
            scope,
            embedder=loaded.embedder,
            names=labels.issue_names(label_conn) if label_conn is not None else {},
            min_sessions=args.min_sessions,
            limit=args.limit,
            projects=projects,
        )
    doc = render.issues_json(
        scope=scope,
        index=stats,
        embed=_embed_doc(loaded, found),
        issues=found.issues,
    )
    _emit(doc, render.issues_text, args.json)
    code = _finish(stats)
    if args.download_embed_model and loaded.embedder is None:
        print(f"analysis: the embed model is not available: {loaded.reason}", file=sys.stderr)
        return 1
    return code


def _cmd_reports_save(argv: list[str], prog: str) -> int:
    ap = argparse.ArgumentParser(prog=f"{prog} reports save")
    ap.add_argument("kind", choices=reports.REPORT_KINDS)
    ap.add_argument("source", help="a file, or - for stdin")
    ap.add_argument("--title")
    ap.add_argument("--window")
    ap.add_argument("--based-on", action="append", default=[])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        body = sys.stdin.read() if args.source == "-" else Path(args.source).read_text("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        print(f"cannot read {args.source}: {error}", file=sys.stderr)
        return 2
    if not body.strip():
        print("report body is empty", file=sys.stderr)
        return 2
    try:
        meta = reports.save_report(
            args.kind,
            body,
            _now(),
            title=args.title,
            window=args.window,
            based_on=args.based_on,
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(render.saved_report_json(meta), indent=2))
    else:
        print(meta.path)
    return 0


def _cmd_reports(argv: list[str], prog: str) -> int:
    if argv and argv[0] == "save":
        return _cmd_reports_save(argv[1:], prog)
    ap = argparse.ArgumentParser(prog=f"{prog} reports")
    ap.add_argument("--kind", choices=reports.REPORT_KINDS)
    ap.add_argument("--limit", type=_positive_int)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    found = reports.list_reports(args.kind, args.limit)
    if args.json:
        print(json.dumps(render.reports_json(found), indent=2))
    elif not found:
        print("no reports")
    else:
        print("\n".join(f"{m.report_id}  {m.title}" for m in found))
    return 0


_ACTIONS: dict[str, Callable[[list[str], str], int]] = {
    "session": _cmd_session,
    "issues": _cmd_issues,
    "reports": _cmd_reports,
}
SUBCOMMANDS: tuple[str, ...] = tuple(_ACTIONS)


def cmd_analyze(argv: list[str], prog: str = "rl analyze") -> int:
    if argv and argv[0] in _ACTIONS:
        return _ACTIONS[argv[0]](argv[1:], prog)
    ap = argparse.ArgumentParser(
        prog=prog,
        description="Where sessions went wrong and where tokens went, from a local analysis index.",
        epilog="subcommands: session <id>, issues, reports, reports save",
    )
    _scope_flags(ap)
    ap.add_argument("--by", action="append", choices=rollups.DIMENSIONS)
    ap.add_argument("--worst", type=_count, default=10)
    ap.add_argument("--issues", type=_count, default=DEFAULT_ISSUES)
    _label_flags(ap)
    _shared(ap)
    args = ap.parse_args(argv)
    _check_window(ap, args)
    reg = registry.load_registry(args.registry)
    _check_selectors(ap, args, reg, (*SUBCOMMANDS, "save"))

    now = _now()
    try:
        stats = _prepare(args, now)
    except store.IndexUnavailable as error:
        return _index_error(error, _index_path(args), args.json)

    doc, ran = _corpus(args, stats, now, _running(), rollups.project_map(reg))
    _emit(doc, render.corpus_text, args.json)
    return _finish(stats, ran)
