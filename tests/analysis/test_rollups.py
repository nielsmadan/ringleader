import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from ringleader.analysis import ingest, render, rollups, store
from ringleader.analysis.facts import MAIN_THREAD
from ringleader.analysis.rollups import ProjectInfo, Scope
from ringleader.analysis.scoping import scope_filter
from ringleader.usage.base import UsageRecord
from ringleader.usage.claude import ClaudeUsageReader

from .factories import bash, calls, claude_records, compaction, fail, message, request, write_jsonl
from .factories import facts as make_facts

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
A = "aaaaaaaa-2222-3333-4444-555555555555"
B = "bbbbbbbb-2222-3333-4444-555555555555"
C = "cccccccc-2222-3333-4444-555555555555"


def scope(**fields):
    base = {
        "since": None,
        "until": None,
        "agents": frozenset(),
        "selectors": (),
        "include_archived": False,
    }
    return Scope(**{**base, **fields})


def specs(name, *items):
    return tuple({"call_id": f"{name}-{i}", **item} for i, item in enumerate(items))


def put(
    conn, name, *, session=A, thread=MAIN_THREAD, archived=False, agent="claude-code", **fields
):
    tool_calls = fields.pop("tool_calls", ())
    if tool_calls and isinstance(tool_calls[0], dict):
        tool_calls = calls(*tool_calls)
    base = make_facts(
        tool_calls,
        session_id=session,
        thread_id=thread,
        thread_key=f"{session}/{name}",
        parent_thread_id=None if thread == MAIN_THREAD else MAIN_THREAD,
        **fields,
    )
    ref = replace(base.ref, path=Path(f"/p/{name}.jsonl"), archived=archived, agent=agent)
    built = replace(base, ref=ref)
    with store.transaction(conn):
        store.replace_transcript(conn, built, *ingest.derive(built))
    return str(ref.path)


def on_day(req, day, **fields):
    return replace(req, timestamp=f"{day}T10:00:00Z", **fields)


@pytest.fixture
def conn(tmp_path):
    opened, _cold = store.open_index_for_write(tmp_path / "idx", NOW.isoformat())
    with closing(opened):
        yield opened


def row(rows, key):
    return next(r for r in rows if r.key == key)


def totals(conn, scope_, projects=None, extra=()):
    return rollups.corpus_rollups(conn, scope_, projects or {}, list(extra), ()).totals


def rollup(conn, scope_, dim, projects=None, extra=()):
    found = rollups.corpus_rollups(conn, scope_, projects or {}, list(extra), (dim,))
    return found.dimensions[dim]


def cache(conn, scope_, projects=None):
    return rollups.corpus_rollups(conn, scope_, projects or {}, [], ()).cache


def test_totals_equal_the_usage_reader_with_fork_subagent_and_workflow(tmp_path):
    root = tmp_path / "claude"
    primary = claude_records(A, ("ls", "pwd"))
    write_jsonl(root / "-w" / f"{A}.jsonl", primary)
    write_jsonl(root / "-w" / f"{B}.jsonl", primary)
    write_jsonl(
        root / "-w" / A / "subagents" / "agent-x.jsonl", claude_records(f"{A}-sub", ("id",))
    )
    write_jsonl(
        root / "-w" / A / "subagents" / "workflows" / "wf_1" / "agent-y.jsonl",
        claude_records(f"{A}-wf", ("who", "date")),
    )
    roots = ingest.Roots(root, tmp_path / "codex", tmp_path / "archive")
    stats = ingest.prepare_index(
        tmp_path / "index",
        refresh=True,
        rebuild=False,
        roots=roots,
        now=NOW,
        notice=lambda m: None,
    )
    expected = sum(r.total for r in ClaudeUsageReader(projects_dir=root).records())
    with closing(store.connect(Path(stats.path), read_only=True)) as conn:
        found = totals(conn, scope(), {}, [])
    assert found.tokens.total == expected
    assert found.requests == 5
    assert found.calls == 5


def test_a_tool_call_duplicated_in_a_fork_counts_once(conn):
    shared = specs("dup", bash("ls"))
    put(conn, "primary", session=A, tool_calls=shared)
    put(conn, "fork", session=B, tool_calls=shared)
    assert totals(conn, scope(), {}, []).calls == 1


