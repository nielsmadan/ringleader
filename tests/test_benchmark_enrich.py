import json
import re
import threading
from collections import Counter
from datetime import UTC, datetime

from ringleader import registry
from ringleader.models.base import CallStats, ModelError
from scripts import benchmark_enrich as bench

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
NOW_ISO = NOW.isoformat(timespec="seconds")
RECENT = "2026-10-14T00:00:00+00:00"
MONTH = "2026-09-25T00:00:00+00:00"
OLD = "2026-01-01T00:00:00+00:00"
CANDIDATE = bench.Candidate("claude", "haiku")
OTHER = bench.Candidate("codex", None)
JUDGE = bench.Candidate("claude", "sonnet")
TRANSCRIPT_MARK = "TRANSCRIPT-TEXT-MARK"
PROMPT_MARK = "LAST-PROMPT-MARK"


def _thread(sid, *, source="claude-code", status="unclassified", what="", **fields):
    return {
        "sessionId": sid,
        "source": source,
        "project": f"/p/{sid}",
        "mtime": RECENT,
        "messageCount": 5,
        "status": status,
        "whatItIs": what,
        "suggestedName": f"name-{sid}" if what else None,
        "lastPrompt": PROMPT_MARK,
        **fields,
    }


def _labelled(sid, status="unfinished", **fields):
    thread = _thread(sid, status=status, what=f"work on {sid}", **fields)
    return {**thread, "classifiedAtMtime": thread["mtime"]}


def _sid(prompt):
    return re.search(r"project: /p/(\S+)", prompt).group(1)


class FakeModel:
    name = "fake"

    def __init__(self, respond):
        self._respond = respond
        self._lock = threading.Lock()
        self.prompts = []

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        return self.complete_json_with_stats(prompt, schema)[0]

    def complete_json_with_stats(self, prompt, schema):
        with self._lock:
            self.prompts.append(prompt)
        return self._respond(prompt), CallStats(2.0, 100, 10, 0.001)


def _classify(status="unfinished"):
    def respond(prompt):
        sid = _sid(prompt)
        return {"status": status, "whatItIs": f"does {sid}", "suggestedName": f"do-{sid}"}

    return respond


def _factory(models):
    return lambda candidate, timeout: models[candidate.spec]


def _sample(*, dev=(), heldout=(), backlog=(), pending=10):
    splits = {"dev": dev, "heldout": heldout, "backlog": backlog}
    return {
        "seed": 7,
        "drawnAt": NOW_ISO,
        "registry": "",
        "pendingCount": pending,
        "labelledCount": len(dev) + len(heldout),
        "dropped": {"labelled": 0, "backlog": 0},
        "splits": {k: [registry.qualified_id(t) for t in v] for k, v in splits.items()},
        "sessions": {registry.qualified_id(t): t for v in splits.values() for t in v},
    }


def _tail(thread, chars):
    return f"{TRANSCRIPT_MARK} {thread['sessionId']}"[-chars:]


def _generate(work, sample, candidates, models, **overrides):
    options = {
        "make_model": _factory(models),
        "tail": _tail,
        "tail_chars": 500,
        "concurrency": 3,
        **overrides,
    }
    return bench.generate(work, sample, candidates, bench.SPLITS, **options)


def _rows(work, sample, candidates):
    return bench.collect_rows(
        work,
        sample,
        candidates,
        bench.SPLITS,
        judge=JUDGE,
        tail=_tail,
        tail_chars=500,
        judge_tail_chars=2000,
    )


def _registry(threads):
    reg = registry.empty_registry()
    reg["threads"] = threads
    return reg


def _strata_registry():
    unfinished = [_labelled(f"u{i}") for i in range(10)]
    finished = [_labelled(f"f{i}", "finished", source="codex", mtime=OLD) for i in range(3)]
    blocked = [_labelled(f"b{i}", "blocked", mtime=MONTH) for i in range(2)]
    lone = [_labelled("lone", "active", source="codex")]
    pending = [_thread(f"p{i}", messageCount=i * 7) for i in range(30)]
    return _registry([*unfinished, *finished, *blocked, *lone, *pending])


