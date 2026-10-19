import fcntl
import io
import json
import os
import re
import sys
from pathlib import Path

import pytest
from ringleader import cli, commands, enrichment, paths
from ringleader.analysis import embed, ingest, labels, store
from ringleader.commands import analyze
from ringleader.models.base import ModelError
from ringleader.usage import aggregate
from ringleader.usage.codex import CodexUsageReader

from analysis.factories import write_claude

A = "aaaaaaaa-2222-3333-4444-555555555555"
B = "aaaaaaab-2222-3333-4444-555555555555"
C = "cccccccc-2222-3333-4444-555555555555"
CODEX = "01a0118b-f3a9-7453-af81-02d2d94a4e64"
CODEX_CHILD = "01a0118c-0000-7000-8000-000000000001"


@pytest.fixture(autouse=True)
def no_presence(monkeypatch):
    monkeypatch.setattr(analyze, "_running", lambda: None)


@pytest.fixture(autouse=True)
def no_embed_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "model2vec", None)


@pytest.fixture
def claude_dir(tmp_path):
    return tmp_path / "_claude"


@pytest.fixture
def index_dir(tmp_path):
    return tmp_path / "index"


def write_codex(tmp_path: Path) -> None:
    day = tmp_path / "_codex" / "2026" / "10" / "02"
    day.mkdir(parents=True)
    usage = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 50}
    records = [
        {"type": "session_meta", "timestamp": "2026-10-02T10:00:00Z", "payload": {"cwd": "/c"}},
        {
            "type": "event_msg",
            "timestamp": "2026-10-02T10:00:05Z",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {**usage, "total_tokens": 1050}},
            },
        },
    ]
    (day / f"rollout-2026-10-02T10-00-00-{CODEX}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )


def run(argv, index_dir, registry_path, capsys):
    code = cli.main(
        [
            "analyze",
            *argv,
            "--index",
            str(index_dir),
            "--registry",
            str(registry_path),
        ]
    )
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def corpus(tmp_path, claude_dir):
    write_claude(claude_dir, A, ("ls", "pwd", "id", "who", "date", "uname"), failing=True)
    write_claude(claude_dir, B, ("id",))
    write_claude(claude_dir, C, ("who",), project="-other")
    write_codex(tmp_path)
    return tmp_path / "registry.json"


def test_json_over_claude_and_codex_has_the_documented_keys(corpus, index_dir, capsys):
    code, out, _ = run(["--json", "--all"], index_dir, corpus, capsys)
    doc = json.loads(out)
    assert code == 0
    assert list(doc)[:3] == ["schema", "generated_at", "since"]
    assert {"coverage", "index", "totals", "cache", "rollups", "worst_sessions"} <= set(doc)
    assert doc["coverage"]["codex"] == "full"
    assert doc["coverage"]["claude-code"] == "full"
    assert list(doc["rollups"]) == ["project", "model", "skill", "tool"]
    assert doc["totals"]["tokens"]["total"] >= 1050


def test_a_second_run_reindexes_nothing(corpus, index_dir, capsys):
    run(["--json", "--all"], index_dir, corpus, capsys)
    _, out, _ = run(["--json", "--all"], index_dir, corpus, capsys)
    index = json.loads(out)["index"]
    assert (index["changed"], index["complete"], index["stale"]) == (0, True, False)


def test_source_claude_limits_the_coverage_to_claude(corpus, index_dir, capsys):
    both = json.loads(run(["--json", "--all"], index_dir, corpus, capsys)[1])
    only = json.loads(run(["--json", "--all", "--source", "claude"], index_dir, corpus, capsys)[1])
    assert only["sources"] == ["claude"]
    assert both["totals"]["tokens"]["total"] - only["totals"]["tokens"]["total"] == 1050
    assert (both["totals"]["sessions"], only["totals"]["sessions"]) == (4, 3)


def test_a_selector_filters_to_its_folder(corpus, index_dir, capsys):
    everything = json.loads(run(["--json", "--all"], index_dir, corpus, capsys)[1])
    one = json.loads(run(["--json", "--all", "/w"], index_dir, corpus, capsys)[1])
    assert one["selectors"] == ["/w"]
    assert (everything["totals"]["sessions"], one["totals"]["sessions"]) == (4, 3)


def test_by_selects_the_rollup_dimensions(corpus, index_dir, capsys):
    doc = json.loads(run(["--json", "--all", "--by", "model"], index_dir, corpus, capsys)[1])
    assert list(doc["rollups"]) == ["model"]


def test_text_output_lists_worst_sessions_with_score_and_points(corpus, index_dir, capsys):
    doc = json.loads(run(["--all", "--json"], index_dir, corpus, capsys)[1])
    health = next(s["health"] for s in doc["worst_sessions"] if s["session_id"] == A)
    code, out, _ = run(["--all"], index_dir, corpus, capsys)
    [line] = [line for line in out.splitlines() if f"claude-code:{A[:8]}" in line]
    score, points = line.split()[:2]
    assert code == 0
    assert "  worst sessions\n    score    pts/100  outcome\n" in out
    assert (int(score), float(points)) == (health["score"], round(health["per_100_calls"], 1))
    assert health["score"] < 100


def test_session_json_returns_the_detail_shape(corpus, index_dir, capsys):
    code, out, _ = run(["session", A[:12], "--json"], index_dir, corpus, capsys)
    doc = json.loads(out)
    assert code == 0
    assert (doc["agent"], doc["session_id"]) == ("claude-code", A)
    assert {"health", "tokens", "episodes", "tool_usage", "context"} <= set(doc)


def test_session_text_names_the_session(corpus, index_dir, capsys):
    code, out, _ = run(["session", A], index_dir, corpus, capsys)
    assert code == 0
    assert f"session claude-code:{A}" in out


def test_an_ambiguous_prefix_lists_matches_on_stderr_and_returns_one(corpus, index_dir, capsys):
    code, out, err = run(["session", "aaaaaaa"], index_dir, corpus, capsys)
    assert (code, out) == (1, "")
    assert A in err
    assert B in err


NO_DATA = (
    "no analysis data for session ffff; droid, opencode and pi sessions are counted for tokens"
    " only, see rl usage session ffff"
)


def test_an_unknown_session_is_a_clean_miss_on_stderr(corpus, index_dir, capsys):
    code, out, err = run(["session", "ffff"], index_dir, corpus, capsys)
    assert (code, out) == (1, "")
    assert err.strip() == NO_DATA


def test_no_refresh_without_an_index_exits_three(corpus, index_dir, capsys):
    code, out, err = run(["--no-refresh", "--json"], index_dir, corpus, capsys)
    doc = json.loads(out)
    assert code == store.EXIT_NO_INDEX
    assert doc["index"]["reason"] == "missing"
    assert doc["error"] in err


def test_no_refresh_without_an_index_prints_the_message_in_text_mode(corpus, index_dir, capsys):
    code, out, err = run(["--no-refresh"], index_dir, corpus, capsys)
    assert (code, out) == (3, "")
    assert "no complete analysis index" in err


def test_no_refresh_never_calls_the_extractor(corpus, index_dir, capsys, monkeypatch):
    run(["--json", "--all"], index_dir, corpus, capsys)
    seen = []
    monkeypatch.setattr(ingest, "extract", lambda ref: seen.append(ref) or None)
    code, _, _ = run(["--json", "--all", "--no-refresh"], index_dir, corpus, capsys)
    assert (code, seen) == (0, [])


def test_a_held_lock_with_a_complete_index_reads_stale(corpus, index_dir, capsys):
    run(["--json", "--all"], index_dir, corpus, capsys)
    with (index_dir / "index.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            code, out, _ = run(["--json", "--all"], index_dir, corpus, capsys)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    assert code == 0
    assert json.loads(out)["index"]["stale"] is True


def test_a_session_read_from_a_stale_index_says_so(corpus, index_dir, capsys):
    run(["--json", "--all"], index_dir, corpus, capsys)
    with (index_dir / "index.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            code, _, err = run(["session", A[:8]], index_dir, corpus, capsys)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    assert code == 0
    assert analyze.STALE_NOTICE in err


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod does not restrict root")
def test_an_unwritable_index_says_it_was_not_refreshed(corpus, index_dir, capsys):
    run(["--json", "--all"], index_dir, corpus, capsys)
    files = sorted(index_dir.iterdir())
    for p in files:
        p.chmod(0o400)
    index_dir.chmod(0o500)
    try:
        code, out, err = run(["--all"], index_dir, corpus, capsys)
    finally:
        index_dir.chmod(0o700)
        for p in files:
            p.chmod(0o600)
    assert code == 0
    assert "not writable" in out
    assert f"cannot write the analysis index in {index_dir}" in err


@pytest.mark.parametrize("flag", ["--worst", "--issues"])
def test_a_negative_count_is_a_usage_error(flag, corpus, index_dir, capsys):
    with pytest.raises(SystemExit) as exit_info:
        run(["--all", flag, "-1"], index_dir, corpus, capsys)
    assert exit_info.value.code == 2


def test_analyze_help_names_its_subcommands(capsys):
    with pytest.raises(SystemExit):
        cli.main(["analyze", "--help"])
    assert "session <id>, issues, reports, reports save" in capsys.readouterr().out


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod does not restrict root")
def test_an_unwritable_index_dir_with_a_complete_index_reports_unwritable(
    corpus, index_dir, capsys
):
    run(["--json", "--all"], index_dir, corpus, capsys)
    files = sorted(index_dir.iterdir())
    for p in files:
        p.chmod(0o400)
    index_dir.chmod(0o500)
    try:
        code, out, _ = run(["--json", "--all"], index_dir, corpus, capsys)
    finally:
        index_dir.chmod(0o700)
        for p in files:
            p.chmod(0o600)
    assert code == 0
    assert json.loads(out)["index"]["writable"] is False


def test_an_unwritable_index_dir_without_an_index_names_the_path(corpus, tmp_path, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    code, _, err = run(["--json", "--all"], blocker / "index", corpus, capsys)
    assert code == 1
    assert str(blocker / "index") in err


def test_index_flag_keeps_every_write_out_of_the_analysis_dir(corpus, index_dir, capsys):
    run(["--json", "--all"], index_dir, corpus, capsys)
    assert not paths.analysis_dir().exists()
    assert (index_dir / f"index-v{store.FACTS_VERSION}.sqlite").is_file()


def test_failing_extractors_return_one_after_printing_output(
    corpus, claude_dir, index_dir, capsys, monkeypatch
):
    for i in range(25):
        write_claude(claude_dir, f"{i:08d}-2222-3333-4444-555555555555", project="-bulk")

    def broken(ref, starts=None):
        raise ValueError("unparseable")

    monkeypatch.setattr(ingest, "extract", broken)
    code, out, err = run(["--json", "--all"], index_dir, corpus, capsys)
    assert code == 1
    index = json.loads(out)["index"]
    attempted = index["failed_now"]
    assert attempted == index["failed"] == len(list(claude_dir.rglob("*.jsonl"))) + 1
    assert (
        f"analysis: {attempted} of {attempted} changed transcripts failed to parse; see stderr"
        in err
    )


def test_a_parse_failure_on_a_terminal_clears_the_progress_line_first(
    corpus, index_dir, capsys, monkeypatch
):
    def broken(ref, starts=None):
        raise ValueError("unparseable")

    monkeypatch.setattr(ingest, "extract", broken)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    _, _, err = run(["--all"], index_dir, corpus, capsys)
    assert "\r\x1b[Krl analyze: could not index " in err
    assert "\ranalysis: indexed 1 of " in err


def test_a_default_run_builds_each_tokens_only_reader_once(corpus, index_dir, capsys, monkeypatch):
    built = []
    real = aggregate.READERS["pi"]

    def counted(**kwargs):
        built.append(kwargs)
        return real(**kwargs)

    monkeypatch.setitem(aggregate.READERS, "pi", counted)
    code, _, _ = run(["--json"], index_dir, corpus, capsys)
    assert (code, built) == (0, [{}])


def test_command_paths_include_analyze_and_its_session():
    found = commands.command_paths()
    assert {"analyze", "analyze session", "analyze issues"} <= found


def test_cli_dispatches_analyze(corpus, index_dir, capsys):
    code, out, _ = run(["--all"], index_dir, corpus, capsys)
    assert code == 0
    assert out.startswith("analysis - ")


def test_session_json_miss_is_a_json_error(corpus, index_dir, capsys):
    code, out, _ = run(["session", "ffff", "--json"], index_dir, corpus, capsys)
    doc = json.loads(out)
    assert code == 1
    assert (doc["error"], doc["matches"]) == (NO_DATA, [])


def test_session_json_ambiguity_lists_the_candidates(corpus, index_dir, capsys):
    code, out, _ = run(["session", "aaaaaaa", "--json"], index_dir, corpus, capsys)
    doc = json.loads(out)
    assert code == 1
    assert sorted(m["session_id"] for m in doc["matches"]) == [A, B]


@pytest.mark.parametrize("days", ["0", "-3"])
def test_days_below_one_is_a_usage_error(days, corpus, index_dir, capsys):
    with pytest.raises(SystemExit) as exit_info:
        run(["--days", days], index_dir, corpus, capsys)
    assert exit_info.value.code == 2


def test_all_with_since_is_a_usage_error(corpus, index_dir, capsys):
    with pytest.raises(SystemExit) as exit_info:
        run(["--all", "--since", "2026-10-01"], index_dir, corpus, capsys)
    assert exit_info.value.code == 2


def test_since_and_until_window_the_corpus(corpus, index_dir, capsys):
    everything = json.loads(run(["--json", "--all"], index_dir, corpus, capsys)[1])
    day = json.loads(
        run(
            ["--json", "--since", "2026-10-02", "--until", "2026-10-02"], index_dir, corpus, capsys
        )[1]
    )
    assert day["totals"]["tokens"]["total"] == 1050
    assert day["totals"]["sessions"] == 1
    assert everything["totals"]["sessions"] == 4


def test_days_anchors_on_the_latest_day_across_sources(corpus, index_dir, capsys):
    doc = json.loads(run(["--json", "--days", "1"], index_dir, corpus, capsys)[1])
    assert doc["since"] == "2026-10-02"
    assert doc["totals"]["tokens"]["total"] == 1050


def test_rebuild_reindexes_every_transcript(corpus, index_dir, capsys):
    run(["--json", "--all"], index_dir, corpus, capsys)
    _, out, _ = run(["--json", "--all", "--rebuild"], index_dir, corpus, capsys)
    assert json.loads(out)["index"]["changed"] == 4


def test_progress_ends_its_line_when_the_build_finishes(capsys):
    analyze._progress(1, 2)
    analyze._progress(2, 2)
    assert capsys.readouterr().err == (
        "\ranalysis: indexed 1 of 2 transcripts\ranalysis: indexed 2 of 2 transcripts\n"
    )


def test_codex_tokens_come_from_the_index_not_the_usage_reader(
    corpus, index_dir, capsys, monkeypatch
):
    def unused(self, since=None, session_prefix=None):
        raise AssertionError("codex is read through the analysis index")

    monkeypatch.setattr(CodexUsageReader, "records", unused)
    code, out, _ = run(["--json", "--all", "--source", "codex"], index_dir, corpus, capsys)
    doc = json.loads(out)
    assert code == 0
    assert (doc["totals"]["tokens"]["total"], doc["totals"]["sessions"]) == (1050, 1)


def test_session_resolves_a_codex_child_prefix_to_its_root(corpus, tmp_path, index_dir, capsys):
    day = tmp_path / "_codex" / "2026" / "10" / "02"
    spawn = {"subagent": {"thread_spawn": {"parent_thread_id": CODEX, "depth": 1}}}
    child = {
        "type": "session_meta",
        "timestamp": "2026-10-02T10:01:00Z",
        "payload": {"id": CODEX_CHILD, "session_id": CODEX, "source": spawn},
    }
    (day / f"rollout-2026-10-02T10-01-00-{CODEX_CHILD}.jsonl").write_text(
        json.dumps(child) + "\n", encoding="utf-8"
    )
    code, out, _ = run(["session", CODEX_CHILD[:13], "--json"], index_dir, corpus, capsys)
    doc = json.loads(out)
    assert code == 0
    assert (doc["agent"], doc["session_id"]) == ("codex", CODEX)
    assert [t["thread_id"] for t in doc["transcripts"]] == ["main", CODEX_CHILD]


@pytest.fixture
def failing_corpus(tmp_path, claude_dir):
    write_claude(claude_dir, A, ("ls", "ls"), failing=True)
    write_claude(claude_dir, B, ("ls",), failing=True)
    write_claude(claude_dir, C, ("who",), project="-other")
    return tmp_path / "registry.json"


def test_issues_json_returns_the_documented_shape(failing_corpus, index_dir, capsys):
    code, out, _ = run(["issues", "--json", "--all"], index_dir, failing_corpus, capsys)
    doc = json.loads(out)
    assert code == 0
    assert list(doc) == ["schema", "since", "until", "index", "embed", "issues"]
    assert doc["embed"] == {"used": False, "reason": embed.NOT_INSTALLED}
    (issue,) = doc["issues"]
    assert issue["sessions"] == 2
    assert issue["occurrences"] == 3
    assert issue["agents"] == ["claude-code"]
    assert {e["session_id"] for e in issue["example_sessions"]} == {A, B}
    assert issue["name"] is None
    assert issue["signatures"] == ["ls|exit_code|make: *** failed"]


def test_issues_min_sessions_and_limit_filter_the_list(failing_corpus, index_dir, capsys):
    strict = json.loads(
        run(
            ["issues", "--json", "--all", "--min-sessions", "3"], index_dir, failing_corpus, capsys
        )[1]
    )
    assert strict["issues"] == []
    lenient = json.loads(
        run(
            ["issues", "--json", "--all", "--min-sessions", "1"], index_dir, failing_corpus, capsys
        )[1]
    )
    assert lenient["issues"][0]["sessions"] == 2


def test_issues_text_lists_the_issue_and_why_embedding_was_not_used(
    failing_corpus, index_dir, capsys
):
    code, out, _ = run(["issues", "--all"], index_dir, failing_corpus, capsys)
    assert code == 0
    assert out.startswith("recurring issues\n")
    assert "2 sessions" in out
    assert out.rstrip().endswith(f"exact signatures only: {embed.NOT_INSTALLED}")


def test_download_embed_model_asks_for_a_download_once(
    failing_corpus, index_dir, capsys, monkeypatch
):
    asked = []

    def load(*, allow_download):
        asked.append(allow_download)
        return embed.EmbedLoad(None, "model not downloaded")

    monkeypatch.setattr(embed, "load_embedder", load)
    asked_for = run(
        ["issues", "--json", "--all", "--download-embed-model"], index_dir, failing_corpus, capsys
    )
    plain = run(["issues", "--json", "--all"], index_dir, failing_corpus, capsys)
    assert asked == [True, False]
    assert (asked_for[0], plain[0]) == (1, 0)
    assert (
        asked_for[2]
        .strip()
        .endswith("analysis: the embed model is not available: model not downloaded")
    )


class FailingEmbedder:
    def encode(self, texts):
        raise RuntimeError("boom")


class ShortEmbedder:
    def encode(self, texts):
        return [[1.0]]


@pytest.fixture
def two_signature_corpus(tmp_path, claude_dir):
    write_claude(claude_dir, A, ("ls", "who"), failing=True)
    write_claude(claude_dir, B, ("ls", "who"), failing=True)
    return tmp_path / "registry.json"


@pytest.mark.parametrize(
    "unused",
    [
        (FailingEmbedder(), "embedding failed: boom"),
        (ShortEmbedder(), "embedder returned 1 vectors for 2 signatures"),
    ],
)
def test_an_embedder_that_was_not_used_is_reported_with_the_reason(
    two_signature_corpus, index_dir, capsys, monkeypatch, unused
):
    embedder, reason = unused
    monkeypatch.setattr(
        embed, "load_embedder", lambda *, allow_download: embed.EmbedLoad(embedder, None)
    )
    issues_doc = json.loads(
        run(["issues", "--json", "--all"], index_dir, two_signature_corpus, capsys)[1]
    )
    corpus_doc = json.loads(run(["--json", "--all"], index_dir, two_signature_corpus, capsys)[1])
    expected = {"used": False, "reason": reason}
    assert (issues_doc["embed"], corpus_doc["embed"]) == (expected, expected)
    assert len(issues_doc["issues"]) == 2


def test_corpus_json_carries_issues_and_embed(failing_corpus, index_dir, capsys):
    doc = json.loads(run(["--json", "--all"], index_dir, failing_corpus, capsys)[1])
    assert doc["embed"] == {"used": False, "reason": embed.NOT_INSTALLED}
    assert [i["sessions"] for i in doc["issues"]] == [2]


def test_corpus_issues_zero_leaves_the_section_empty(failing_corpus, index_dir, capsys):
    doc = json.loads(
        run(["--json", "--all", "--issues", "0"], index_dir, failing_corpus, capsys)[1]
    )
    assert (doc["issues"], doc["embed"]) == ([], None)


def test_corpus_text_lists_recurring_issues(failing_corpus, index_dir, capsys):
    out = run(["--all"], index_dir, failing_corpus, capsys)[1]
    assert "  recurring issues\n" in out


class LabelModel:
    name = "claude"
    model = "haiku"

    def __init__(self):
        self.calls = 0

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        self.calls += 1
        if schema is labels.ISSUE_NAME_SCHEMA:
            return {"name": "make keeps failing", "summary": "The build fails each time."}
        ids = re.search(r"no others: ([^\n]*)", prompt).group(1).split(", ")
        if schema is labels.MESSAGE_LABEL_SCHEMA:
            return {"labels": [{"id": i, "kind": "other"} for i in ids]}
        return {
            "labels": [
                {"id": i, "category": "loop", "friction": "buggy_code", "cause": "make fails"}
                for i in ids
            ]
        }


class RefusingModel(LabelModel):
    def complete_json(self, prompt, schema):
        raise AssertionError("labelling was not asked for")


@pytest.fixture
def streak_corpus(tmp_path, claude_dir):
    write_claude(claude_dir, A, ("ls", "ls", "ls"), failing=True)
    write_claude(claude_dir, B, ("ls", "ls", "ls"), failing=True)
    return tmp_path / "registry.json"


@pytest.fixture
def label_model(monkeypatch):
    model = LabelModel()
    monkeypatch.setattr(analyze, "active_model", lambda cfg, task: model)
    return model


def test_json_without_label_never_calls_the_model(corpus, index_dir, capsys, monkeypatch):
    monkeypatch.setattr(analyze, "active_model", lambda cfg, task: RefusingModel())
    code, out, _ = run(["--json", "--all"], index_dir, corpus, capsys)
    doc = json.loads(out)
    assert code == 0
    assert doc["labels"]["note"] == labels.JSON_NOTE
    assert (doc["labels"]["scope"], doc["labels"]["pending"]) == ("none", 1)


def test_an_explicit_label_without_a_model_sets_the_note_and_exits_one(
    corpus, index_dir, capsys, monkeypatch
):
    monkeypatch.setattr(analyze, "active_model", lambda cfg, task: None)
    code, out, err = run(["--json", "--all", "--label", "recent"], index_dir, corpus, capsys)
    assert code == 1
    assert json.loads(out)["labels"]["note"] == labels.NO_MODEL_NOTE
    assert "the requested labelling did not complete" in err


def test_no_model_without_an_explicit_label_exits_zero(corpus, index_dir, capsys, monkeypatch):
    monkeypatch.setattr(analyze, "active_model", lambda cfg, task: None)
    code, out, _ = run(["--json", "--all"], index_dir, corpus, capsys)
    assert code == 0
    assert json.loads(out)["labels"]["note"] == labels.JSON_NOTE


class FailingModel(LabelModel):
    def complete_json(self, prompt, schema):
        self.calls += 1
        raise ModelError("model is down")


def test_an_explicit_label_whose_calls_all_fail_exits_one(
    streak_corpus, index_dir, capsys, monkeypatch
):
    model = FailingModel()
    monkeypatch.setattr(analyze, "active_model", lambda cfg, task: model)
    code, out, err = run(["--json", "--all", "--label", "recent"], index_dir, streak_corpus, capsys)
    run_doc = json.loads(out)["labels"]["run"]
    assert code == 1
    assert (run_doc["episodes"], run_doc["issues"]) == (0, 0)
    assert run_doc["failed"] == 3
    assert "the requested labelling did not complete" in err


class QuotaModel(LabelModel):
    def complete_json(self, prompt, schema):
        self.calls += 1
        raise ModelError("quota")


def test_failed_calls_and_a_tripped_breaker_are_reported_on_stderr(
    streak_corpus, index_dir, capsys, monkeypatch
):
    breaker = enrichment.Breaker
    monkeypatch.setattr(enrichment, "Breaker", lambda: breaker(limit=1))
    monkeypatch.setattr(analyze, "active_model", lambda cfg, task: QuotaModel())
    code, _, err = run(["--all", "--label", "recent"], index_dir, streak_corpus, capsys)
    errors = [line for line in err.splitlines() if line.startswith("  ")]
    assert code == 1
    assert errors
    assert {line.split(": ", 1)[1] for line in errors} == {"quota"}
    assert "stopped early: 1 calls failed in a row: quota" in err.splitlines()


class DiesAfterOneModel(LabelModel):
    def complete_json(self, prompt, schema):
        if self.calls == 0:
            return super().complete_json(prompt, schema)
        self.calls += 1
        raise ModelError("quota")


def test_a_breaker_that_trips_after_some_labels_were_stored_exits_one(
    streak_corpus, index_dir, capsys, monkeypatch
):
    breaker = enrichment.Breaker
    monkeypatch.setattr(enrichment, "Breaker", lambda: breaker(limit=1))
    monkeypatch.setattr(analyze, "active_model", lambda cfg, task: DiesAfterOneModel())
    code, _, err = run(["--all", "--label", "recent"], index_dir, streak_corpus, capsys)
    assert code == 1
    assert "stopped early: 1 calls failed in a row: quota" in err.splitlines()


def test_explicit_label_when_piped_prints_the_estimate_and_labels_without_asking(
    streak_corpus, index_dir, capsys, label_model
):
    code, out, err = run(["--all", "--label", "recent"], index_dir, streak_corpus, capsys)
    assert code == 0
    assert "labelling 2 episodes, 0 messages, 1 issues: ~2 calls, ~8s" in err
    assert "via claude (haiku)" in err
    assert label_model.calls == 2
    assert "make keeps failing" in out
    assert "labels: 2 episodes labelled, 0 not" in out


def test_ctrl_c_during_labelling_kills_model_children_and_propagates(
    streak_corpus, index_dir, capsys, label_model, monkeypatch
):
    killed: list[bool] = []

    def interrupt(*_args: object) -> labels.LabelOutcome:
        raise KeyboardInterrupt

    monkeypatch.setattr(labels, "run_labels", interrupt)
    monkeypatch.setattr(analyze.proc, "kill_all_children", lambda: killed.append(True))
    with pytest.raises(KeyboardInterrupt):
        run(["--all", "--label", "recent"], index_dir, streak_corpus, capsys)
    assert killed == [True]


def test_labels_survive_a_rebuild_and_rejoin(corpus, index_dir, capsys, label_model):
    code, out, _ = run(["session", A, "--label", "recent", "--json"], index_dir, corpus, capsys)
    assert code == 0
    assert json.loads(out)["labels"]["run"]["episodes"] == 1
    _, out, _ = run(["session", A, "--rebuild", "--json"], index_dir, corpus, capsys)
    doc = json.loads(out)
    assert doc["episodes"][0]["label"] == {
        "category": "loop",
        "friction": "buggy_code",
        "cause": "make fails",
        "model": "haiku",
    }
    assert doc["labels"]["labelled"] == 1


def test_issue_names_from_labels_show_in_analyze_issues(
    streak_corpus, index_dir, capsys, label_model
):
    run(["--json", "--all", "--label", "all"], index_dir, streak_corpus, capsys)
    doc = json.loads(run(["issues", "--json", "--all"], index_dir, streak_corpus, capsys)[1])
    assert (doc["issues"][0]["name"], doc["issues"][0]["summary"]) == (
        "make keeps failing",
        "The build fails each time.",
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod does not restrict root")
def test_an_unwritable_index_dir_skips_labelling_with_a_note(
    corpus, index_dir, capsys, label_model
):
    run(["--json", "--all"], index_dir, corpus, capsys)
    files = sorted(index_dir.iterdir())
    for p in files:
        p.chmod(0o400)
    index_dir.chmod(0o500)
    try:
        code, out, _ = run(["--json", "--all", "--label", "recent"], index_dir, corpus, capsys)
    finally:
        index_dir.chmod(0o700)
        for p in files:
            p.chmod(0o600)
    assert code == 1
    assert json.loads(out)["labels"]["note"] == (
        f"labels skipped: the analysis index dir {index_dir} is not writable"
    )
    assert label_model.calls == 0


@pytest.mark.parametrize("case", [((), 0), (("--yes",), 2)])
def test_an_interactive_run_asks_first_unless_yes(
    streak_corpus, index_dir, capsys, label_model, monkeypatch, case
):
    flags, calls = case
    monkeypatch.setattr(analyze, "_interactive", lambda: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))
    code, out, err = run(["--all", "--label", "recent", *flags], index_dir, streak_corpus, capsys)
    assert code == 0
    assert label_model.calls == calls
    assert ("continue? [Y/n]" in err) is not bool(flags)
    assert (analyze.DECLINED_NOTE in out) is not bool(flags)


def test_eof_at_the_prompt_declines(streak_corpus, index_dir, capsys, label_model, monkeypatch):
    monkeypatch.setattr(analyze, "_interactive", lambda: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code, out, _ = run(["--all", "--label", "recent"], index_dir, streak_corpus, capsys)
    assert code == 0
    assert label_model.calls == 0
    assert analyze.DECLINED_NOTE in out


@pytest.mark.parametrize("answer", ["\n", "y\n", "YES\n"])
def test_enter_or_yes_at_the_prompt_confirms(
    streak_corpus, index_dir, capsys, label_model, monkeypatch, answer
):
    monkeypatch.setattr(analyze, "_interactive", lambda: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
    code, _, _ = run(["--all", "--label", "recent"], index_dir, streak_corpus, capsys)
    assert code == 0
    assert label_model.calls == 2


def test_a_tty_run_inside_an_agent_session_defaults_to_no_labels(
    streak_corpus, index_dir, capsys, label_model, monkeypatch
):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Tty("y\n"))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    code, out, err = run(["--all"], index_dir, streak_corpus, capsys)
    assert code == 0
    assert label_model.calls == 0
    assert "continue? [Y/n]" not in err
    assert labels.NON_INTERACTIVE_NOTE in out


def reports_cli(argv, capsys, stdin=None, monkeypatch=None):
    if stdin is not None:
        assert monkeypatch is not None
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = cli.main(["analyze", "reports", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_command_paths_include_analyze_reports():
    assert "analyze reports" in commands.command_paths()


def test_reports_save_reads_stdin_and_prints_the_path(capsys, monkeypatch):
    code, out, _ = reports_cli(["save", "findings", "-"], capsys, "# Loops\n\nbody\n", monkeypatch)
    path = Path(out.strip())
    assert code == 0
    assert path.parent == paths.analysis_dir() / "findings"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\nkind: findings\n")
    assert text.endswith("---\n# Loops\n\nbody\n")


def test_reports_save_json_and_list_json(capsys, monkeypatch, tmp_path):
    source = tmp_path / "f.txt"
    source.write_text("# Found\n", encoding="utf-8")
    _, out, _ = reports_cli(["save", "findings", str(source), "--window", "w1", "--json"], capsys)
    saved = json.loads(out)
    _, listing, _ = reports_cli(["--json"], capsys)
    found = json.loads(listing)
    (row,) = found["reports"]
    assert (saved["schema"], found["schema"]) == (1, 1)
    assert row == {
        "report_id": saved["report_id"],
        "kind": "findings",
        "created": row["created"],
        "title": "Found",
        "window": "w1",
        "based_on": [],
        "path": saved["path"],
    }


def test_reports_kind_filter_and_limit(capsys, monkeypatch):
    reports_cli(["save", "findings", "-", "--title", "one"], capsys, "a\n", monkeypatch)
    reports_cli(["save", "recommendations", "-", "--title", "two"], capsys, "b\n", monkeypatch)
    _, out, _ = reports_cli(["--kind", "recommendations", "--json"], capsys)
    assert [r["title"] for r in json.loads(out)["reports"]] == ["two"]
    _, limited, _ = reports_cli(["--limit", "1", "--json"], capsys)
    assert len(json.loads(limited)["reports"]) == 1


def test_reports_save_with_unknown_based_on_exits_two_and_writes_nothing(capsys, monkeypatch):
    code, _, err = reports_cli(
        ["save", "recommendations", "-", "--based-on", "ghost"], capsys, "x\n", monkeypatch
    )
    assert code == 2
    assert err.strip() == "unknown report id: ghost; list the ids with rl analyze reports"
    assert not paths.analysis_reports_dir("recommendations").exists()


def test_reports_save_rejects_an_empty_body(capsys, monkeypatch):
    code, _, err = reports_cli(["save", "findings", "-"], capsys, "  \n", monkeypatch)
    assert (code, err.strip()) == (2, "report body is empty")


def test_reports_text_lists_ids_and_titles(capsys, monkeypatch):
    assert reports_cli([], capsys)[1].strip() == "no reports"
    reports_cli(["save", "findings", "-", "--title", "Hello"], capsys, "x\n", monkeypatch)
    out = reports_cli([], capsys)[1]
    assert out.strip().endswith("-findings-hello  Hello")


def _never_prepare(*_args, **_kwargs):
    raise AssertionError("the index was refreshed")


@pytest.mark.parametrize(("word", "verb"), [("issue", "issues"), ("sesion", "session")])
def test_a_mistyped_subcommand_fails_before_any_refresh(
    word, verb, corpus, index_dir, capsys, monkeypatch
):
    monkeypatch.setattr(ingest, "prepare_index", _never_prepare)
    with pytest.raises(SystemExit) as exit_info:
        run([word], index_dir, corpus, capsys)
    assert exit_info.value.code == 2
    assert f"did you mean '{verb}'?" in capsys.readouterr().err


@pytest.mark.parametrize("verb", [[], ["issues"]])
def test_a_bare_word_matching_no_project_fails_before_any_refresh(
    verb, corpus, index_dir, capsys, monkeypatch
):
    monkeypatch.setattr(ingest, "prepare_index", _never_prepare)
    with pytest.raises(SystemExit) as exit_info:
        run([*verb, "--project", "nosuchproject"], index_dir, corpus, capsys)
    assert exit_info.value.code == 2
    assert "no project or path matches 'nosuchproject'" in capsys.readouterr().err


def test_a_registered_project_label_is_a_selector(corpus, index_dir, capsys):
    thread = {"sessionId": A, "gitProjectLabel": "juggler", "project": "/w"}
    corpus.write_text(json.dumps({"threads": [thread]}), encoding="utf-8")
    code, out, _ = run(["--json", "--all", "juggler"], index_dir, corpus, capsys)
    assert code == 0
    assert json.loads(out)["totals"]["sessions"] == 1


def test_a_label_known_only_from_archived_sessions_is_a_selector(corpus, index_dir, capsys):
    thread = {"sessionId": A, "gitProjectLabel": "juggler", "project": "/w"}
    corpus.write_text(json.dumps({"threads": [], "archived": [thread]}), encoding="utf-8")
    code, _, _ = run(["--json", "--all", "juggler"], index_dir, corpus, capsys)
    assert code == 0


@pytest.mark.parametrize("argv", [[], ["session", A], ["issues"]])
def test_rebuild_with_no_refresh_is_a_usage_error(argv, corpus, index_dir, capsys):
    with pytest.raises(SystemExit) as exit_info:
        run([*argv, "--rebuild", "--no-refresh"], index_dir, corpus, capsys)
    assert exit_info.value.code == 2
    assert "--rebuild cannot be combined with --no-refresh" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["issues"]])
def test_since_later_than_until_is_a_usage_error(argv, corpus, index_dir, capsys):
    with pytest.raises(SystemExit) as exit_info:
        run([*argv, "--since", "2026-10-05", "--until", "2026-10-01"], index_dir, corpus, capsys)
    assert exit_info.value.code == 2
    assert "--since cannot be later than --until" in capsys.readouterr().err


@pytest.mark.parametrize(
    "window", [["--all"], ["--since", "2026-10-01"], ["--until", "2026-10-02"]]
)
def test_explicit_days_with_another_window_is_a_usage_error(window, corpus, index_dir, capsys):
    with pytest.raises(SystemExit) as exit_info:
        run(["--days", "3", *window], index_dir, corpus, capsys)
    assert exit_info.value.code == 2
    assert "--days cannot be combined" in capsys.readouterr().err


def test_until_alone_reads_all_history_up_to_that_day(corpus, index_dir, capsys):
    doc = json.loads(run(["--json", "--until", "2026-10-01"], index_dir, corpus, capsys)[1])
    assert (doc["since"], doc["until"]) == (None, "2026-10-01")
    assert doc["totals"]["sessions"] == 3


def test_source_accepts_the_claude_code_spelling_from_the_json(corpus, index_dir, capsys):
    short = json.loads(run(["--json", "--all", "--source", "claude"], index_dir, corpus, capsys)[1])
    long = json.loads(
        run(["--json", "--all", "--source", "claude-code"], index_dir, corpus, capsys)[1]
    )
    assert long["sources"] == ["claude"]
    assert long["totals"] == short["totals"]


def test_the_default_window_anchors_on_the_chosen_source(corpus, index_dir, capsys):
    doc = json.loads(
        run(["--json", "--days", "1", "--source", "claude"], index_dir, corpus, capsys)[1]
    )
    assert doc["since"] == "2026-10-01"
    assert doc["totals"]["sessions"] == 3


def test_session_json_lists_the_recurring_issues_it_hit(failing_corpus, index_dir, capsys):
    found = json.loads(run(["issues", "--json", "--all"], index_dir, failing_corpus, capsys)[1])
    _, out, _ = run(["session", A, "--json"], index_dir, failing_corpus, capsys)
    assert json.loads(out)["issues"] == [found["issues"][0]["issue_id"]]


@pytest.mark.parametrize(
    ("typed", "suggested"), [("save", "reports save"), ("sav", "reports save")]
)
def test_a_near_miss_of_save_suggests_the_full_reports_command(
    typed, suggested, corpus, index_dir, capsys
):
    with pytest.raises(SystemExit) as exit_info:
        run([typed], index_dir, corpus, capsys)
    assert exit_info.value.code == 2
    assert f"did you mean '{suggested}'?" in capsys.readouterr().err


def test_a_tty_with_stderr_redirected_is_not_interactive(monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr(sys, "stdout", Tty())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert analyze._interactive() is False
    monkeypatch.setattr(sys, "stderr", Tty())
    assert analyze._interactive() is True