def test_a_user_message_duplicated_in_a_fork_counts_once(conn):
    kinds = replace(message(0, "no, stop"), rule_kind="correction")
    put(conn, "primary", user_messages=(kinds,))
    put(conn, "fork", user_messages=(kinds,))
    detail = render.session_detail_json(
        rollups.session_detail(conn, "claude-code", A, {}, NOW, None, None)
    )
    assert detail["user_messages"]["counts"] == {"correction": 1}


def test_the_fork_copy_counts_when_the_primary_is_archived_and_out_of_scope(conn):
    shared = specs("dup", bash("ls"))
    put(conn, "primary", session=A, archived=True, tool_calls=shared)
    put(conn, "fork", session=B, tool_calls=shared)
    only_live = totals(conn, scope(), {}, [])
    with_archive = totals(conn, scope(include_archived=True), {}, [])
    assert (only_live.calls, only_live.sessions) == (1, 1)
    assert (with_archive.calls, with_archive.sessions) == (1, 1)


def test_archived_rows_appear_only_with_include_archived(conn):
    put(conn, "live", session=A, tool_calls=specs("live", bash("ls")))
    put(conn, "old", session=B, archived=True, tool_calls=specs("old", bash("ls")))
    assert totals(conn, scope(), {}, []).calls == 1
    assert totals(conn, scope(include_archived=True), {}, []).calls == 2


def test_two_cwds_of_one_git_project_form_one_row(conn):
    put(conn, "one", session=A, cwd="/w/one", requests=(request(1, 100),))
    put(conn, "two", session=B, cwd="/w/two", requests=(request(9, 100),))
    info = ProjectInfo("git:r", "proj", "/w")
    projects = {("claude-code", A): info, ("claude-code", B): info}
    rows = rollup(conn, scope(), "project", projects, [])
    assert [(r.key, r.sessions, r.requests) for r in rows] == [("proj", 2, 2)]


def test_an_unregistered_session_falls_back_to_its_folder(conn):
    put(conn, "one", cwd="/w/loose", requests=(request(1, 100),))
    rows = rollup(conn, scope(), "project", {}, [])
    assert [r.key for r in rows] == ["/w/loose"]


def test_source_scoping_excludes_the_other_agents(conn):
    put(conn, "cc", session=A, requests=(request(1, 100),))
    put(conn, "cx", session=B, agent="codex", requests=(request(9, 100),))
    rows = rollup(conn, scope(agents=frozenset({"claude-code"})), "agent", {}, [])
    assert [r.key for r in rows] == ["claude-code"]


def test_selectors_filter_by_label_and_by_cwd_prefix(conn):
    put(conn, "one", session=A, cwd="/w/one", requests=(request(1, 100),))
    put(conn, "two", session=B, cwd="/other/two", requests=(request(9, 100),))
    projects = {("claude-code", A): ProjectInfo("k", "proj", "/w")}
    by_label = totals(conn, scope(selectors=("proj",)), projects, [])
    by_cwd = totals(conn, scope(selectors=("/other",)), projects, [])
    assert (by_label.sessions, by_cwd.sessions) == (1, 1)
    assert [r.key for r in rollup(conn, scope(selectors=("/other",)), "folder", {}, [])] == [
        "/other/two"
    ]


def test_the_window_includes_the_boundary_days(conn):
    days = ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04")
    put(
        conn,
        "t",
        requests=tuple(on_day(request(i * 2 + 1, 100), d) for i, d in enumerate(days)),
    )
    window = scope(since=date(2026, 10, 2), until=date(2026, 10, 3))
    assert totals(conn, window, {}, []).requests == 2


def test_skill_tokens_come_from_attribution_and_calls_from_the_skill_tool(conn):
    tool_calls = tuple(
        replace(c, skill="lint")
        for c in calls(*specs("s", {"tool": "Skill", "command": None}, {"tool": "Skill"}))
    )
    put(
        conn,
        "t",
        tool_calls=tool_calls,
        requests=(replace(request(1, 100, output=50), skill="lint"), request(3, 100)),
    )
    skill = row(rollup(conn, scope(), "skill", {}, []), "lint")
    assert (skill.calls, skill.requests, skill.tokens.total) == (2, 1, 150)