def test_sample_is_deterministic_stratified_and_disjoint():
    draw = lambda seed: bench.draw_sample(  # noqa: E731
        _strata_registry(), now_iso=NOW_ISO, seed=seed, readable=lambda t: True, backlog_size=8
    )
    first, again = draw(3), draw(3)
    dev, heldout = set(first["splits"]["dev"]), set(first["splits"]["heldout"])
    labelled = {f"claude-code:u{i}" for i in range(10)} | {"claude-code:b0", "claude-code:b1"}
    labelled |= {f"codex:f{i}" for i in range(3)} | {"codex:lone"}

    assert first == again
    assert dev | heldout == labelled
    assert dev.isdisjoint(heldout)
    assert (len(dev), len(heldout)) == (6, 10)
    assert {"claude-code:b0", "claude-code:b1"} & dev
    assert {"claude-code:b0", "claude-code:b1"} & heldout
    assert {q for q in dev if q.startswith("codex:f")}
    assert {q for q in heldout if q.startswith("codex:f")}
    assert draw(4)["splits"] != first["splits"]


def test_a_stratum_of_one_goes_to_heldout():
    sample = bench.draw_sample(
        _strata_registry(), now_iso=NOW_ISO, seed=3, readable=lambda t: True, backlog_size=8
    )

    assert "codex:lone" in sample["splits"]["heldout"]


def test_backlog_is_stratified_and_unreadable_sessions_are_dropped_and_counted():
    reg = _strata_registry()
    reg["threads"] += [_thread(f"x{i}", source="codex", messageCount=60) for i in range(4)]
    unreadable = {"codex:x0", "codex:x1", "codex:x2", "claude-code:u3"}
    everything = bench.draw_sample(
        reg,
        now_iso=NOW_ISO,
        seed=3,
        readable=lambda t: registry.qualified_id(t) not in unreadable,
        backlog_size=40,
    )
    eight = bench.draw_sample(reg, now_iso=NOW_ISO, seed=3, readable=lambda t: True, backlog_size=8)
    sessions = eight["sessions"]
    buckets = Counter(
        (sessions[q]["source"], bench.message_bucket(sessions[q]))
        for q in eight["splits"]["backlog"]
    )

    assert Counter(q.split(":")[0] for q in everything["splits"]["backlog"]) == {
        "claude-code": 30,
        "codex": 1,
    }
    assert "codex:x3" in everything["splits"]["backlog"]
    assert everything["dropped"] == {"labelled": 1, "backlog": 3}
    assert everything["pendingCount"] == 34
    assert buckets == {
        ("claude-code", "<12"): 1,
        ("claude-code", "12-49"): 1,
        ("claude-code", ">=50"): 5,
        ("codex", ">=50"): 1,
    }


def test_allocate_gives_every_stratum_one_then_proportional_shares():
    quota = bench.allocate({("a",): 30, ("b",): 9, ("c",): 1}, 10)

    assert quota == {("a",): 7, ("b",): 2, ("c",): 1}


def test_generate_caches_successes_and_retries_failed_calls(tmp_path):
    flaky = {"s1"}

    def respond(prompt):
        sid = _sid(prompt)
        if sid in flaky:
            flaky.discard(sid)
            raise ModelError("rate limited")
        return _classify()(prompt)

    model = FakeModel(respond)
    sample = _sample(heldout=[_labelled("s0"), _labelled("s1")], backlog=[_thread("s2")])

    first = _generate(tmp_path, sample, [CANDIDATE], {CANDIDATE.spec: model})
    second = _generate(tmp_path, sample, [CANDIDATE], {CANDIDATE.spec: model})
    third = _generate(tmp_path, sample, [CANDIDATE], {CANDIDATE.spec: model})

    assert (first[CANDIDATE.spec]["calls"], first[CANDIDATE.spec]["errors"]) == (3, 1)
    assert first[CANDIDATE.spec]["errorMessages"] == {"ModelError: rate limited": 1}
    assert (second[CANDIDATE.spec]["calls"], second[CANDIDATE.spec]["errors"]) == (1, 0)
    assert (third[CANDIDATE.spec]["calls"], third[CANDIDATE.spec]["cached"]) == (0, 3)
    assert len(model.prompts) == 4


def test_an_unparseable_output_is_an_error_and_is_not_cached(tmp_path):
    model = FakeModel(lambda prompt: {"status": "done", "whatItIs": "x", "suggestedName": "y"})
    sample = _sample(heldout=[_labelled("s0")])

    first = _generate(tmp_path, sample, [CANDIDATE], {CANDIDATE.spec: model})
    second = _generate(tmp_path, sample, [CANDIDATE], {CANDIDATE.spec: model})

    assert first[CANDIDATE.spec]["errors"] == 1
    assert second[CANDIDATE.spec]["calls"] == 1


