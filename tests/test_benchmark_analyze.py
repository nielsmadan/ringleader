import hashlib
import json
import re
import stat
import threading
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ringleader.analysis import labels, store
from ringleader.analysis.facts import MAIN_THREAD
from ringleader.models.base import CallStats
from scripts import benchmark_analyze as bench
from tests.analysis.factories import fail
from tests.analysis.test_rollups import put, specs

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
NOW_ISO = NOW.isoformat(timespec="seconds")
HAIKU = bench.Candidate("claude", "haiku")
SONNET = bench.Candidate("claude", "sonnet")
LABEL = {"category": "loop", "friction": "none", "cause": "same command re-run"}
COMBOS = [(agent, thread) for agent in ("claude-code", "codex") for thread in (MAIN_THREAD, "sub")]


def failing(name):
    return specs(name, *(fail("make") for _ in range(3)))


def build_index(directory, per_combo=15):
    conn, _cold = store.open_index_for_write(directory, NOW.isoformat())
    for agent, thread in COMBOS:
        for i in range(per_combo):
            name = f"{agent}-{thread}-{i}"
            put(conn, name, agent=agent, thread=thread, tool_calls=failing(name))
    return conn


@pytest.fixture
def index_path(tmp_path):
    with closing(build_index(tmp_path / "idx")):
        pass
    return store.paths.analysis_index_path(store.FACTS_VERSION, tmp_path / "idx")


class FakeModel:
    name = "fake"

    def __init__(self, respond):
        self._respond = respond
        self._lock = threading.Lock()
        self.prompts = []

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        with self._lock:
            self.prompts.append(prompt)
        return self._respond(prompt)

    def complete_json_with_stats(self, prompt, schema):
        return self.complete_json(prompt, schema), CallStats(2.0, 100, 10, 0.001)


def sent_ids(prompt):
    return re.search(r"no others: ([^\n]*)", prompt).group(1).split(", ")


def label_all(prompt):
    return {"labels": [{"id": i, **LABEL} for i in sent_ids(prompt)]}


def nothing(prompt):
    return {"labels": []}


def runner(models):
    return bench.Runner(lambda candidate, timeout: models[candidate.spec], 3)


@pytest.fixture
def sampled(tmp_path, index_path):
    work = tmp_path / "work"
    work.mkdir()
    sample = bench.ensure_sample(work, index_path, seed=7, size=bench.SAMPLE_SIZE, now_iso=NOW_ISO)
    return work, sample


def rows_of(index_path):
    with closing(store.connect(index_path, read_only=True)) as index:
        return bench.sample_rows(index)


def test_the_sample_is_deterministic_stratified_and_split_disjointly(index_path):
    rows = rows_of(index_path)

    def draw(seed):
        return bench.draw_sample(rows, seed=seed, size=40, now_iso=NOW_ISO, source="x")

    first = draw(7)
    assert first == draw(7)
    assert first["splits"] != draw(8)["splits"]
    dev, heldout = first["splits"]["dev"], first["splits"]["heldout"]
    assert (len(dev), len(heldout)) == (16, 24)
    assert set(dev).isdisjoint(heldout)
    drawn = Counter(tuple(first["strata"][i]) for i in [*dev, *heldout])
    assert drawn == {("failure_streak", agent, thread): 10 for agent, thread in kinds_of(COMBOS)}


def kinds_of(combos):
    return [(agent, "main" if thread == MAIN_THREAD else "subagent") for agent, thread in combos]


def test_a_backup_copy_leaves_the_source_index_untouched(tmp_path):
    writer = build_index(tmp_path / "idx", per_combo=1)
    with closing(writer):
        source = store.paths.analysis_index_path(store.FACTS_VERSION, tmp_path / "idx")
        wal = source.with_name(source.name + "-wal")
        before = [(p.stat().st_mtime_ns, p.stat().st_size) for p in (source, wal)]
        copy = tmp_path / "work" / "index.sqlite"
        bench.copy_index(source, copy)
        after = [(p.stat().st_mtime_ns, p.stat().st_size) for p in (source, wal)]
        assert after == before
        assert writer.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == len(COMBOS)
    with closing(store.connect(copy, read_only=True)) as dst:
        assert dst.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == len(COMBOS)