def test_plugin_rows_combine_attribution_and_the_skill_prefix(conn):
    tool_calls = tuple(
        replace(c, skill="acme:review") for c in calls(*specs("s", {"tool": "Skill"}))
    )
    put(
        conn,
        "t",
        tool_calls=tool_calls,
        requests=(replace(request(1, 100), plugin="acme"),),
    )
    plugin = row(rollup(conn, scope(), "plugin", {}, []), "acme")
    assert (plugin.calls, plugin.requests) == (1, 1)


def test_mcp_rows_carry_calls_and_errors_but_no_tokens(conn):
    tool_calls = tuple(
        replace(c, mcp_server="gh", kind="mcp", tool="mcp__gh__pr")
        for c in calls(*specs("m", {"tool": "x"}, {"tool": "x", "status": "error"}))
    )
    put(conn, "t", tool_calls=tool_calls, requests=(request(1, 100),))
    gh = row(rollup(conn, scope(), "mcp_server", {}, []), "gh")
    assert (gh.calls, gh.errors, gh.tokens, gh.context) == (2, 1, None, None)


def test_user_caused_errors_are_not_counted_as_errors(conn):
    items = specs(
        "e",
        fail("make"),
        {"tool": "Bash", "command": "x", "status": "error", "error_category": "interruption"},
    )
    put(conn, "t", tool_calls=items)
    assert totals(conn, scope(), {}, []).errors == 1


def test_tokens_only_sources_appear_in_the_agent_rollup(conn):
    put(conn, "t", requests=(request(1, 100),))
    put(conn, "cx", session=B, agent="codex", requests=(request(2, 15, output=10),))
    extra = [
        UsageRecord(
            "pi",
            "p1",
            "/w/pi",
            "2026-10-01T10:00:00Z",
            "anthropic",
            "claude-opus-5",
            10,
            5,
            0,
            0,
            0,
            15,
        ),
    ]
    rows = rollup(conn, scope(), "agent", {}, extra)
    by_key = {r.key: r.tokens.total for r in rows}
    assert by_key == {"claude-code": 110, "pi": 15, "codex": 25}
    assert totals(conn, scope(), {}, extra).tokens.total == 150


def test_a_thread_with_a_300k_request_gets_the_large_window_and_its_sibling_does_not(conn):
    put(conn, "big", cwd="/big", requests=(request(1, 300_000),))
    put(conn, "small", session=B, cwd="/small", requests=(request(9, 150_000),))
    fills = {r.key: r.context.fill_p95 for r in rollup(conn, scope(), "folder")}
    assert fills == {
        "/big": 300_000 / rollups.LARGE_CONTEXT_WINDOW,
        "/small": 150_000 / rollups.DEFAULT_CONTEXT_WINDOW,
    }


def test_context_stats_use_each_requests_own_thread_window(conn):
    put(conn, "big", requests=(request(1, 300_000), request(3, 100_000)))
    stats = totals(conn, scope(), {}, []).context
    assert (stats.p50, stats.max, stats.fill_p95) == (100_000, 300_000, 0.3)


def test_cache_stats_count_a_two_hour_rebuild_in_both_idle_buckets_at_1h_prices(conn):
    rebuilt = replace(
        request(3, 1000),
        input=0,
        cache_write=1000,
        cache_write_1h=1000,
        rebuild=True,
        gap_seconds=7200.0,
    )
    put(conn, "t", requests=(request(1, 1000), rebuilt))
    stats = cache(conn, scope())
    assert (stats.rebuilds, stats.rebuilds_after_5m, stats.rebuilds_after_1h) == (1, 1, 1)
    assert stats.rebuild_tokens == 1000
    assert stats.rebuild_cost == pytest.approx(1000 * 5 / 1e6 * 2)


def test_cache_hit_rate_is_reads_over_all_input_side_tokens(conn):
    hit = replace(request(1, 100), input=100, cache_read=300, cache_write=100)
    put(conn, "t", requests=(hit,))
    assert cache(conn, scope()).hit_rate == pytest.approx(300 / 500)


def failing_session(conn, name, session, failures):
    items = specs(name, *([fail("make")] * failures), *([bash("ls")] * (6 - failures)))
    return put(conn, name, session=session, tool_calls=items, requests=(request(1, 100),))


