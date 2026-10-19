from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ringleader.analysis import ingest, render, rollups, store
from ringleader.analysis.rollups import Scope

from .factories import claude_records, write_jsonl

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
SID = "aaaaaaaa-2222-3333-4444-555555555555"
SCOPE = Scope(since=None, until=None, agents=frozenset(), selectors=(), include_archived=False)
CORPUS_KEYS = {
    "schema",
    "generated_at",
    "since",
    "until",
    "sources",
    "selectors",
    "coverage",
    "index",
    "totals",
    "cache",
    "rollups",
    "worst_sessions",
    "issues",
    "embed",
}


@pytest.fixture
def built(tmp_path):
    root = tmp_path / "claude"
    write_jsonl(
        root / "-w" / f"{SID}.jsonl",
        claude_records(SID, ("ls", "make", "make", "make", "make", "make"), failing=True),
    )
    roots = ingest.Roots(root, tmp_path / "codex", tmp_path / "archive")
    stats = ingest.prepare_index(
        tmp_path / "index", refresh=True, rebuild=False, roots=roots, now=NOW, notice=lambda m: None
    )
    with closing(store.connect(Path(stats.path), read_only=True)) as conn:
        yield stats, conn


def corpus(stats, conn, **extra):
    found = rollups.corpus_rollups(conn, SCOPE, {}, [], rollups.DEFAULT_DIMENSIONS)
    return render.corpus_json(
        scope=SCOPE,
        generated_at=NOW,
        coverage={"claude-code": "full"},
        index=stats,
        totals=found.totals,
        cache=found.cache,
        rollups=found.dimensions,
        worst=rollups.worst_sessions(conn, SCOPE, {}, NOW, None, 5),
        issues=extra.get("issues", []),
        labels=extra.get("labels"),
        embed=extra.get("embed"),
    )


def test_corpus_json_keys_are_the_documented_set(built):
    doc = corpus(*built)
    assert set(doc) == CORPUS_KEYS
    assert set(doc["totals"]) == {
        "sessions",
        "requests",
        "calls",
        "errors",
        "error_rate",
        "tokens",
        "cache_hit_rate",
        "context",
    }
    assert set(doc["rollups"]) == set(rollups.DEFAULT_DIMENSIONS)
    assert set(doc["index"]) == {
        "path",
        "facts_version",
        "derive_version",
        "transcripts",
        "attempted",
        "changed",
        "removed",
        "failed",
        "failed_now",
        "rederived",
        "complete",
        "stale",
        "writable",
        "seconds",
    }


def test_labels_appear_only_when_given(built):
    doc = corpus(*built, issues=[{"issue_id": "i1"}], labels={"engine": None})
    assert set(doc) == CORPUS_KEYS | {"labels"}
    assert (doc["issues"], doc["labels"]) == ([{"issue_id": "i1"}], {"engine": None})


def test_row_keys_and_null_tokens_for_the_tool_rollup(built):
    doc = corpus(*built)
    tool = doc["rollups"]["tool"][0]
    assert set(tool) == {
        "key",
        "sessions",
        "calls",
        "errors",
        "error_rate",
        "tokens",
        "cache_hit_rate",
        "context",
    }
    assert (tool["key"], tool["tokens"], tool["context"]) == ("Bash", None, None)
    assert doc["totals"]["calls"] == 6


def test_worst_session_json_has_the_documented_keys(built):
    [worst] = corpus(*built)["worst_sessions"]
    assert set(worst) == {
        "agent",
        "session_id",
        "project",
        "folder",
        "started_at",
        "ended_at",
        "outcome",
        "health",
        "signals",
        "subagent_signals",
        "tool_calls",
        "tool_errors",
        "episode_tokens",
        "tokens",
    }
    assert set(worst["health"]) == {"score", "points", "per_100_calls", "penalties"}