def test_copying_an_idle_index_never_writes_the_source_file(tmp_path):
    with closing(build_index(tmp_path / "idx", per_combo=1)):
        pass
    source = store.paths.analysis_index_path(store.FACTS_VERSION, tmp_path / "idx")
    before = (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_size)
    bench.copy_index(source, tmp_path / "work" / "index.sqlite")
    after = (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_size)
    assert after == before
    names = {p.name for p in source.parent.iterdir()}
    sidecars = {source.name + "-wal", source.name + "-shm"}
    assert names - sidecars == {source.name}
    assert names <= {source.name, *sidecars}


def test_the_copy_and_its_work_dir_are_private(tmp_path):
    with closing(build_index(tmp_path / "idx", per_combo=1)):
        pass
    source = store.paths.analysis_index_path(store.FACTS_VERSION, tmp_path / "idx")
    work = tmp_path / "work"
    work.mkdir(mode=0o755)
    work.chmod(0o755)
    bench.copy_index(source, bench.work_index(work))
    bench.write_json(work / "gold.json", {})
    modes = [stat.S_IMODE(p.stat().st_mode) for p in (work, bench.work_index(work))]
    assert modes == [0o700, 0o600]
    assert stat.S_IMODE((work / "gold.json").stat().st_mode) == 0o600


def test_a_work_dir_that_is_a_symlink_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="is not a directory you own"):
        bench.private_dir(link)


def test_a_recorded_sample_recopies_a_missing_work_index(sampled, index_path):
    work, sample = sampled
    bench.work_index(work).unlink()
    again = bench.ensure_sample(work, index_path, seed=7, size=bench.SAMPLE_SIZE, now_iso=NOW_ISO)
    assert again == sample
    assert len(bench.load_episodes(work, bench.sample_ids(sample))) == 40


def test_a_recorded_sample_refuses_a_different_seed_or_index(sampled, index_path, tmp_path):
    work, _sample = sampled
    with pytest.raises(ValueError, match="pass the same --seed and --index"):
        bench.ensure_sample(work, index_path, seed=8, size=bench.SAMPLE_SIZE, now_iso=NOW_ISO)
    with pytest.raises(ValueError, match="pass the same --seed and --index"):
        bench.ensure_sample(
            work, tmp_path / "other.sqlite", seed=7, size=bench.SAMPLE_SIZE, now_iso=NOW_ISO
        )


def test_a_recorded_sample_accepts_the_same_index_by_another_path(sampled, index_path, monkeypatch):
    work, sample = sampled
    monkeypatch.chdir(index_path.parent)
    again = bench.ensure_sample(
        work, Path(index_path.name), seed=7, size=bench.SAMPLE_SIZE, now_iso=NOW_ISO
    )
    assert again == sample


def test_main_reports_a_seed_mismatch_and_exits(tmp_path, index_path, capsys):
    work = tmp_path / "w"
    args = ["--index", str(index_path), "--work", str(work), "--phase", "sample"]
    bench.main(args, now=NOW)
    capsys.readouterr()
    code = bench.main([*args, "--seed", "5"], now=NOW)
    assert code == 2
    assert "pass the same --seed and --index" in capsys.readouterr().err


def test_generate_caches_and_a_second_run_makes_no_calls(sampled):
    work, sample = sampled
    model = FakeModel(label_all)
    first = bench.generate(work, sample, [HAIKU], runner({HAIKU.spec: model}))
    assert (first[HAIKU.spec]["jobs"], first[HAIKU.spec]["calls"]) == (10, 10)
    assert len(model.prompts) == 10
    again = bench.generate(work, sample, [HAIKU], runner({HAIKU.spec: model}))
    assert (again[HAIKU.spec]["cached"], again[HAIKU.spec]["calls"]) == (10, 0)
    assert len(model.prompts) == 10


def test_unparseable_output_is_not_cached_and_the_next_run_retries(sampled):
    work, sample = sampled
    broken = bench.generate(work, sample, [HAIKU], runner({HAIKU.spec: FakeModel(nothing)}))
    assert (broken[HAIKU.spec]["calls"], broken[HAIKU.spec]["errors"]) == (10, 10)
    assert list((work / "generate").glob("*.json")) == []
    model = FakeModel(label_all)
    fixed = bench.generate(work, sample, [HAIKU], runner({HAIKU.spec: model}))
    assert (fixed[HAIKU.spec]["calls"], fixed[HAIKU.spec]["errors"]) == (10, 0)


