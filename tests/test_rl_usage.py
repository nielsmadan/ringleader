import json

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
    monkeypatch.setattr(usage_cmd, "collect", lambda sources, dirs=None: list(recs))
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


def test_source_filter_is_passed_through(monkeypatch, capsys):
    seen = {}

    def fake_collect(sources, dirs=None):
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
        usage_cmd, "collect", lambda sources, dirs=None: [_rec(session="s-live", project="live")]
    )
    monkeypatch.setattr(
        usage_cmd, "collect_archived", lambda: [_rec(session="s-archived", project="archived")]
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
    monkeypatch.setattr(usage_cmd, "collect", lambda sources, dirs=None: list(live_records))
    monkeypatch.setattr(usage_cmd, "archive_root", lambda: archive_dir)

    usage_cmd.cmd_usage(["--all", "--archived", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert out["total_tokens"] == 8
    assert out["rows"][0]["records"] == 1


def test_archive_is_left_out_unless_asked_for(monkeypatch, capsys):
    monkeypatch.setattr(usage_cmd, "collect", lambda sources, dirs=None: [_rec(project="live")])
    monkeypatch.setattr(usage_cmd, "collect_archived", lambda: [_rec(project="archived")])

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


def test_ambiguous_session_prefix_refuses_instead_of_merging(monkeypatch, capsys):
    monkeypatch.setattr(
        usage_cmd,
        "collect",
        lambda sources, dirs=None: [_rec(session="ab111"), _rec(session="ab222")],
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
        lambda sources, dirs=None: [_rec(session="ab111"), _rec(session="ab111")],
    )

    rc = usage_cmd.cmd_usage(["session", "ab"])

    assert rc == 0
    assert "ab111" in capsys.readouterr().out