def test_session_detail_has_the_documented_top_level_keys(built):
    detail = render.session_detail_json(
        rollups.session_detail(built[1], "claude-code", SID, {}, NOW, None, None)
    )
    assert set(detail) == {
        "schema",
        "agent",
        "session_id",
        "project",
        "folder",
        "git_branch",
        "transcripts",
        "started_at",
        "ended_at",
        "outcome",
        "health",
        "tokens",
        "cache",
        "context",
        "rewinds",
        "subagents",
        "tool_usage",
        "skills",
        "signals",
        "subagent_signals",
        "episodes",
        "user_messages",
        "issues",
    }
    assert set(detail["context"]) == {"window", "series", "threads", "compactions"}


def test_corpus_text_names_the_totals_and_the_worst_session(built):
    text = render.corpus_text(corpus(*built))
    assert "6 tool calls" in text
    assert f"claude-code:{SID[:8]}" in text
    assert "by project" in text


def test_session_text_prints_the_health_episode_and_evidence_lines(built):
    detail = render.session_detail_json(
        rollups.session_detail(built[1], "claude-code", SID, {}, NOW, None, None)
    )
    [episode] = detail["episodes"]
    lines = render.session_text(detail).splitlines()
    assert lines[0] == f"session claude-code:{SID}"
    assert lines[3:5] == [
        "  health 65/100  583.3 points per 100 calls",
        "    -15  tool_error_rate x6",
    ]
    head = lines.index("  episodes") + 1
    assert lines[head : head + 3] == [
        (
            f"    {episode['episode_id'][:8]}  failure_streak,retry_run,runaway_window"
            "  lines 2-12  6 calls  90 tokens"
        ),
        "           2  error   Bash  ls",
        "           4  error   Bash  make",
    ]


def test_corpus_text_prints_the_worst_session_line_and_caps_a_long_rollup(built):
    doc = corpus(*built)
    doc["rollups"]["tool"] = doc["rollups"]["tool"] * (render.ROW_LIMIT + 2)
    lines = render.corpus_text(doc).splitlines()
    worst = lines.index("  worst sessions") + 2
    tool = lines.index("  by tool")
    assert lines[worst] == f"     65      583.3  unknown    claude-code:{SID[:8]}  /w" + (
        "  tool_error_rate -15, runaway_window -10"
    )
    assert lines[tool + 2 + render.ROW_LIMIT] == "    ... 2 more"


def test_issues_text_lists_each_issue_by_name():
    doc = {"issues": [{"name": "Flaky make", "sessions": 3, "occurrences": 7, "signatures": ["s"]}]}
    assert "3 sessions" in render.issues_text(doc)
    assert "Flaky make" in render.issues_text(doc)
    assert render.issues_text({"issues": []}) == "no recurring issues"


def test_issues_and_embed_are_always_present(built):
    doc = corpus(*built, embed=render.embed_json(False, "extra not installed"))
    assert doc["embed"] == {"used": False, "reason": "extra not installed"}
    assert (corpus(*built)["issues"], corpus(*built)["embed"]) == ([], None)


def test_issues_text_prints_the_embed_reason():
    doc = {
        "issues": [{"name": None, "sessions": 2, "occurrences": 3, "signatures": ["sig"]}],
        "embed": {"used": False, "reason": "extra not installed"},
    }
    assert render.issues_text(doc).splitlines() == [
        "recurring issues",
        "     2 sessions      3 occurrences  sig",
        "exact signatures only: extra not installed",
    ]


def test_session_text_strips_terminal_control_sequences(built):
    detail = render.session_detail_json(
        rollups.session_detail(built[1], "claude-code", SID, {}, NOW, None, None)
    )
    hostile = "\x1b]52;c;cGFzdGU=\x07ls\x9b2J"
    detail["folder"] = hostile
    detail["episodes"][0]["evidence"][0]["input_preview"] = hostile
    text = render.session_text(detail)
    assert text.count("]52;c;cGFzdGU=ls2J") == 2
    assert {c for c in text if not c.isprintable()} == {"\n"}


def test_corpus_text_strips_control_sequences_from_rollup_keys(built):
    doc = corpus(*built)
    doc["rollups"]["project"][0]["key"] = "/w\x1b[2Jx"
    assert "/w[2Jx" in render.corpus_text(doc)