def test_a_prompt_change_regenerates_only_the_candidate_run_with_it(tmp_path):
    models = {CANDIDATE.spec: FakeModel(_classify()), OTHER.spec: FakeModel(_classify())}
    sample = _sample(heldout=[_labelled("s0"), _labelled("s1")])
    revised = lambda thread, tail: "revised\n" + bench.enrichment.enrich_prompt(thread, tail)  # noqa: E731

    _generate(tmp_path, sample, [CANDIDATE, OTHER], models)
    changed = _generate(tmp_path, sample, [CANDIDATE], models, prompt=revised)
    unchanged = _generate(tmp_path, sample, [OTHER], models)

    assert changed[CANDIDATE.spec]["calls"] == 2
    assert unchanged[OTHER.spec]["calls"] == 0
    assert len(models[CANDIDATE.spec].prompts) == 4
    assert len(models[OTHER.spec].prompts) == 2


VERDICTS = {
    "s1": (True, True, False, False),
    "s2": (False, True, True, False),
    "s3": (True, True, True, True),
    "s4": (False, True, True, True),
}


def _judge_model(verdicts=VERDICTS):
    def respond(prompt):
        status_ok, what_ok, name_ok, disagrees = verdicts[_sid(prompt)]
        return {
            "status_ok": status_ok,
            "what_ok": what_ok,
            "name_ok": name_ok,
            "label_disagrees": disagrees,
            "rationale": f"because {_sid(prompt)} " + "x" * 300,
        }

    return FakeModel(respond)


def _judged_report(tmp_path):
    heldout = [
        _labelled("s1", "finished"),
        _labelled("s2", "unfinished"),
        _labelled("s3", "finished"),
        _labelled("s4", "unfinished"),
    ]
    calls = {"s1": "finished", "s2": "finished", "s3": "unfinished", "s4": "unfinished"}

    def respond(prompt):
        sid = _sid(prompt)
        return {"status": calls[sid], "whatItIs": f"does {sid}", "suggestedName": f"do-{sid}"}

    sample = _sample(heldout=heldout, pending=100)
    models = {CANDIDATE.spec: FakeModel(respond), JUDGE.spec: _judge_model()}
    _generate(tmp_path, sample, [CANDIDATE], models)
    bench.run_judge(
        tmp_path, _rows(tmp_path, sample, [CANDIDATE]), judge=JUDGE, make_model=_factory(models)
    )
    return bench.build_report(
        tmp_path,
        sample,
        _rows(tmp_path, sample, [CANDIDATE]),
        candidates=[CANDIDATE],
        splits=("heldout",),
        judge=JUDGE,
        concurrency=4,
        meta={},
    )


def test_judge_verdicts_feed_pass_rates_and_the_confusion_matrix(tmp_path):
    entry = _judged_report(tmp_path)["results"][0]

    assert entry["rates"] == {"status": 0.5, "what": 1.0, "name": 0.75}
    assert entry["headline"] == {"status": 0.5, "mean": 0.75}
    assert entry["meetsBar"] is False
    assert entry["confusion"] == {
        "finished_called_finished": 1,
        "finished_called_open": 1,
        "open_called_finished": 1,
        "open_called_open": 1,
    }
    assert entry["latency"] == {"median": 2.0, "p90": 2.0}
    assert (entry["inputTokens"], entry["outputTokens"]) == (400, 40)


def test_report_projects_the_backlog_from_median_latency(tmp_path):
    projection = _judged_report(tmp_path)["projection"][CANDIDATE.spec]

    assert projection["seconds"] == 100 * 2.0 / 4
    assert round(projection["costUsd"], 6) == 0.1


def test_report_lists_every_disagreement_and_failed_field(tmp_path):
    found = {f["qid"]: f for f in _judged_report(tmp_path)["findings"]}

    assert sorted(found) == ["claude-code:s1", "claude-code:s2", "claude-code:s3", "claude-code:s4"]
    assert found["claude-code:s1"]["failed"] == ["name"]
    assert found["claude-code:s3"]["labelDisagrees"] is True
    assert found["claude-code:s4"]["failed"] == ["status"]
    assert len(found["claude-code:s1"]["rationale"]) == bench.RATIONALE_CHARS


