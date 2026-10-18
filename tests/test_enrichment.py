import gc
import threading
import time
import weakref

import pytest
from ringleader import enrichment, registry
from ringleader.enrichment import Classification, Classified
from ringleader.models.base import ModelError

MTIME = "2026-10-01T00:00:00+00:00"
LATER = "2026-10-02T00:00:00+00:00"
GOOD = {"status": "unfinished", "whatItIs": "work in progress", "suggestedName": "wip"}


def _thread(sid, **fields):
    return {
        "sessionId": sid,
        "source": "claude-code",
        "mtime": MTIME,
        "project": "/p",
        "status": "unclassified",
        "decision": None,
        **fields,
    }


def _registry(tmp_path, threads):
    reg = registry.empty_registry()
    reg["threads"] = threads
    path = tmp_path / "registry.json"
    registry.save_registry(path, reg)
    return str(path)


def _ids(n):
    return [("claude-code", f"s{i}") for i in range(n)]


def _classified_ids(path):
    return {
        t["sessionId"]
        for t in registry.load_registry(path)["threads"]
        if not registry.needs_classification(t)
    }


class FakeModel:
    name = "fake"

    def __init__(self, respond=lambda prompt: GOOD):
        self._respond = respond
        self._lock = threading.Lock()
        self.calls = 0

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        with self._lock:
            self.calls += 1
        return self._respond(prompt)


def _run(path, ids, model, **overrides):
    options = {
        "model": model,
        "concurrency": 3,
        "progress": lambda done, total: None,
        "cancelled": lambda: False,
        "tail_for": lambda thread: f"tail of {thread['sessionId']}",
        **overrides,
    }
    return enrichment.enrich_sessions(path, ids, **options)


def test_parse_classification_strips_whitespace():
    raw = {"status": " finished ", "whatItIs": "  shipped it \n", "suggestedName": " ship-it "}
    assert enrichment.parse_classification(raw) == Classification(
        "finished", "shipped it", "ship-it"
    )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({**GOOD, "status": "done"}, "unknown status 'done'"),
        ({**GOOD, "whatItIs": "   "}, "empty whatItIs or suggestedName"),
        ({**GOOD, "suggestedName": ""}, "empty whatItIs or suggestedName"),
        ({"status": "active"}, "empty whatItIs or suggestedName"),
    ],
)
def test_parse_classification_rejects_bad_answers(raw, message):
    with pytest.raises(ModelError, match=message):
        enrichment.parse_classification(raw)


def test_enrich_prompt_carries_metadata_and_the_whole_tail():
    tail = "x" * 9000
    prompt = enrichment.enrich_prompt(_thread("s0", lastPrompt="fix login"), tail)
    assert "last prompt: fix login" in prompt
    assert tail in prompt


def test_apply_classifications_applies_fresh_and_counts_stale():
    reg = {
        "threads": [
            _thread("fresh"),
            _thread("moved", mtime=LATER),
            _thread("done", status="finished", classifiedAtMtime=MTIME),
            _thread("kept", decision="keep"),
        ]
    }
    found = Classification("active", "doing it", "do-it")
    results = [
        Classified(("claude-code", sid), MTIME, found)
        for sid in ("fresh", "moved", "done", "vanished", "kept")
    ]

    counts = enrichment.apply_classifications(reg, results)

    assert counts == enrichment.ApplyCounts(applied=2, stale=3)
    fresh, moved, done, kept = reg["threads"]
    assert (fresh["status"], fresh["whatItIs"], fresh["suggestedName"]) == (
        "active",
        "doing it",
        "do-it",
    )
    assert fresh["classifiedAtMtime"] == MTIME
    assert moved["status"] == "unclassified"
    assert done["status"] == "finished"
    assert (kept["decision"], kept["status"]) == ("keep", "active")


def test_apply_batch_writes_under_the_registry_lock(tmp_path):
    path = _registry(tmp_path, [_thread("s0")])
    result = Classified(("claude-code", "s0"), MTIME, Classification("blocked", "w", "n"))

    assert enrichment.apply_batch(path, [result]) == enrichment.ApplyCounts(1, 0)
    assert registry.load_registry(path)["threads"][0]["status"] == "blocked"


def test_enrich_sessions_classifies_every_id_and_commits_in_batches(tmp_path):
    path = _registry(tmp_path, [_thread(f"s{i}") for i in range(25)])
    seen = []

    def progress(done, total):
        seen.append((done, total, len(_classified_ids(path))))

    outcome = _run(path, _ids(25), FakeModel(), progress=progress)

    assert outcome == enrichment.EnrichOutcome(
        classified=25, failed=0, stale=0, cancelled=False, aborted=None, errors=()
    )
    assert seen == [(10, 25, 10), (20, 25, 20), (25, 25, 25)]
    assert _classified_ids(path) == {f"s{i}" for i in range(25)}


def test_enrich_sessions_hands_batches_to_an_injected_apply(tmp_path):
    path = _registry(tmp_path, [_thread(f"s{i}") for i in range(25)])
    batches = []

    outcome = _run(path, _ids(25), FakeModel(), apply=batches.append)

    assert [len(batch) for batch in batches] == [10, 10, 5]
    assert (outcome.classified, outcome.stale) == (0, 0)
    assert {c.identity for batch in batches for c in batch} == set(_ids(25))


