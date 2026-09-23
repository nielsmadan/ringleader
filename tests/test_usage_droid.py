import json
import os

from ringleader.usage.droid import DroidUsageReader
from ringleader.usage.pricing import estimate

PROJECT = "-Users-x-wrksp-foo"
CWD = "/Users/x/wrksp/foo"
PARENT = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"

USAGE = {
    "inputTokens": 1200,
    "outputTokens": 300,
    "cacheReadTokens": 40,
    "cacheCreationTokens": 7,
    "thinkingTokens": 120,
    "factoryCredits": 9.5,
}


def _usage(**overrides):
    out = dict(USAGE)
    out.update(overrides)
    return out


def _session(  # noqa: PLR0913 -- test data-builder helper, one arg per fixture field
    root,
    session_id,
    *,
    calling_session_id=None,
    usage=USAGE,
    model="gpt-5.6-sol",
    router=None,
    cwd=CWD,
    directory=PROJECT,
):
    d = root / directory if directory else root
    d.mkdir(parents=True, exist_ok=True)
    transcript = d / f"{session_id}.jsonl"
    start = {
        "type": "session_start",
        "id": session_id,
        "cwd": cwd,
        "title": "a droid thread",
        "callingSessionId": calling_session_id,
    }
    transcript.write_text(json.dumps(start) + "\n", encoding="utf-8")

    body = {"model": model, "assistantActiveTimeMs": 0}
    if usage is not None:
        body["tokenUsage"] = usage
        body["inclusiveTokenUsage"] = _usage(inputTokens=9_000_000)
    if router is not None:
        body["effectiveFactoryRouterModel"] = router
    _sidecar(transcript).write_text(json.dumps(body), encoding="utf-8")
    transcript.with_name(f"{session_id}.settings.json.bak").write_text("{}", encoding="utf-8")
    return transcript


def _sidecar(transcript):
    return transcript.with_name(transcript.stem + ".settings.json")


def _patch(transcript, **fields):
    path = _sidecar(transcript)
    body = json.loads(path.read_text(encoding="utf-8"))
    body.update(fields)
    path.write_text(json.dumps(body), encoding="utf-8")


def _copy_to(transcript, dest):
    dest.mkdir(parents=True, exist_ok=True)
    for p in (transcript, _sidecar(transcript)):
        (dest / p.name).write_bytes(p.read_bytes())
    return dest / transcript.name


def _move_to(transcript, dest):
    moved = _copy_to(transcript, dest)
    transcript.unlink()
    _sidecar(transcript).unlink()
    return moved


def test_a_top_level_session_yields_one_record_with_its_own_token_buckets(tmp_path):
    _session(tmp_path, PARENT)

    records = list(DroidUsageReader(sessions_dir=tmp_path).records())

    assert len(records) == 1
    r = records[0]
    assert (r.input, r.output, r.cache_read, r.cache_write) == (1200, 300, 40, 7)
    assert r.reasoning == 120
    assert r.total == 1200 + 300 + 40 + 7
    assert r.source == "droid" and r.session_id == PARENT
    assert r.provider == "factory" and r.model == "gpt-5.6-sol"
    assert r.project == "/Users/x/wrksp/foo"
    assert r.cost is None


def test_a_subagent_its_parent_claims_reports_its_own_tokens_beside_the_parent(tmp_path):
    parent = _session(tmp_path, PARENT, usage=_usage(inputTokens=5000))
    _session(tmp_path, CHILD, calling_session_id=PARENT, usage=_usage(inputTokens=4000))
    _patch(parent, childInclusiveTokenUsageBySessionId={CHILD: _usage(inputTokens=4000)})

    records = list(DroidUsageReader(sessions_dir=tmp_path).records())

    assert [(r.session_id, r.input) for r in records] == [(PARENT, 5000), (CHILD, 4000)]


def test_a_subagent_no_ancestor_claims_is_reported_with_its_own_token_usage(tmp_path):
    _session(tmp_path, PARENT, usage=_usage(inputTokens=5000))
    child = _session(
        tmp_path,
        CHILD,
        calling_session_id=PARENT,
        usage=_usage(inputTokens=4000),
        cwd="/Users/x/wrksp/bar",
    )
    _patch(child, tokenUsage=_usage(inputTokens=99, outputTokens=1, cacheReadTokens=0))

    records = list(DroidUsageReader(sessions_dir=tmp_path).records())

    assert [(r.session_id, r.input, r.project) for r in records] == [
        (PARENT, 5000, CWD),
        (CHILD, 99, "/Users/x/wrksp/bar"),
    ]
    assert records[1].total == 99 + 1 + 0 + 7


def test_a_subagents_total_is_the_same_whether_or_not_its_parent_is_in_the_store(tmp_path):
    """Archiving a parent moves it out of the live store and leaves its subagents behind.
    Attribution that depended on the parent being present would shift at that moment, and
    the two stores together would report the same tokens twice."""
    parent = _session(tmp_path, PARENT, usage=_usage(inputTokens=5000))
    _patch(parent, childInclusiveTokenUsageBySessionId={CHILD: _usage(inputTokens=4000)})
    _session(tmp_path, CHILD, calling_session_id=PARENT, usage=_usage(inputTokens=4000))

    def child_totals():
        reader = DroidUsageReader(sessions_dir=tmp_path)
        return [r.total for r in reader.records() if r.session_id == CHILD]

    beside_its_parent = child_totals()
    parent.unlink()
    _sidecar(parent).unlink()

    assert beside_its_parent == child_totals() == [4000 + 300 + 40 + 7]