def test_judge_errors_are_retried_on_the_next_run(tmp_path):
    sample = _sample(heldout=[_labelled("s1")])
    broken = FakeModel(lambda prompt: {"status_ok": True})
    models = {CANDIDATE.spec: FakeModel(_classify()), JUDGE.spec: broken}
    _generate(tmp_path, sample, [CANDIDATE], models)

    first = bench.run_judge(
        tmp_path, _rows(tmp_path, sample, [CANDIDATE]), judge=JUDGE, make_model=_factory(models)
    )
    models[JUDGE.spec] = _judge_model()
    second = bench.run_judge(
        tmp_path, _rows(tmp_path, sample, [CANDIDATE]), judge=JUDGE, make_model=_factory(models)
    )

    assert first[CANDIDATE.spec]["errors"] == 1
    assert second[CANDIDATE.spec] == {"calls": 1, "errors": 0, "errorMessages": {}}


def _report(tmp_path, sample, models):
    return bench.build_report(
        tmp_path,
        sample,
        _rows(tmp_path, sample, [CANDIDATE]),
        candidates=[CANDIDATE],
        splits=("heldout",),
        judge=JUDGE,
        concurrency=4,
        meta={},
    )["results"][0]


def test_meets_bar_is_unknown_until_every_row_is_judged(tmp_path):
    sample = _sample(heldout=[_labelled("s1"), _labelled("s2")])
    allgood = dict.fromkeys(("s1", "s2"), (True, True, True, False))
    models = {CANDIDATE.spec: FakeModel(_classify()), JUDGE.spec: _judge_model(allgood)}
    _generate(tmp_path, sample, [CANDIDATE], models)
    rows = _rows(tmp_path, sample, [CANDIDATE])
    bench.run_judge(tmp_path, rows[:1], judge=JUDGE, make_model=_factory(models))

    partial = _report(tmp_path, sample, models)
    bench.run_judge(
        tmp_path, _rows(tmp_path, sample, [CANDIDATE]), judge=JUDGE, make_model=_factory(models)
    )
    complete = _report(tmp_path, sample, models)

    assert (partial["judged"], partial["meetsBar"]) == (1, None)
    assert (complete["judged"], complete["meetsBar"]) == (2, True)


def test_judge_failures_reach_the_report_and_clear_when_retried(tmp_path):
    sample = _sample(heldout=[_labelled("s1")])
    models = {
        CANDIDATE.spec: FakeModel(_classify()),
        JUDGE.spec: FakeModel(lambda prompt: {"status_ok": True}),
    }
    _generate(tmp_path, sample, [CANDIDATE], models)
    bench.run_judge(
        tmp_path, _rows(tmp_path, sample, [CANDIDATE]), judge=JUDGE, make_model=_factory(models)
    )

    failed = _report(tmp_path, sample, models)
    models[JUDGE.spec] = _judge_model({"s1": (True, True, True, False)})
    bench.run_judge(
        tmp_path, _rows(tmp_path, sample, [CANDIDATE]), judge=JUDGE, make_model=_factory(models)
    )
    retried = _report(tmp_path, sample, models)

    assert failed["judgeErrors"] == 1
    assert list(failed["judgeErrorMessages"].values()) == [1]
    assert retried["judgeErrors"] == 0


def test_an_unreadable_judge_tail_leaves_the_row_unjudged(tmp_path):
    sample = _sample(heldout=[_labelled("s1"), _labelled("s2")])
    models = {CANDIDATE.spec: FakeModel(_classify())}
    _generate(tmp_path, sample, [CANDIDATE], models)

    def tail(thread, chars):
        if chars == 2000 and thread["sessionId"] == "s2":
            raise OSError("gone")
        return _tail(thread, chars)

    rows = bench.collect_rows(
        tmp_path,
        sample,
        [CANDIDATE],
        bench.SPLITS,
        judge=JUDGE,
        tail=tail,
        tail_chars=500,
        judge_tail_chars=2000,
    )

    assert [(r.qid, r.output is not None, r.judge_prompt is not None) for r in rows] == [
        ("claude-code:s1", True, True),
        ("claude-code:s2", True, False),
    ]


def test_identical_outputs_from_two_candidates_cost_one_judge_call(tmp_path):
    sample = _sample(heldout=[_labelled("s1")])
    judge_model = _judge_model({"s1": (True, True, True, False)})
    models = {
        CANDIDATE.spec: FakeModel(_classify()),
        OTHER.spec: FakeModel(_classify()),
        JUDGE.spec: judge_model,
    }
    _generate(tmp_path, sample, [CANDIDATE, OTHER], models)
    rows = _rows(tmp_path, sample, [CANDIDATE, OTHER])

    bench.run_judge(tmp_path, rows, judge=JUDGE, make_model=_factory(models))

    assert len(judge_model.prompts) == 1