def test_a_new_candidate_or_prompt_regenerates_only_what_changed(sampled):
    work, sample = sampled
    haiku, sonnet = FakeModel(label_all), FakeModel(label_all)
    models = {HAIKU.spec: haiku, SONNET.spec: sonnet}
    bench.generate(work, sample, [HAIKU], runner(models))
    both = bench.generate(work, sample, [HAIKU, SONNET], runner(models))
    assert (both[HAIKU.spec]["calls"], both[SONNET.spec]["calls"]) == (0, 10)
    changed = bench.PromptSpec(lambda batch: labels.episode_prompt(batch) + "\nbe terse")
    revised = bench.generate(work, sample, [HAIKU], runner(models), changed)
    assert revised[HAIKU.spec]["calls"] == 10
    bumped = bench.generate(work, sample, [HAIKU], runner(models), bench.PromptSpec(version=99))
    assert bumped[HAIKU.spec]["calls"] == 10
    restored = bench.generate(work, sample, [HAIKU], runner(models))
    assert restored[HAIKU.spec]["calls"] == 0


def test_annotate_saves_each_answer_and_stops_when_input_closes(sampled):
    work, sample = sampled
    answers = iter(["2", "3", "s"])

    def ask(_prompt):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    shown = []
    done = bench.annotate(work, sample, ask=ask, show=shown.append)
    first = bench.sample_ids(sample)[0]
    assert done == {"annotated": 1, "gold": 1, "missing": 39}
    assert bench.read_gold(work, sample) == {
        first: {"category": "detour", "friction": "buggy_code"}
    }
    assert any("category:" in line for line in shown)


def test_annotate_shows_excerpts_without_terminal_escapes(sampled):
    work, sample = sampled
    first = bench.sample_ids(sample)[0]
    with closing(store.connect(bench.work_index(work))) as conn:
        conn.execute(
            "UPDATE episodes SET excerpt = ? WHERE episode_id = ?",
            ("make\x1b]0;owned\x07 failed\nnext line", first),
        )
    shown = []

    def ask(_prompt):
        raise EOFError

    bench.annotate(work, sample, ask=ask, show=shown.append)
    [episode] = [line for line in shown if "failed" in line]
    assert "make]0;owned failed\nnext line" in episode


SAMPLE = {"seed": 1, "splits": {"dev": ["d1"], "heldout": ["h1", "h2", "h3", "h4", "h5"]}}
GOLD = {
    "d1": {"category": "loop", "friction": "none"},
    "h1": {"category": "loop", "friction": "none"},
    "h2": {"category": "loop", "friction": "none"},
    "h3": {"category": "detour", "friction": "wrong_approach"},
    "h4": {"category": "detour", "friction": "buggy_code"},
    "h5": {"category": "environment", "friction": "none"},
}


def results(predicted, seconds=(2.0, 4.0)):
    return {
        HAIKU.spec: {
            "labels": predicted,
            "calls": {
                "dev": [],
                "heldout": [
                    {"seconds": s, "inputTokens": 1, "outputTokens": 1, "costUsd": 0.5}
                    for s in seconds
                ],
            },
        }
    }


def label(category, friction):
    return {"category": category, "friction": friction, "cause": "x"}


def test_report_computes_accuracy_confusion_and_the_bar_verdict():
    predicted = {
        "h1": label("loop", "none"),
        "h2": label("detour", "none"),
        "h3": label("detour", "wrong_approach"),
        "h4": label("detour", "buggy_code"),
        "h5": label("environment", "excessive_changes"),
    }
    report = bench.build_report(SAMPLE, GOLD, results(predicted), [HAIKU], {})
    dev, held = report["rows"]
    assert (dev["split"], dev["n"], dev["outputs"], dev["meetsBar"]) == ("dev", 1, 0, None)
    assert (held["n"], held["outputs"]) == (5, 5)
    assert (held["category"], held["friction"]) == (0.8, 0.8)
    assert held["confusion"]["category"] == {
        "detour": {"detour": 2},
        "environment": {"environment": 1},
        "loop": {"detour": 1, "loop": 1},
    }
    assert (held["medianSeconds"], held["costUsd"], held["meetsBar"]) == (3.0, 1.0, True)