def test_the_inclusive_figure_does_not_change_a_top_level_sessions_buckets(tmp_path):
    transcript = _session(tmp_path, PARENT)
    _patch(transcript, inclusiveTokenUsage=_usage(inputTokens=1, outputTokens=2, cacheReadTokens=3))

    record = next(iter(DroidUsageReader(sessions_dir=tmp_path).records()))

    assert (record.input, record.output, record.cache_read) == (1200, 300, 40)


def test_the_router_model_wins_over_the_configured_model(tmp_path):
    _session(
        tmp_path,
        PARENT,
        model="auto",
        router={"modelId": "claude-opus-5", "reasoningEffort": "high", "fallbacks": []},
    )

    record = next(iter(DroidUsageReader(sessions_dir=tmp_path).records()))

    assert record.model == "claude-opus-5"


def test_auto_is_reported_as_the_model_when_no_router_model_was_recorded(tmp_path):
    _session(tmp_path, PARENT, model="auto")

    record = next(iter(DroidUsageReader(sessions_dir=tmp_path).records()))

    assert record.model == "auto"


def test_a_null_router_model_falls_back_to_the_configured_model(tmp_path):
    _session(tmp_path, PARENT, model="kimi-k3", router=None)

    record = next(iter(DroidUsageReader(sessions_dir=tmp_path).records()))

    assert record.model == "kimi-k3"


def test_a_droid_record_is_never_priced_at_the_model_vendors_rates(tmp_path):
    _session(tmp_path, PARENT, model="claude-opus-5")

    record = next(iter(DroidUsageReader(sessions_dir=tmp_path).records()))

    assert record.model == "claude-opus-5"
    assert estimate(record) is None


def test_sidecars_without_a_usage_object_are_skipped_while_the_rest_of_the_run_reports(tmp_path):
    _sidecar(_session(tmp_path, "missing-sidecar")).unlink()
    _sidecar(_session(tmp_path, "broken-sidecar")).write_text("{not json", encoding="utf-8")
    _session(tmp_path, "no-usage-keys", usage=None)
    _session(tmp_path, PARENT)

    records = list(DroidUsageReader(sessions_dir=tmp_path).records())

    assert [r.session_id for r in records] == [PARENT]
    assert records[0].total == 1547


def test_a_sidecar_that_cannot_be_read_is_noted_on_stderr(tmp_path, capsys):
    sidecar = _sidecar(_session(tmp_path, PARENT))
    sidecar.write_text("{not json", encoding="utf-8")

    list(DroidUsageReader(sessions_dir=tmp_path).records())

    assert f"skipped {sidecar}: " in capsys.readouterr().err


def test_a_session_that_spent_nothing_is_still_reported_as_zero(tmp_path):
    _session(tmp_path, PARENT, usage=dict.fromkeys(USAGE, 0))

    records = list(DroidUsageReader(sessions_dir=tmp_path).records())

    assert [(r.session_id, r.total) for r in records] == [(PARENT, 0)]


def test_a_btw_fork_is_left_out_of_the_usage_report(tmp_path):
    _session(tmp_path, PARENT)
    _move_to(_session(tmp_path, CHILD), tmp_path / "btw")

    records = list(DroidUsageReader(sessions_dir=tmp_path).records())

    assert [r.session_id for r in records] == [PARENT]


def test_a_session_written_to_the_global_sessions_root_is_counted(tmp_path):
    """Droid's third layout: `sessions/<id>.jsonl`, with no launch-dir slug above it."""
    _session(tmp_path, PARENT)
    _session(tmp_path, CHILD, directory=None, usage=_usage(inputTokens=11))

    records = list(DroidUsageReader(sessions_dir=tmp_path).records())

    assert [(r.session_id, r.input) for r in records] == [(PARENT, 1200), (CHILD, 11)]


def test_a_transcript_with_no_sidecar_beside_it_is_passed_over_in_silence(tmp_path, capsys):
    """`rl usage --archived` reads a shared archive holding every source's transcripts, so
    an absent droid sidecar is the normal case, not something to report."""
    foreign = tmp_path / "other" / "aaa1.jsonl"
    foreign.parent.mkdir(parents=True)
    foreign.write_text(json.dumps({"type": "summary", "summary": "a claude thread"}) + "\n")

    records = list(DroidUsageReader(sessions_dir=tmp_path).records())

    assert records == []
    assert capsys.readouterr().err == ""


def test_one_session_filed_under_two_directories_is_reported_once(tmp_path):
    _copy_to(_session(tmp_path, PARENT), tmp_path / "-Users-x-wrksp-foo-old")

    records = list(DroidUsageReader(sessions_dir=tmp_path).records())

    assert [(r.record_id, r.total) for r in records] == [(f"droid|{PARENT}", 1547)]


def test_the_record_timestamp_tracks_the_transcripts_last_activity(tmp_path):
    transcript = _session(tmp_path, PARENT)
    os.utime(transcript, (1_771_000_000, 1_771_000_000))

    record = next(iter(DroidUsageReader(sessions_dir=tmp_path).records()))

    assert record.timestamp.startswith("2026-02-13T")


def test_the_record_id_is_stable_across_two_reads_of_the_same_store(tmp_path):
    _session(tmp_path, PARENT)
    reader = DroidUsageReader(sessions_dir=tmp_path)

    first = [r.record_id for r in reader.records()]
    second = [r.record_id for r in reader.records()]

    assert first == second == [f"droid|{PARENT}"]


def test_available_is_false_when_the_sessions_directory_is_absent(tmp_path):
    assert DroidUsageReader(sessions_dir=tmp_path / "nope").available() is False
    assert list(DroidUsageReader(sessions_dir=tmp_path / "nope").records()) == []