def test_the_report_shows_when_the_cached_outputs_were_generated(tmp_path):
    sample = _sample(heldout=[_labelled("s1"), _labelled("s2")])
    stamps = iter(["2026-10-14T01:00:00+00:00", "2026-10-16T01:00:00+00:00"])
    models = {CANDIDATE.spec: FakeModel(_classify())}
    _generate(tmp_path, sample, [CANDIDATE], models, concurrency=1, clock=lambda: next(stamps))

    entry = _report(tmp_path, sample, models)

    assert entry["generatedAt"] == {
        "first": "2026-10-14T01:00:00+00:00",
        "last": "2026-10-16T01:00:00+00:00",
    }
    assert bench.generated_span(entry["generatedAt"]) == "2026-10-14 to 2026-10-16"


def test_the_spot_check_table_has_fifteen_rows(tmp_path):
    heldout = [_labelled(f"h{i}") for i in range(12)]
    backlog = [_thread(f"k{i}") for i in range(8)]
    sample = _sample(heldout=heldout, backlog=backlog)
    verdicts = {f"h{i}": (True, True, True, False) for i in range(12)}
    verdicts |= {f"k{i}": (True, True, True, False) for i in range(8)}
    models = {CANDIDATE.spec: FakeModel(_classify()), JUDGE.spec: _judge_model(verdicts)}
    _generate(tmp_path, sample, [CANDIDATE], models)
    bench.run_judge(
        tmp_path, _rows(tmp_path, sample, [CANDIDATE]), judge=JUDGE, make_model=_factory(models)
    )
    report = bench.build_report(
        tmp_path,
        sample,
        _rows(tmp_path, sample, [CANDIDATE]),
        candidates=[CANDIDATE],
        splits=bench.SPLITS,
        judge=JUDGE,
        concurrency=4,
        meta={
            "date": "2026-10-15",
            "commit": "abc",
            "promptLabel": "r0",
            "promptHash": "123",
            "tailChars": 500,
            "judgeTailChars": 2000,
        },
    )
    markdown = bench.render_markdown(report, title="haiku-r0", local=True)
    spot_section = markdown.split(f"## Spot check — {CANDIDATE.spec}")[1]
    spot_rows = [line for line in spot_section.splitlines() if line.startswith("| claude-code:")]

    assert len(report["spotCheck"][CANDIDATE.spec]) == bench.SPOT_CHECK_ROWS
    assert len(spot_rows) == bench.SPOT_CHECK_ROWS
    assert "| identity | candidate output | stored label | judge | human agrees? |" in markdown
    assert all(row.endswith("|  |") for row in spot_rows)


def _live(tmp_path):
    threads = [_labelled(f"l{i}", mtime=OLD if i % 2 else RECENT) for i in range(6)]
    threads += [_thread(f"p{i}", messageCount=i * 20) for i in range(5)]
    path = tmp_path / "live" / "registry.json"
    path.parent.mkdir()
    path.write_text(json.dumps(_registry(threads)), encoding="utf-8")
    return path


def _files(root):
    return {p for p in root.rglob("*") if p.is_file()}


def test_main_writes_only_into_the_work_dir_and_out(tmp_path, capsys):
    live = _live(tmp_path)
    live_bytes = live.read_bytes()
    before = _files(tmp_path)
    work, out = tmp_path / "work", tmp_path / "docs" / "run.md"
    verdicts = {f"l{i}": (True, True, True, False) for i in range(6)}
    verdicts |= {f"p{i}": (True, False, True, False) for i in range(5)}
    models = {CANDIDATE.spec: FakeModel(_classify()), JUDGE.spec: _judge_model(verdicts)}

    code = bench.main(
        [
            "--registry",
            str(live),
            "--work",
            str(work),
            "--out",
            str(out),
            "--candidates",
            CANDIDATE.spec,
            "--judge",
            JUDGE.spec,
        ],
        make_model=_factory(models),
        read_tail=_tail,
        now=NOW,
    )
    summary = json.loads(capsys.readouterr().out)
    written = _files(tmp_path) - before

    assert code == 0
    assert live.read_bytes() == live_bytes
    assert written - {out} == {p for p in written if p.is_relative_to(work)}
    assert out in written
    assert summary["generate"][CANDIDATE.spec]["calls"] == 11
    assert summary["report"]["sample"]["splits"]["backlog"]["n"] == 5