def test_worst_sessions_order_by_points_per_100_calls_and_skip_short_sessions(conn):
    failing_session(conn, "bad", A, 6)
    failing_session(conn, "ok", B, 0)
    put(
        conn,
        "short",
        session=C,
        tool_calls=specs("short", fail("make"), fail("make"), fail("make")),
    )
    worst = rollups.worst_sessions(conn, scope(), {}, NOW, None, 10)
    assert [s.session_id for s in worst] == [A, B]
    assert worst[0].health.per_100_calls > worst[1].health.per_100_calls
    assert worst[0].tool_errors == 6
    assert worst[0].signals["failure_streak"] >= 1


def test_a_session_the_presence_layer_reports_running_is_open(conn):
    failing_session(conn, "bad", A, 6)
    running = frozenset({("claude-code", A)})
    [live] = rollups.worst_sessions(conn, scope(), {}, NOW, running, 10)
    [idle] = rollups.worst_sessions(conn, scope(), {}, NOW, frozenset(), 10)
    assert (live.outcome, idle.outcome) == ("open", "completed")


def test_subagent_signals_are_reported_apart_from_the_main_thread(conn):
    failing_session(conn, "main", A, 0)
    put(
        conn,
        "sub",
        thread="agent-1",
        tool_calls=specs("sub", *([fail("make")] * 4)),
        requests=(request(7, 100),),
    )
    [summary] = rollups.worst_sessions(conn, scope(), {}, NOW, None, 10)
    assert summary.tool_calls == 10
    assert summary.signals.get("failure_streak", 0) == 0
    assert summary.subagent_signals["failure_streak"] == 1


def test_a_thread_id_prefix_resolves_to_its_root_session(conn):
    put(conn, "main", session=A)
    put(conn, "sub", session=A, thread="agent-abc123")
    assert rollups.resolve_session(conn, "agent-abc") == [("claude-code", A)]
    assert rollups.resolve_session(conn, "aaaa") == [("claude-code", A)]
    assert rollups.resolve_session(conn, "zzz") == []


def test_detail_lists_subagents_with_their_tokens_and_signals(conn):
    put(conn, "main", requests=(request(1, 100),), tool_calls=specs("main", bash("ls")))
    put(
        conn,
        "sub",
        thread="agent-1",
        tool_calls=specs("sub", *([fail("make")] * 3)),
        requests=(request(7, 100, output=40),),
        subagent_type="Explore",
    )
    detail = render.session_detail_json(
        rollups.session_detail(conn, "claude-code", A, {}, NOW, None, None)
    )
    [sub] = detail["subagents"]
    assert sub["subagent_type"] == "Explore"
    assert sub["tokens"]["total"] == 140
    assert sub["tool_calls"] == 3
    assert sub["signals"]["failure_streak"] == 1
    assert detail["subagent_signals"]["failure_streak"] == 1
    assert detail["schema"] == 1


def test_detail_reports_context_episodes_and_notable_messages(conn):
    items = specs("t", *([fail("make")] * 3), bash("ls"))
    messages = (
        message(0, "start"),
        replace(message(2, "that is wrong"), rule_kind="correction"),
        message(3, "[Request interrupted by user]", is_interrupt=True),
        replace(message(1, "ignore me", origin="agent"), rule_kind="correction"),
    )
    put(
        conn,
        "t",
        tool_calls=items,
        requests=(request(1, 1000), request(5, 300_000)),
        user_messages=messages,
    )
    with closing(sqlite3.connect(":memory:")) as labels:
        labels.executescript(";".join(store.LABELS_DDL))
        detail = render.session_detail_json(
            rollups.session_detail(conn, "claude-code", A, {}, NOW, None, labels)
        )
    context = detail["context"]
    assert context["window"] == rollups.LARGE_CONTEXT_WINDOW
    assert [p["tokens"] for p in context["series"]] == [1000, 300_000]
    assert context["threads"][0]["max"] == 300_000
    [episode] = [e for e in detail["episodes"] if "failure_streak" in e["kinds"]]
    assert episode["evidence"][0]["status"] == "error"
    assert len(episode["evidence"]) <= rollups.EVIDENCE_LIMIT
    assert episode["label"] is None
    assert [m["kind"] for m in detail["user_messages"]["notable"]] == ["correction", "interrupt"]
    assert detail["tool_usage"][0] == {"tool": "Bash", "calls": 4, "errors": 3}