def test_report_fails_the_bar_counts_missing_outputs_wrong_and_waits_for_gold():
    predicted = {"h1": label("loop", "none"), "h2": label("loop", "buggy_code")}
    held = bench.build_report(SAMPLE, GOLD, results(predicted), [HAIKU], {})["rows"][1]
    assert (held["outputs"], held["category"], held["friction"]) == (2, 0.4, 0.2)
    assert held["confusion"]["category"]["environment"] == {bench.NONE_LABEL: 1}
    assert held["meetsBar"] is False
    partial = {i: g for i, g in GOLD.items() if i != "h5"}
    pending = bench.build_report(SAMPLE, partial, results(predicted), [HAIKU], {})["rows"][1]
    assert (pending["n"], pending["sampled"], pending["meetsBar"]) == (4, 5, None)


RECORD_META = {
    "date": "2026-10-19",
    "commit": "abc",
    "promptVersion": 1,
    "promptHash": "h",
    "factsVersion": 7,
    "seed": 1,
}


def test_the_rendered_record_carries_the_verdict():
    predicted = {i: label(g["category"], g["friction"]) for i, g in GOLD.items() if i != "d1"}
    report = bench.build_report(SAMPLE, GOLD, results(predicted), [HAIKU], RECORD_META)
    text = bench.render_markdown(report, "run")
    assert "| claude:haiku | heldout | 5/5 | 5 | 100% | 100% | 3.0 | 1.0000 | pass |" in text
    assert "| detour | 2 |" in text
    assert "this record holds counts only" in text


def test_the_rendered_record_leaves_out_episode_ids_and_causes():
    ids = {i: f"episode-sentinel-{i}" for i in GOLD}
    sample = {"seed": 1, "splits": {k: [ids[i] for i in v] for k, v in SAMPLE["splits"].items()}}
    gold = {ids[i]: g for i, g in GOLD.items()}
    predicted = {ids[i]: {**g, "cause": "cause-sentinel: rm -rf build"} for i, g in GOLD.items()}
    report = bench.build_report(sample, gold, results(predicted), [HAIKU], RECORD_META)
    text = bench.render_markdown(report, "run")
    assert "| claude:haiku | heldout | 5/5 |" in text
    assert "episode-sentinel" not in text
    assert "cause-sentinel" not in text


def test_main_runs_every_phase_and_writes_the_record(tmp_path, index_path, capsys):
    work, out = tmp_path / "w", tmp_path / "out" / "run.md"
    bench.main(["--index", str(index_path), "--work", str(work), "--phase", "sample"], now=NOW)
    capsys.readouterr()
    sample = json.loads((work / "sample.json").read_text(encoding="utf-8"))
    gold = {i: {"category": "loop", "friction": "none"} for i in bench.sample_ids(sample)}
    gold_file = tmp_path / "gold.json"
    gold_file.write_text(json.dumps(gold), encoding="utf-8")
    model = FakeModel(label_all)
    code = bench.main(
        [
            "--index",
            str(index_path),
            "--work",
            str(work),
            "--gold",
            str(gold_file),
            "--out",
            str(out),
        ],
        make_model=lambda candidate, timeout: model,
        now=NOW,
    )
    summary = json.loads(capsys.readouterr().out)
    assert code == 0
    assert summary["goldImport"] == {"read": 40, "kept": 40}
    assert summary["generate"]["claude:haiku"]["calls"] == 10
    assert "| claude:haiku | heldout | 24/24 | 24 | 100% | 100% | 2.0 | 0.0060 | pass |" in (
        out.read_text(encoding="utf-8")
    )


def test_a_missing_index_exits_with_a_message(tmp_path, capsys):
    code = bench.main(["--index", str(tmp_path / "none.sqlite"), "--work", str(tmp_path / "w")])
    assert code == 2
    assert "no analysis index at" in capsys.readouterr().err


def test_the_index_flag_takes_the_index_dir_like_rl_analyze(tmp_path, index_path, capsys):
    work = tmp_path / "w"
    code = bench.main(
        ["--index", str(index_path.parent), "--work", str(work), "--phase", "sample"], now=NOW
    )
    capsys.readouterr()
    sample = json.loads((work / "sample.json").read_text(encoding="utf-8"))
    assert code == 0
    assert sample["index"] == str(index_path)
