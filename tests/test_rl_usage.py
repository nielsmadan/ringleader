import json
from pathlib import Path

import pytest
from ringleader.commands import usage as usage_cmd
from ringleader.usage.base import UsageRecord


def _rec(  # noqa: PLR0913, PLR0917 -- test data-builder helper, one arg per fixture field
    source="claude",
    session="s1",
    project="p",
    ts="2026-08-17T10:00:00Z",
    model="claude-opus-5",
    inp=100,
    out=20,
    cr=500,
    cw=50,
    reasoning=0,
    cost=None,
):
    return UsageRecord(
        source=source,
        session_id=session,
        project=project,
        timestamp=ts,
        provider=None,
        model=model,
        input=inp,
        output=out,
        cache_read=cr,
        cache_write=cw,
        reasoning=reasoning,
        total=inp + out + cr + cw,
        cost=cost,
    )


@pytest.fixture
def fake_records(monkeypatch):
    recs = []
    monkeypatch.setattr(usage_cmd, "collect", lambda sources, dirs=None, **_: list(recs))
    return recs


def test_json_output_totals_and_rows(fake_records, capsys):
    fake_records += [_rec(project="alpha"), _rec(project="beta", inp=1, out=1, cr=1, cw=1)]

    rc = usage_cmd.cmd_usage(["--all", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["total_tokens"] == 670 + 4
    assert {r["key"] for r in out["rows"]} == {"alpha", "beta"}
    assert out["by"] == "project"


def test_by_session_and_source_grouping(fake_records, capsys):
    fake_records += [_rec(source="codex", session="c1"), _rec(source="pi", session="p1")]

    usage_cmd.cmd_usage(["--all", "--by", "source", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert {r["key"] for r in out["rows"]} == {"codex", "pi"}


def test_breakdown_includes_every_split(fake_records, capsys):
    fake_records += [_rec(reasoning=7)]

    usage_cmd.cmd_usage(["--all", "--breakdown", "--json"])

    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert row["input"] == 100 and row["output"] == 20
    assert row["cache_read"] == 500 and row["cache_write"] == 50
    assert row["reasoning"] == 7


def test_window_filters_by_day(fake_records, capsys):
    fake_records += [_rec(ts="2026-08-01T10:00:00Z"), _rec(ts="2026-08-17T10:00:00Z")]

    usage_cmd.cmd_usage(["--since", "2026-08-10", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert out["rows"][0]["records"] == 1


def test_until_bounds_the_window_and_is_echoed_as_a_day(fake_records, capsys):
    fake_records += [_rec(ts="2026-08-01T10:00:00Z", inp=1), _rec(ts="2026-08-17T10:00:00Z")]

    usage_cmd.cmd_usage(["--until", "2026-08-10", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert (out["since"], out["until"]) == (None, "2026-08-10")
    assert out["per_day"] == {"2026-08-01": 571}


@pytest.mark.parametrize("flag", ["--since", "--until"])
@pytest.mark.parametrize("value", ["2026-13-01", "2026-8-1", "last-week", "2026-08-10T00:00"])
def test_a_malformed_window_day_is_a_usage_error(fake_records, capsys, flag, value):
    with pytest.raises(SystemExit) as exit_info:
        usage_cmd.cmd_usage([flag, value, "--json"])

    assert exit_info.value.code == 2
    assert f"argument {flag}: {value!r} is not a YYYY-MM-DD date" in capsys.readouterr().err


def test_source_filter_is_passed_through(monkeypatch, capsys):
    seen = {}

    def fake_collect(sources, dirs=None, **_):
        seen["sources"] = sources
        return [_rec()]

    monkeypatch.setattr(usage_cmd, "collect", fake_collect)
    usage_cmd.cmd_usage(["--source", "codex", "--all", "--json"])

    assert seen["sources"] == ["codex"]


def test_text_output_is_human_readable(fake_records, capsys):
    fake_records += [_rec(project="alpha")]

    usage_cmd.cmd_usage(["--all"])

    text = capsys.readouterr().out
    assert "alpha" in text and "by project" in text


def test_empty_state(fake_records, capsys):
    usage_cmd.cmd_usage(["--all"])
    assert "no usage data" in capsys.readouterr().out


def test_costless_scope_reports_null_not_zero(fake_records, capsys):
    fake_records += [_rec(source="codex", model="gpt-5-codex")]

    usage_cmd.cmd_usage(["--all", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert out["total_cost"] is None


def test_empty_json_output_is_parseable(fake_records, capsys):
    usage_cmd.cmd_usage(["--all", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert out["total_tokens"] == 0
    assert out["total_cost"] is None
    assert out["rows"] == []


def test_mixed_priced_and_unpriced_scope_is_flagged_partial(fake_records, capsys):
    fake_records += [_rec(), _rec(source="codex", model=None)]

    usage_cmd.cmd_usage(["--all", "--by", "source", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert out["cost_partial"] is True
    assert out["total_cost"] is not None


def test_fully_priced_scope_is_not_flagged_partial(fake_records, capsys):
    fake_records += [_rec(), _rec(project="beta")]

    usage_cmd.cmd_usage(["--all", "--json"])

    assert json.loads(capsys.readouterr().out)["cost_partial"] is False


def test_partial_row_carries_its_own_marker(fake_records, capsys):
    fake_records += [_rec(project="alpha"), _rec(project="alpha", source="codex", model=None)]

    usage_cmd.cmd_usage(["--all", "--json"])

    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert row["key"] == "alpha"
    assert row["cost_partial"] is True


def test_text_total_says_the_cost_is_partial(fake_records, capsys):
    fake_records += [_rec(inp=10_000_000), _rec(source="codex", model=None)]

    usage_cmd.cmd_usage(["--all"])

    assert "partial" in capsys.readouterr().out


def test_archived_flag_adds_the_archive_records(monkeypatch, capsys):
    monkeypatch.setattr(
        usage_cmd,
        "collect",
        lambda sources, dirs=None, **_: [_rec(session="s-live", project="live")],
    )
    monkeypatch.setattr(
        usage_cmd,
        "collect_archived",
        lambda sources=None: [_rec(session="s-archived", project="archived")],
    )

    usage_cmd.cmd_usage(["--all", "--archived", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert {r["key"] for r in out["rows"]} == {"live", "archived"}


def test_archived_dedupes_a_session_present_in_both_trees(monkeypatch, tmp_path, capsys):
    def write_session(root):
        d = root / "-Users-x-wrksp-foo"
        d.mkdir(parents=True)
        (d / "dup-session.jsonl").write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "requestId": "r1",
                    "timestamp": "2026-08-17T10:00:00.000Z",
                    "cwd": "/Users/x/wrksp/foo",
                    "message": {
                        "id": "m1",
                        "model": "claude-opus-5",
                        "usage": {"input_tokens": 7, "output_tokens": 1},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

    live_dir = tmp_path / "live"
    archive_dir = tmp_path / "archive"
    write_session(live_dir)
    write_session(archive_dir)

    from ringleader.usage.claude import ClaudeUsageReader

    live_records = list(ClaudeUsageReader(projects_dir=live_dir).records())
    monkeypatch.setattr(usage_cmd, "collect", lambda sources, dirs=None, **_: list(live_records))
    monkeypatch.setattr(usage_cmd, "archive_root", lambda: archive_dir)

    usage_cmd.cmd_usage(["--all", "--archived", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert out["total_tokens"] == 8
    assert out["rows"][0]["records"] == 1


def test_archive_is_left_out_unless_asked_for(monkeypatch, capsys):
    monkeypatch.setattr(
        usage_cmd, "collect", lambda sources, dirs=None, **_: [_rec(project="live")]
    )
    monkeypatch.setattr(
        usage_cmd, "collect_archived", lambda sources=None: [_rec(project="archived")]
    )

    usage_cmd.cmd_usage(["--all", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert {r["key"] for r in out["rows"]} == {"live"}


def test_collect_archived_reads_the_archive_root(monkeypatch, tmp_path):
    d = tmp_path / "-Users-x-wrksp-foo"
    d.mkdir(parents=True)
    (d / "sess1.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "requestId": "r1",
                "timestamp": "2026-08-17T10:00:00.000Z",
                "cwd": "/Users/x/wrksp/foo",
                "message": {
                    "id": "m1",
                    "model": "claude-opus-5",
                    "usage": {"input_tokens": 7, "output_tokens": 1},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(usage_cmd, "archive_root", lambda: tmp_path)

    recs = usage_cmd.collect_archived()

    assert [r.input for r in recs] == [7]


def _write_claude_transcript(path: Path, *, inp: int, out: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "requestId": "r-claude",
                "timestamp": "2026-08-17T10:00:00.000Z",
                "cwd": "/Users/x/wrksp/foo",
                "message": {
                    "id": "m-claude",
                    "model": "claude-opus-5",
                    "usage": {"input_tokens": inp, "output_tokens": out},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _write_droid_session(path: Path, *, inp: int, out: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "session_start",
                "id": path.stem,
                "cwd": "/Users/x/wrksp/foo",
                "callingSessionId": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    usage = {"inputTokens": inp, "outputTokens": out}
    path.with_name(path.stem + ".settings.json").write_text(
        json.dumps({"model": "auto", "inclusiveTokenUsage": usage, "tokenUsage": usage}),
        encoding="utf-8",
    )
    path.with_name(path.stem + ".settings.json.bak").write_text("{}", encoding="utf-8")


def _write_codex_transcript(path: Path, *, inp: int, cached: int, out: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "timestamp": "2026-08-17T11:00:00.000Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": inp,
                            "cached_input_tokens": cached,
                            "cache_write_input_tokens": 0,
                            "output_tokens": out,
                            "reasoning_output_tokens": 0,
                            "total_tokens": inp + out,
                        },
                        "total_token_usage": {
                            "input_tokens": inp,
                            "cached_input_tokens": cached,
                            "cache_write_input_tokens": 0,
                            "output_tokens": out,
                            "reasoning_output_tokens": 0,
                            "total_tokens": inp + out,
                        },
                    },
                    "rate_limits": {},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_collect_reads_droid_sessions_through_the_reader_registry(tmp_path):
    _write_droid_session(
        tmp_path / "-Users-x-wrksp-foo" / "44444444-4444-4444-8444-444444444444.jsonl",
        inp=100,
        out=10,
    )

    recs = usage_cmd.collect(["droid"], dirs={"droid": {"sessions_dir": tmp_path}})

    assert [(r.source, r.total) for r in recs] == [("droid", 110)]


def test_collect_archived_covers_claude_codex_and_droid(monkeypatch, tmp_path):
    _write_claude_transcript(tmp_path / "-Users-x-wrksp-foo" / "sess-claude.jsonl", inp=9, out=1)
    _write_codex_transcript(
        tmp_path
        / "proj"
        / "rollout-2026-08-17T11-00-00-019fe602-6c5d-7190-abca-ce9c8c8543c1.jsonl",
        inp=20,
        cached=6,
        out=5,
    )
    _write_droid_session(
        tmp_path / "-Users-x-wrksp-bar" / "44444444-4444-4444-8444-444444444444.jsonl",
        inp=100,
        out=10,
    )
    monkeypatch.setattr(usage_cmd, "archive_root", lambda: tmp_path)

    recs = usage_cmd.collect_archived()
    by_source = {r.source: r for r in recs}

    assert by_source["claude"].total == 10
    assert (
        by_source["codex"].total == 25
    )  # 20 input (incl 6 cached) -> 14+6 + 5 out; total stays 25
    assert by_source["droid"].total == 110
    # No double counting: each file only yields its own source, sources differ, ids differ.
    assert {r.source for r in recs} == {"claude", "codex", "droid"}
    assert len({r.session_id for r in recs}) == 3


def test_archived_merges_claude_and_codex_without_double_counting(monkeypatch, tmp_path, capsys):
    _write_claude_transcript(tmp_path / "-Users-x-wrksp-foo" / "sess-claude.jsonl", inp=9, out=1)
    _write_codex_transcript(
        tmp_path
        / "proj"
        / "rollout-2026-08-17T11-00-00-019fe602-6c5d-7190-abca-ce9c8c8543c1.jsonl",
        inp=20,
        cached=6,
        out=5,
    )
    monkeypatch.setattr(usage_cmd, "archive_root", lambda: tmp_path)
    monkeypatch.setattr(usage_cmd, "collect", lambda sources, dirs=None, **_: [])

    usage_cmd.cmd_usage(["--all", "--archived", "--by", "source", "--json"])

    out = json.loads(capsys.readouterr().out)
    rows = {r["key"]: r for r in out["rows"]}
    assert rows["claude"]["tokens"] == 10
    assert rows["codex"]["tokens"] == 25
    assert out["total_tokens"] == 35


def test_ambiguous_session_prefix_refuses_instead_of_merging(monkeypatch, capsys):
    monkeypatch.setattr(
        usage_cmd,
        "collect",
        lambda sources, dirs=None, **_: [_rec(session="ab111"), _rec(session="ab222")],
    )

    rc = usage_cmd.cmd_usage(["session", "ab"])

    text = capsys.readouterr().out
    assert rc == 1
    assert "ambiguous" in text
    assert "ab111" in text and "ab222" in text


def test_unambiguous_session_prefix_still_reports(monkeypatch, capsys):
    monkeypatch.setattr(
        usage_cmd,
        "collect",
        lambda sources, dirs=None, **_: [_rec(session="ab111"), _rec(session="ab111")],
    )

    rc = usage_cmd.cmd_usage(["session", "ab"])

    assert rc == 0
    assert "ab111" in capsys.readouterr().out