def test_detail_joins_a_stored_label(conn):
    items = specs("t", *([fail("make")] * 3))
    put(conn, "t", tool_calls=items, requests=(request(1, 100),))
    first = render.session_detail_json(
        rollups.session_detail(conn, "claude-code", A, {}, NOW, None, None)
    )["episodes"][0]
    with closing(sqlite3.connect(":memory:")) as labels:
        labels.executescript(";".join(store.LABELS_DDL))
        row_ = conn.execute(
            "SELECT content_hash FROM episodes WHERE episode_id = ?", (first["episode_id"],)
        )
        digest = row_.fetchone()[0]
        labels.execute(
            "INSERT INTO episode_labels VALUES (?, ?, 1, 'tooling', 'high', 'bad flag', 'claude', 'haiku', 'now')",
            (first["episode_id"], digest),
        )
        joined = render.session_detail_json(
            rollups.session_detail(conn, "claude-code", A, {}, NOW, None, labels)
        )
    assert joined["episodes"][0]["label"] == {
        "category": "tooling",
        "friction": "high",
        "cause": "bad flag",
        "model": "haiku",
    }


def test_project_map_keys_threads_and_archived_by_identity():
    reg = {
        "threads": [
            {
                "sessionId": A,
                "gitProjectKey": "k",
                "gitProjectLabel": "proj",
                "gitRoot": "/r",
                "project": "/r/x",
            }
        ],
        "archived": [{"sessionId": B, "source": "codex", "project": "/loose"}],
    }
    assert rollups.project_map(reg) == {
        ("claude-code", A): ProjectInfo("k", "proj", "/r"),
        ("codex", B): ProjectInfo("/loose", "/loose", None),
    }


def test_latest_day_reads_the_newest_request_day_of_the_index(conn):
    assert rollups.latest_day(conn) is None
    put(conn, "t", requests=(on_day(request(1, 100), "2026-10-03"),))
    assert rollups.latest_day(conn) == "2026-10-03"


def tokens_only(scope_, dirs, archive=None):
    readers = rollups.tokens_only_readers(scope_, dirs, archive)
    return rollups.tokens_only_records(scope_, readers)


def test_tokens_only_records_read_pi_sessions_inside_the_window_and_agent_scope(tmp_path):
    sessions = tmp_path / "pi"
    record = {
        "timestamp": "2026-10-02T10:00:00Z",
        "message": {
            "provider": "anthropic",
            "model": "claude-opus-5",
            "usage": {"input": 10, "output": 5, "cacheRead": 0, "cacheWrite": 0},
        },
    }
    write_jsonl(sessions / "--w--" / "2026-10-02_pisession.jsonl", [record])
    dirs = {"pi": {"sessions_dir": sessions}}
    found = tokens_only(scope(since=date(2026, 10, 2)), dirs)
    assert [(r.source, r.session_id, r.total) for r in found] == [("pi", "pisession", 15)]
    assert tokens_only(scope(since=date(2026, 10, 3)), dirs) == []
    only_claude = scope(agents=frozenset({"claude-code"}))
    assert rollups.tokens_only_readers(only_claude, dirs) == rollups.TokenReaders(
        live=(), archived=()
    )
    assert tokens_only(only_claude, dirs) == []


def test_requests_without_a_claude_model_are_left_out_of_claude_code_rollups(conn):
    put(
        conn,
        "t",
        requests=(
            request(1, 100),
            replace(request(3, 100), model=None),
            replace(request(5, 100), model=""),
            replace(request(7, 100), model="<synthetic>"),
            replace(request(9, 100), model="gpt-5"),
        ),
    )
    found = totals(conn, scope(), {}, [])
    assert (found.requests, found.tokens.total) == (1, 110)
    assert [r.key for r in rollup(conn, scope(), "model", {}, [])] == ["claude-opus-5"]


def test_compactions_count_once_per_event_inside_the_window(conn):
    early = compaction(1)
    late = compaction(3)
    reqs = (
        on_day(request(1, 100), "2026-10-01"),
        on_day(request(3, 100), "2026-10-05"),
    )
    kept = (
        replace(early, timestamp="2026-10-01T10:00:00Z"),
        replace(late, timestamp="2026-10-05T10:00:00Z", line=9),
    )
    put(conn, "a", requests=reqs, compactions=kept)
    put(conn, "b", session=B, requests=reqs, compactions=kept)
    assert totals(conn, scope(), {}, []).context.compactions == 2
    window = scope(since=date(2026, 10, 4))
    assert totals(conn, window, {}, []).context.compactions == 1