def test_enrich_sessions_stops_when_cancelled_and_commits_what_finished(tmp_path):
    path = _registry(tmp_path, [_thread(f"s{i}") for i in range(25)])
    cancel = threading.Event()
    model = FakeModel()

    def respond(prompt):
        if model.calls == 3:
            cancel.set()
        return GOOD

    model._respond = respond
    outcome = _run(path, _ids(25), model, concurrency=1, cancelled=cancel.is_set)

    assert (outcome.classified, outcome.cancelled, model.calls) == (3, True, 3)
    assert _classified_ids(path) == {"s0", "s1", "s2"}


def test_enrich_sessions_trips_the_breaker_on_repeated_identical_failures(tmp_path):
    path = _registry(tmp_path, [_thread(f"s{i}") for i in range(20)])

    def respond(prompt):
        raise ModelError("rate limited: try again later")

    model = FakeModel(respond)
    outcome = _run(path, _ids(20), model, concurrency=1)

    assert outcome.aborted == "5 calls failed in a row: rate limited: try again later"
    assert outcome.errors[0] == "s0: rate limited: try again later"
    assert (outcome.failed, model.calls, outcome.classified) == (5, 5, 0)


def test_enrich_sessions_breaker_ignores_varied_failures(tmp_path):
    path = _registry(tmp_path, [_thread(f"s{i}") for i in range(8)])

    def respond(prompt):
        raise ModelError(f"failure for {prompt.split('tail of ')[1]}")

    outcome = _run(path, _ids(8), FakeModel(respond))

    assert (outcome.aborted, outcome.failed) == (None, 8)


def test_enrich_sessions_counts_a_raising_tail_and_a_missing_id_as_failed(tmp_path):
    path = _registry(tmp_path, [_thread("s0"), _thread("s1")])

    def tail_for(thread):
        if thread["sessionId"] == "s1":
            raise OSError("transcript unreadable")
        return "fine"

    outcome = _run(path, [*_ids(2), ("claude-code", "gone")], FakeModel(), tail_for=tail_for)

    assert (outcome.classified, outcome.failed) == (1, 2)
    assert set(outcome.errors) == {"gone: no longer in the registry", "s1: transcript unreadable"}
    assert _classified_ids(path) == {"s0"}


def test_enrich_sessions_counts_a_session_that_changed_during_the_call_as_stale(tmp_path):
    path = _registry(tmp_path, [_thread("s0")])

    def respond(prompt):
        registry.mutate_registry(path, lambda reg: reg["threads"][0].update(mtime=LATER))
        return GOOD

    outcome = _run(path, _ids(1), FakeModel(respond))

    assert (outcome.classified, outcome.stale, outcome.failed) == (0, 1, 0)
    assert registry.load_registry(path)["threads"][0]["status"] == "unclassified"


def test_enrich_sessions_runs_model_calls_concurrently(tmp_path):
    path = _registry(tmp_path, [_thread(f"s{i}") for i in range(3)])
    barrier = threading.Barrier(3, timeout=5)

    def respond(prompt):
        barrier.wait()
        return GOOD

    outcome = _run(path, _ids(3), FakeModel(respond), concurrency=3)

    assert (outcome.classified, outcome.failed) == (3, 0)


def test_enrich_sessions_counts_an_ambiguous_session_as_stale():
    reg = {"threads": [_thread("dup"), _thread("dup")]}
    result = Classified(("claude-code", "dup"), MTIME, Classification("active", "w", "n"))

    assert enrichment.apply_classifications(reg, [result]) == enrichment.ApplyCounts(0, 1)


def test_enrich_sessions_tail_failures_do_not_trip_the_breaker(tmp_path):
    path = _registry(tmp_path, [_thread(f"s{i}") for i in range(8)])

    def tail_for(thread):
        raise OSError("no readable conversation")

    outcome = _run(path, _ids(8), FakeModel(), concurrency=1, tail_for=tail_for)

    assert (outcome.aborted, outcome.failed) == (None, 8)


def test_enrich_sessions_stops_the_workers_and_salvages_the_batch_when_progress_raises(tmp_path):
    path = _registry(tmp_path, [_thread(f"s{i}") for i in range(60)])

    def respond(prompt):
        time.sleep(0.05)
        return GOOD

    model = FakeModel(respond)
    batches = []

    def progress(done, total):
        raise RuntimeError("ui gone")

    with pytest.raises(RuntimeError, match="ui gone"):
        _run(path, _ids(60), model, apply=batches.append, progress=progress, concurrency=2)
    calls = model.calls
    time.sleep(0.3)

    assert model.calls == calls
    assert calls < 60
    assert len(batches) == 1


def test_default_concurrency_and_estimate():
    class Llm(FakeModel):
        name = "llm"

    assert enrichment.default_concurrency(Llm()) == 1
    assert enrichment.default_concurrency(FakeModel()) == 4
    assert enrichment.estimate_seconds(9, "claude", 4) == 3 * 8.0
    assert enrichment.estimate_seconds(2, None, 0) == 2 * 15.0


def test_enrich_sessions_releases_the_loaded_registry_before_calling_the_model(
    tmp_path, monkeypatch
):
    path = _registry(tmp_path, [_thread("s0")])
    load = registry.load_registry
    loaded = []

    def tracked(target):
        reg = load(target)
        loaded.append(weakref.ref(reg))
        return reg

    monkeypatch.setattr(registry, "load_registry", tracked)
    released = []

    def respond(prompt):
        gc.collect()
        released.append(loaded[0]() is None)
        return GOOD

    _run(path, _ids(1), FakeModel(respond), concurrency=1)

    assert released == [True]