def test_the_committed_record_holds_no_content_and_the_local_report_holds_it(tmp_path, capsys):
    live = _live(tmp_path)
    out = tmp_path / "run.md"
    work = tmp_path / "w"
    verdicts = {f"l{i}": (False, True, True, True) for i in range(6)}
    verdicts |= {f"p{i}": (True, True, True, False) for i in range(5)}

    def echo(prompt):
        return {
            "status": "unfinished",
            "whatItIs": f"{TRANSCRIPT_MARK} {PROMPT_MARK} {_sid(prompt)}",
            "suggestedName": f"echo-{_sid(prompt)}",
        }

    models = {CANDIDATE.spec: FakeModel(echo), JUDGE.spec: _judge_model(verdicts)}

    bench.main(
        [
            "--registry",
            str(live),
            "--work",
            str(work),
            "--out",
            str(out),
            "--candidates",
            CANDIDATE.spec,
            "--judge",
            JUDGE.spec,
            "--prompt-label",
            "r0",
        ],
        make_model=_factory(models),
        read_tail=_tail,
        now=NOW,
    )
    record = out.read_text(encoding="utf-8")
    local_path = work / "reports" / "r0-dev-heldout-backlog.md"
    local = local_path.read_text(encoding="utf-8")
    capsys.readouterr()
    findings = [line for line in record.splitlines() if line.startswith("- `")]

    assert f"It is in `reports/{local_path.name}` in the `--work` dir" in record
    assert all(line.endswith("failed status; stored label disagrees") for line in findings)
    assert len(findings) == 6
    assert "echo-" in local
    assert f"{TRANSCRIPT_MARK} {PROMPT_MARK} l0" in local
    assert "because l0" in local
    assert "echo-" not in record
    assert TRANSCRIPT_MARK not in record
    assert PROMPT_MARK not in record
    assert "because" not in record
    assert "/p/l0" not in record
    assert "## Spot check — claude:haiku" not in record
    assert "## Spot check — claude:haiku" in local


def test_judge_prompt_marks_the_stored_label_as_a_possibly_wrong_reference():
    thread = _labelled("s1", "finished", title="ship it", aiTitle="Shipping")
    output = {"status": "finished", "whatItIs": "ships", "suggestedName": "ship"}

    labelled = bench.judge_prompt(thread, "tail", output, with_label=True, tail_chars=10)
    backlog = bench.judge_prompt(thread, "tail", output, with_label=False, tail_chars=10)

    assert bench.LABEL_WARNING in labelled
    assert "whatItIs: work on s1" in labelled
    assert "title: ship it" in labelled
    assert "ai title: Shipping" in labelled
    assert "Stored label" not in backlog


def test_candidate_specs_parse_engine_and_optional_model():
    assert bench.parse_candidates("claude:haiku, codex:") == (CANDIDATE, OTHER)
    assert bench.parse_splits("heldout,backlog") == ("heldout", "backlog")
    assert bench.parse_splits("all") == bench.SPLITS


def test_main_reuses_an_existing_sample(tmp_path, capsys):
    live = _live(tmp_path)
    work = tmp_path / "work"
    args = ["--registry", str(live), "--work", str(work), "--phase", "sample"]
    bench.main(args, make_model=_factory({}), read_tail=_tail, now=NOW)
    first = (work / "sample.json").read_text(encoding="utf-8")
    live.write_text(json.dumps(_registry([])), encoding="utf-8")

    bench.main([*args, "--seed", "99"], make_model=_factory({}), read_tail=_tail, now=NOW)
    capsys.readouterr()

    assert (work / "sample.json").read_text(encoding="utf-8") == first
    assert len(json.loads((work / "registry.json").read_text(encoding="utf-8"))["threads"]) == 11


def test_model_builder_launches_cli_candidates_through_the_prefix():
    build = bench.model_builder(["sops-exec"])

    claude = build(bench.Candidate("claude", "haiku"), 30.0)
    codex = build(bench.Candidate("codex", None), 30.0)

    assert (claude.command_prefix, codex.command_prefix) == (("sops-exec",), ("sops-exec",))
    assert (claude.model, claude.timeout) == ("haiku", 30.0)