def test_a_mixed_rebuild_splits_its_cost_between_5m_and_1h_write_prices(conn):
    rebuilt = replace(
        request(3, 1000),
        input=0,
        cache_write=1000,
        cache_write_5m=600,
        cache_write_1h=400,
        rebuild=True,
        gap_seconds=400.0,
    )
    put(conn, "t", requests=(rebuilt,))
    stats = cache(conn, scope())
    assert (stats.write_5m, stats.write_1h) == (600, 400)
    assert (stats.rebuilds_after_5m, stats.rebuilds_after_1h) == (1, 0)
    assert stats.rebuild_cost == pytest.approx((600 * 6.25 + 400 * 10) / 1e6)


def test_a_gap_of_exactly_five_minutes_is_not_an_idle_rebuild(conn):
    rebuilt = replace(request(3, 1000), rebuild=True, gap_seconds=300.0)
    put(conn, "t", requests=(rebuilt,))
    stats = cache(conn, scope())
    assert (stats.rebuilds, stats.rebuilds_after_5m) == (1, 0)


def test_a_label_selector_with_projects_limits_the_context_windows_and_cache_stats(conn):
    put(conn, "mine", session=A, requests=(request(1, 300_000),))
    put(
        conn,
        "other",
        session=B,
        cwd="/elsewhere",
        requests=(replace(request(9, 150_000), input=0, cache_write=7, cache_write_1h=7),),
    )
    projects = {("claude-code", A): ProjectInfo("k", "proj", "/w")}
    picked = scope(selectors=("proj",))
    assert totals(conn, picked, projects).context.fill_p95 == 0.3
    stats = cache(conn, picked, projects)
    assert (stats.write_5m, stats.write_1h, stats.hit_rate) == (0, 0, 0.0)
    assert cache(conn, scope(), projects).write_1h == 7


def test_a_label_selector_and_a_day_window_select_the_exact_totals(conn):
    put(
        conn,
        "mine",
        session=A,
        requests=(
            on_day(request(1, 100), "2026-10-01"),
            on_day(request(3, 200, output=20), "2026-10-05"),
        ),
    )
    put(
        conn,
        "other",
        session=B,
        cwd="/elsewhere",
        requests=(on_day(request(9, 400), "2026-10-05"),),
    )
    projects = {("claude-code", A): ProjectInfo("k", "proj", "/w")}
    picked = scope(selectors=("proj",), since=date(2026, 10, 4))
    found = totals(conn, picked, projects, [])
    assert (found.sessions, found.requests) == (1, 1)
    assert (found.tokens.input, found.tokens.output, found.tokens.total) == (200, 20, 220)


def test_rollup_tokens_match_the_usage_reader_when_non_claude_requests_are_present(tmp_path):
    root = tmp_path / "claude"
    records = claude_records(A, ("ls", "pwd"))
    assistants = [r for r in records if r["type"] == "assistant"]
    for i, (model, usage) in enumerate(
        (
            ("<synthetic>", {"input_tokens": 1000, "output_tokens": 1000}),
            ("gpt-5", {"input_tokens": 2000, "output_tokens": 2000}),
            (
                "claude-opus-5",
                {
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "cache_read_input_tokens": 11,
                    "cache_creation_input_tokens": 13,
                },
            ),
        )
    ):
        extra = {
            **assistants[0],
            "uuid": f"{A}-x{i}",
            "requestId": f"{A}-xr{i}",
            "message": {
                **assistants[0]["message"],
                "id": f"{A}-xm{i}",
                "model": model,
                "usage": usage,
            },
        }
        records.append(extra)
    write_jsonl(root / "-w" / f"{A}.jsonl", records)
    roots = ingest.Roots(root, tmp_path / "codex", tmp_path / "archive")
    stats = ingest.prepare_index(
        tmp_path / "index",
        refresh=True,
        rebuild=False,
        roots=roots,
        now=NOW,
        notice=lambda m: None,
    )
    reader = list(ClaudeUsageReader(projects_dir=root).records())
    with closing(store.connect(Path(stats.path), read_only=True)) as index:
        found = totals(index, scope(), {}, []).tokens
    assert (found.input, found.output, found.cache_read, found.cache_write) == (
        sum(r.input for r in reader),
        sum(r.output for r in reader),
        sum(r.cache_read for r in reader),
        sum(r.cache_write for r in reader),
    )
    assert (found.input, found.output, found.cache_read, found.cache_write) == (27, 13, 11, 13)


def test_corpus_rollups_match_one_dimension_at_a_time(conn):
    items = specs("mix", bash("ls"), fail("boom"))
    reqs = (request(1, 100), request(2, 250))
    put(conn, "t", requests=reqs, tool_calls=items, compactions=(compaction(1),))
    cx_reqs = (request(3, 15, output=10),)
    put(conn, "cx", session=B, agent="codex", requests=cx_reqs, compactions=(compaction(2),))
    stamp = "2026-10-01T10:00:00Z"
    extra = [
        UsageRecord("pi", "p1", "/w/pi", stamp, "anthropic", "claude-opus-5", 10, 5, 0, 0, 0, 15),
        UsageRecord("pi", "p2", "/w/pj", stamp, "openai", "gpt-6", 7, 3, 0, 0, 0, 10),
    ]
    projects = {("claude-code", A): ProjectInfo("k", "proj", "/r")}
    dims = ("project", "folder", "agent", "tool", "model")

    found = rollups.corpus_rollups(conn, scope(), projects, extra, dims)

    assert (found.totals.tokens.total, found.totals.context.compactions) == (420, 2)
    assert found.totals == totals(conn, scope(), projects, extra)
    assert found.cache == cache(conn, scope(), projects)
    assert found.dimensions == {d: rollup(conn, scope(), d, projects, extra) for d in dims}


def test_corpus_rollups_scan_the_requests_once_for_every_dimension(conn):
    put(conn, "t", requests=(request(1, 100), request(2, 250)), tool_calls=specs("mix", bash("ls")))
    projects = {("claude-code", A): ProjectInfo("k", "proj", "/r")}
    seen = []
    conn.set_trace_callback(seen.append)

    rollups.corpus_rollups(conn, scope(), projects, [], ("project", "agent", "tool", "model"))
    conn.set_trace_callback(None)

    assert len([sql for sql in seen if "FROM requests" in sql and "ROW_NUMBER" in sql]) == 1


def test_an_inferred_compaction_counts_only_on_its_own_day(conn):
    dropped = (
        on_day(request(1, 150_000), "2026-10-01"),
        on_day(request(2, 20_000), "2026-10-01"),
        on_day(request(9, 30_000), "2026-10-18"),
    )
    early_call = replace(calls(bash("ls"))[0], timestamp="2026-10-01T10:00:20Z", line=4)
    late_call = replace(
        calls(bash("ls"), bash("pwd"))[1], timestamp="2026-10-18T10:00:00Z", line=12
    )
    put(conn, "t", requests=dropped, tool_calls=(early_call, late_call))
    assert totals(conn, scope(), {}, []).context.compactions == 1
    late = totals(conn, scope(since=date(2026, 10, 15)), {}, [])
    assert (late.requests, late.context.compactions) == (1, 0)


def test_worst_sessions_and_session_detail_agree_on_health(conn):
    put(conn, "main", tool_calls=specs("main", *([fail("make")] * 4), bash("ls")))
    put(
        conn,
        "sub",
        thread="agent-1",
        tool_calls=specs("sub", *([fail("make")] * 3)),
        first_ts="2026-09-01T10:00:00Z",
        last_ts="2026-09-01T10:05:00Z",
    )
    [worst] = rollups.worst_sessions(conn, scope(since=date(2026, 10, 1)), {}, NOW, None, 10)
    detail = rollups.session_detail(conn, "claude-code", A, {}, NOW, None, None)
    assert worst.health == detail.health
    assert (
        worst.subagent_signals
        == detail.subagent_signals
        == {"failure_streak": 1, "tool_failure": 3}
    )


def test_corpus_cache_matches_the_session_detail_cache(conn):
    rebuilt = replace(on_day(request(3, 100), "2026-10-02"), rebuild=True)
    put(conn, "t", requests=(on_day(request(1, 100), "2026-10-01"), rebuilt))
    found = rollups.corpus_rollups(conn, scope(), {}, [], ("model",))
    detail = rollups.session_detail(conn, "claude-code", A, {}, NOW, None, None)
    assert found.cache == detail.cache
    assert found.cache.rebuilds == 1


def test_tokens_only_records_read_archived_droid_sessions_when_asked(tmp_path):
    archive = tmp_path / "archive"
    transcript = archive / "-w" / "44444444-4444-4444-8444-444444444444.jsonl"
    transcript.parent.mkdir(parents=True)
    start = {"type": "session_start", "id": transcript.stem, "cwd": "/w"}
    transcript.write_text(json.dumps(start) + "\n", encoding="utf-8")
    usage = {"inputTokens": 100, "outputTokens": 10}
    transcript.with_name(transcript.stem + ".settings.json").write_text(
        json.dumps({"model": "auto", "tokenUsage": usage}), encoding="utf-8"
    )
    dirs = {"droid": {"sessions_dir": tmp_path / "live"}}
    archived = tokens_only(scope(include_archived=True), dirs, archive)
    assert [(r.source, r.total) for r in archived] == [("droid", 110)]
    assert tokens_only(scope(), dirs, archive) == []


def test_latest_day_follows_the_source_and_archive_scope(conn):
    put(conn, "c", requests=(on_day(request(1, 100), "2026-10-02"),))
    put(conn, "x", session=B, agent="codex", requests=(on_day(request(1, 100), "2026-10-05"),))
    put(conn, "old", session=C, archived=True, requests=(on_day(request(1, 100), "2026-10-09"),))
    claude = scope(agents=frozenset({"claude-code"}))
    assert rollups.latest_day(conn, claude) == "2026-10-02"
    assert rollups.latest_day(conn, scope()) == "2026-10-05"
    assert rollups.latest_day(conn, scope(include_archived=True)) == "2026-10-09"


def test_a_filter_keeps_its_own_selectors_after_another_is_built(conn):
    put(conn, "a", session=A, cwd="/one")
    put(conn, "b", session=B, cwd="/two")
    first = scope_filter(conn, scope(selectors=("/one",)), {})
    scope_filter(conn, scope(selectors=("/two",)), {})
    rows = conn.execute(
        f"SELECT t.session_id FROM transcripts t WHERE {first.where}", first.params
    ).fetchall()
    assert [r[0] for r in rows] == [A]


def test_a_non_positive_codex_context_window_leaves_the_fill_unknown(conn):
    zero = replace(request(1, 1000), context_window=0)
    negative = replace(request(3, 1000), context_window=-5)
    put(conn, "zero", agent="codex", cwd="/zero", requests=(zero,))
    put(conn, "negative", session=B, agent="codex", cwd="/negative", requests=(negative,))
    contexts = {r.key: (r.context.max, r.context.fill_p95) for r in rollup(conn, scope(), "folder")}
    assert contexts == {"/zero": (1000, None), "/negative": (1000, None)}
    detail = rollups.session_detail(conn, "codex", A, {}, NOW, None, None)
    assert (detail.context.window, detail.context.threads[0].fill_max) == (None, None)
    assert "context window unknown" in render.session_text(render.session_detail_json(detail))


def test_a_windowed_scan_reads_requests_and_calls_through_the_day_indexes(conn):
    put(conn, "t", requests=(on_day(request(1, 100), "2026-10-03"),))
    store.finish_cold_build(conn)
    seen = []
    conn.set_trace_callback(seen.append)
    found = rollups.corpus_rollups(conn, scope(since=date(2026, 10, 2)), {}, [], ())
    conn.set_trace_callback(None)
    plans = " ".join(
        str(tuple(step))
        for sql in seen
        if "requests" in sql or "tool_calls" in sql
        for step in conn.execute(f"EXPLAIN QUERY PLAN {sql}")
    )
    assert found.totals.requests == 1
    assert plans.count("USING INDEX requests_day") == 2
    assert plans.count("USING INDEX tool_calls_day") == 1


def test_a_session_ranks_from_exactly_the_minimum_number_of_calls(conn):
    enough = rollups.MIN_CALLS_FOR_RANKING
    put(conn, "ranked", session=A, tool_calls=specs("ranked", *([fail("make")] * enough)))
    put(conn, "short", session=B, tool_calls=specs("short", *([fail("make")] * (enough - 1))))
    worst = rollups.worst_sessions(conn, scope(), {}, NOW, None, 10)
    assert [(s.session_id, s.tool_calls) for s in worst] == [(A, enough)]
