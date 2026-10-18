import fcntl
import os
import threading
import time

import pytest
from ringleader import registry, stateio
from ringleader import roundup as roundup_api
from ringleader.revalidate import Seen
from ringleader.sources.base import stat_fingerprint

NOW = "2026-06-30T00:00:00+00:00"
OLD = "2026-06-01T00:00:00+00:00"
RECENT = "2026-06-28T00:00:00+00:00"
GOOD = {"status": "unfinished", "whatItIs": "work in progress", "suggestedName": "wip"}


@pytest.fixture(autouse=True)
def _claude_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(tmp_path / "projects"))


class FakeModel:
    name = "claude"
    model = "haiku"

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = 0

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        with self._lock:
            self.calls += 1
        return GOOD


def _thread(  # noqa: PLR0913 -- test data builder, one arg per field
    tmp_path,
    sid,
    *,
    mtime=OLD,
    msgs=50,
    finish=False,
    age_seconds=600.0,
    status="unclassified",
):
    transcript = tmp_path / "projects" / f"{sid}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text('{"type":"user","message":{"content":"hi"}}\n')
    when = time.time() - age_seconds
    os.utime(transcript, (when, when))
    thread = {
        "sessionId": sid,
        "source": "claude-code",
        "file": str(transcript),
        "project": "/w/x",
        "title": f"{sid}-title",
        "mtime": mtime,
        "messageCount": msgs,
        "status": status,
        "decision": None,
        "fileFingerprint": stat_fingerprint(transcript.stat()),
    }
    if status != "unclassified":
        thread["classifiedAtMtime"] = mtime
    if finish:
        thread["finishOnExit"] = True
    return thread


def _archived(tmp_path, sid, archived_at="2026-05-01T00:00:00+00:00"):
    path = tmp_path / "archive" / "repo" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n")
    return {
        "sessionId": sid,
        "source": "claude-code",
        "archivedAt": archived_at,
        "reason": "archived",
        "archivePath": str(path),
        "summary": "s",
        "project": "repo",
    }


def _write(tmp_path, threads, archived=()):
    reg = registry.empty_registry()
    reg["threads"] = list(threads)
    reg["archived"] = list(archived)
    path = tmp_path / "registry.json"
    registry.save_registry(path, reg)
    return str(path)


def _add(path, thread):
    registry.mutate_registry(path, lambda reg: reg["threads"].append(thread))


def _plan(path, model=None, *, retention_days=30, concurrency=None):
    return roundup_api.plan_roundup(
        path,
        registry.load_registry(path),
        NOW,
        scan_note="(scanning… 3 new)",
        model=model,
        now_epoch=time.time(),
        retention_days=retention_days,
        concurrency=concurrency,
    )


def _phases(plan, *, presence=frozenset, unknown="allow", **overrides):
    options = {
        "presence": presence,
        "unknown": unknown,
        "progress": lambda event: None,
        "cancelled": lambda: False,
        "archive_root_path": str(plan_root(plan)),
        "now_epoch": time.time,
        **overrides,
    }
    return roundup_api.run_phases(plan, **options)


def plan_root(plan):
    return os.path.join(os.path.dirname(plan.registry_path), "archive")


def _sids(identities):
    return [sid for _, sid in identities]


def _live_sids(path):
    return sorted(t["sessionId"] for t in registry.load_registry(path)["threads"])


def _world(tmp_path):
    return _write(
        tmp_path,
        [
            _thread(tmp_path, "fin", msgs=3, finish=True),
            _thread(tmp_path, "tiny", msgs=3),
            _thread(tmp_path, "big-old"),
            _thread(tmp_path, "big-new", mtime=RECENT),
            _thread(tmp_path, "done", status="active"),
        ],
        archived=[_archived(tmp_path, "expired")],
    )


def test_plan_names_each_phase_set(tmp_path):
    plan = _plan(_world(tmp_path), FakeModel())

    assert [item.identity[1] for item in plan.sweep_items] == ["fin"]
    assert [item.identity[1] for item in plan.prune_items] == ["tiny"]
    assert _sids(plan.purge_ids) == ["expired"]
    assert _sids(plan.enrich_all_ids) == ["big-new", "big-old"]
    assert _sids(plan.enrich_recent_ids) == ["big-new"]
    assert (plan.engine, plan.model_label, plan.concurrency) == ("claude", "claude (haiku)", 4)


def test_plan_without_a_model_still_names_enrich_ids(tmp_path):
    plan = _plan(_world(tmp_path))

    assert _sids(plan.enrich_all_ids) == ["big-new", "big-old"]
    assert (plan.engine, plan.model_label, plan.concurrency) == (None, None, 1)


def test_enrich_warning_flips_above_the_threshold_with_a_model(tmp_path):
    threads = [_thread(tmp_path, f"s{i}") for i in range(26)]
    over = _write(tmp_path, threads)
    assert (_plan(over, FakeModel()).enrich_warning, _plan(over).enrich_warning) == (True, False)
    at = _write(tmp_path, threads[:25])
    assert _plan(at, FakeModel()).enrich_warning is False


def test_estimated_seconds_divides_calls_across_concurrency(tmp_path):
    path = _write(tmp_path, [_thread(tmp_path, f"s{i}") for i in range(8)])
    four = _plan(path, FakeModel(), concurrency=4).estimated_seconds("all")
    two = _plan(path, FakeModel(), concurrency=2).estimated_seconds("all")
    assert (four, two) == (16.0, 32.0)


def test_describe_plan_names_counts_model_and_durations(tmp_path):
    lines = roundup_api.describe_plan(_plan(_world(tmp_path), FakeModel()))
    assert lines == [
        "scanned — 3 new",
        "archive 1 finished session (sweep)",
        "delete 1 tiny session older than 7 days (prune)",
        "purge 1 archived session past retention",
        "enrich 2 sessions with claude (haiku) — about 1 min",
    ]


def test_describe_plan_says_enrich_is_skipped_without_a_model(tmp_path):
    lines = roundup_api.describe_plan(_plan(_world(tmp_path)))
    assert lines[-1] == "enrich: no model configured — skipped (rl config)"


def test_describe_plan_names_the_model_a_read_only_run_would_use(tmp_path):
    path = _world(tmp_path)
    plan = roundup_api.plan_roundup(
        path,
        registry.load_registry(path),
        NOW,
        scan_note=None,
        model=None,
        now_epoch=time.time(),
        retention_days=30,
        read_only_model=FakeModel(),
    )
    assert roundup_api.describe_plan(plan)[-1] == (
        "enrich: read-only — skipped (would use claude (haiku))"
    )


def test_run_phases_sweeps_prunes_and_purges_the_plan(tmp_path):
    path = _world(tmp_path)
    plan = _plan(path)

    result = _phases(plan)

    assert (result.swept, result.deleted, result.purged, result.errors) == (1, 1, 1, 0)
    saved = registry.load_registry(path)
    assert [a["sessionId"] for a in saved["archived"]] == ["fin"]
    assert sorted(d["sessionId"] for d in saved["deleted"]) == ["expired", "tiny"]


def test_run_phases_deletes_only_planned_prune_ids(tmp_path):
    path = _write(tmp_path, [_thread(tmp_path, "tiny", msgs=3)])
    plan = _plan(path)
    late = _thread(tmp_path, "late", msgs=3)
    _add(path, late)

    result = _phases(plan)

    assert result.deleted == 1
    assert _live_sids(path) == ["late"]
    assert os.path.exists(late["file"])


def test_run_phases_skips_a_prune_candidate_whose_transcript_grew(tmp_path):
    thread = _thread(tmp_path, "tiny", msgs=3)
    path = _write(tmp_path, [thread])
    plan = _plan(path)
    with open(thread["file"], "a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant"}\n')

    result = _phases(plan)

    assert result.deleted == 0
    assert [(item.identity[1], reason) for item, reason in result.skipped] == [
        ("tiny", "session changed since selection")
    ]
    assert os.path.exists(thread["file"])


def test_run_phases_skips_a_sweep_session_written_within_the_grace(tmp_path):
    thread = _thread(tmp_path, "fin", finish=True)
    path = _write(tmp_path, [thread])
    plan = _plan(path)
    os.utime(thread["file"], None)

    result = _phases(plan)

    assert [(item.identity[1], reason) for item, reason in result.skipped] == [
        ("fin", "still being written")
    ]
    assert os.path.exists(thread["file"])


def test_run_phases_skips_a_running_session(tmp_path):
    thread = _thread(tmp_path, "tiny", msgs=3)
    path = _write(tmp_path, [thread])

    result = _phases(_plan(path), presence=lambda: frozenset({("claude", "tiny")}))

    assert [reason for _, reason in result.skipped] == [
        "tiny-title is running — finish it from inside with the ringleader-finish skill"
    ]
    assert _live_sids(path) == ["tiny"]


def test_run_phases_with_unknown_presence_and_refuse_deletes_nothing(tmp_path):
    path = _world(tmp_path)

    result = _phases(_plan(path), presence=lambda: None, unknown="refuse")

    assert (result.swept, result.deleted) == (0, 0)
    assert _live_sids(path) == ["big-new", "big-old", "done", "fin", "tiny"]


def test_run_phases_sweeps_a_batch_with_one_presence_probe_and_one_registry_write(
    tmp_path, monkeypatch
):
    path = _write(
        tmp_path,
        [_thread(tmp_path, f"fin-{i}", finish=True) for i in range(3)],
    )
    plan = _plan(path)
    probes = []
    write = registry.atomic_write_text
    writes = []

    def counted(target, text, **options):
        writes.append(target)
        write(target, text, **options)

    monkeypatch.setattr(registry, "atomic_write_text", counted)

    result = _phases(plan, presence=lambda: probes.append(1) or frozenset())

    assert (result.swept, len(probes), len(writes)) == (3, 1, 1)
    assert sorted(a["sessionId"] for a in registry.load_registry(path)["archived"]) == [
        "fin-0",
        "fin-1",
        "fin-2",
    ]


def test_run_phases_reports_a_sweep_batch_whose_registry_write_failed(tmp_path, monkeypatch):
    path = _write(
        tmp_path,
        [_thread(tmp_path, "fin-a", finish=True), _thread(tmp_path, "fin-b", finish=True)],
    )
    plan = _plan(path)

    def failing(target, text, **options):
        raise OSError("disk full")

    monkeypatch.setattr(registry, "atomic_write_text", failing)

    result = _phases(plan)

    assert (result.swept, result.errors) == (0, 2)
    assert result.warnings == (
        "sweep fin-a-title: archived on disk, but the registry was not updated: disk full",
        "sweep fin-b-title: archived on disk, but the registry was not updated: disk full",
    )


def test_run_phases_refuses_while_another_roundup_holds_the_lock(tmp_path):
    plan = _plan(_world(tmp_path))
    with stateio.roundup_lock() as held:
        assert held
        with pytest.raises(roundup_api.RoundupBusy, match="another roundup is running"):
            _phases(plan)


def test_run_phases_reports_progress_in_phase_order(tmp_path):
    events = []
    _phases(_plan(_world(tmp_path)), progress=events.append)
    phases = [event.phase for event in events]
    assert list(dict.fromkeys(phases)) == ["sweep", "prune", "purge"]
    assert events[-1] == roundup_api.RoundupProgress("purge", 1, 1, "done")


def test_run_phases_stops_pruning_between_batches_when_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(roundup_api, "PRUNE_BATCH", 1)
    path = _write(tmp_path, [_thread(tmp_path, f"t{i}", msgs=3) for i in range(3)])
    stop = threading.Event()

    def progress(event):
        if event.phase == "prune" and event.done == 1:
            stop.set()

    result = _phases(_plan(path), progress=progress, cancelled=stop.is_set)

    assert (result.deleted, result.cancelled) == (1, True)
    assert len(_live_sids(path)) == 2


def _roundup(plan, scope, model, **overrides):
    options = {
        "model": model,
        "presence": frozenset,
        "unknown": "allow",
        "progress": lambda event: None,
        "cancelled": lambda: False,
        "archive_root_path": plan_root(plan),
        **overrides,
    }
    return roundup_api.run_roundup(plan, scope, **options)


def test_run_roundup_enriches_the_scope_and_stamps(tmp_path):
    path = _world(tmp_path)
    model = FakeModel()

    result = _roundup(_plan(path, model), "recent", model)

    assert (result.enrich.classified, result.stamped, model.calls) == (1, True, 1)
    saved = registry.load_registry(path)
    assert saved["lastRoundupAt"] == NOW
    assert registry.find_session(saved, "big-new")["status"] == "unfinished"


def test_run_roundup_cancelled_before_enrich_leaves_it_unstamped(tmp_path):
    path = _world(tmp_path)
    model = FakeModel()
    stop = threading.Event()

    def progress(event):
        if event.phase == "purge":
            stop.set()

    result = _roundup(_plan(path, model), "all", model, progress=progress, cancelled=stop.is_set)

    assert (result.enrich, result.stamped, model.calls) == (None, False, 0)
    assert registry.load_registry(path)["lastRoundupAt"] is None


def test_run_roundup_with_scope_none_stamps_without_enriching(tmp_path):
    path = _world(tmp_path)
    model = FakeModel()

    result = _roundup(_plan(path, model), "none", model)

    assert (result.enrich, result.stamped, model.calls) == (None, True, 0)
    assert registry.load_registry(path)["lastRoundupAt"] == NOW


def test_is_stale_compares_the_last_roundup_with_the_window():
    assert [
        roundup_api.is_stale({"lastRoundupAt": stamp}, NOW, 8)
        for stamp in (None, "2026-06-29T23:00:00+00:00", "2026-06-29T15:00:00+00:00")
    ] == [True, False, True]


def test_prune_planned_reports_changed_since_the_plan(tmp_path):
    thread = _thread(tmp_path, "tiny", msgs=3)
    path = _write(tmp_path, [thread])
    item = Seen(("claude-code", "tiny"), "tiny-title", "2026-05-01T00:00:00+00:00", None)

    deleted, skipped = roundup_api.prune_planned(
        path,
        [item],
        NOW,
        presence=frozenset,
        unknown="allow",
        cancelled=lambda: False,
        progress=lambda done, total: None,
        report=lambda message: None,
    )

    assert (deleted, skipped) == (0, ((item, "changed since the plan"),))


def test_run_phases_does_not_purge_an_archive_renewed_after_planning(tmp_path):
    entry = _archived(tmp_path, "renewed")
    path = _write(tmp_path, [], archived=[entry])
    plan = _plan(path)
    registry.mutate_registry(
        path, lambda reg: reg["archived"][0].update(archivedAt="2026-06-29T23:00:00+00:00")
    )

    result = _phases(plan)

    assert result.purged == 0
    assert os.path.exists(entry["archivePath"])


def test_run_phases_survives_an_archive_timestamp_that_became_unparseable(tmp_path):
    entry = _archived(tmp_path, "garbled")
    path = _write(tmp_path, [], archived=[entry])
    plan = _plan(path)
    registry.mutate_registry(path, lambda reg: reg["archived"][0].update(archivedAt="garbage"))

    result = _phases(plan)

    assert (result.purged, result.errors) == (0, 0)
    assert os.path.exists(entry["archivePath"])


def test_run_roundup_holds_the_lock_through_enrich(tmp_path):
    path = _world(tmp_path)
    model = FakeModel()
    plan = _plan(path, model)
    seen_busy = []

    def progress(event):
        if event.phase == "enrich":
            with stateio.roundup_lock() as held:
                seen_busy.append(not held)

    _roundup(plan, "all", model, progress=progress)

    assert seen_busy
    assert all(seen_busy)


def test_plan_roundup_leaves_out_sessions_of_unavailable_sources(tmp_path):
    path = _write(tmp_path, [_thread(tmp_path, "pending")])

    plan = roundup_api.plan_roundup(
        path,
        registry.load_registry(path),
        NOW,
        scan_note=None,
        model=FakeModel(),
        now_epoch=time.time(),
        retention_days=0,
        unavailable_sources=frozenset({"claude-code"}),
    )

    assert plan.enrich_all_ids == ()


def test_prune_planned_attributes_skips_to_the_item_not_the_log_text(tmp_path):
    thread = _thread(tmp_path, "tiny", msgs=3)
    path = _write(tmp_path, [thread])
    plan = _plan(path)

    deleted, skipped = roundup_api.prune_planned(
        path,
        plan.prune_items,
        NOW,
        presence=lambda: frozenset({("claude", "tiny")}),
        unknown="allow",
        cancelled=lambda: False,
        progress=lambda done, total: None,
        report=pytest.fail,
    )

    assert (deleted, [item.identity for item, _ in skipped]) == (0, [plan.prune_items[0].identity])


def test_interrupt_mid_batch_still_tombstones_the_unlinked_sessions(tmp_path, monkeypatch):
    from ringleader import prune

    threads = [_thread(tmp_path, f"t{i}", msgs=3) for i in range(3)]
    path = _write(tmp_path, threads)
    plan = _plan(path)
    real = prune.registry.prune_session
    calls = []

    def interrupting(reg, qualified, now_iso, **options):
        real(reg, qualified, now_iso, **options)
        calls.append(qualified)
        if len(calls) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(prune.registry, "prune_session", interrupting)

    with pytest.raises(KeyboardInterrupt):
        _phases(plan)

    saved = registry.load_registry(path)
    assert len(saved["deleted"]) == 2


def _handoff(directory, name, age_days):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("# handoff\n")
    when = time.time() - age_days * 86_400
    os.utime(path, (when, when))
    return path


def test_roundup_plans_and_purges_handoff_exports_past_retention(tmp_path):
    handoffs = tmp_path / "handoffs"
    old = _handoff(handoffs, "old.md", 40)
    fresh = _handoff(handoffs, "fresh.md", 1)
    path = _write(tmp_path, [])
    plan = roundup_api.plan_roundup(
        path,
        registry.load_registry(path),
        NOW,
        scan_note=None,
        model=None,
        now_epoch=time.time(),
        retention_days=30,
        handoffs_dir=handoffs,
    )

    assert plan.handoff_paths == (str(old),)
    assert roundup_api.describe_plan(plan)[3] == (
        "purge 0 archived sessions past retention, and 1 handoff export"
    )
    result = _phases(plan)
    assert result.handoffs_purged == 1
    assert sorted(p.name for p in handoffs.iterdir()) == [fresh.name]


def test_roundup_keeps_handoff_exports_when_retention_is_off(tmp_path):
    handoffs = tmp_path / "handoffs"
    old = _handoff(handoffs, "old.md", 400)
    path = _write(tmp_path, [])
    plan = roundup_api.plan_roundup(
        path,
        registry.load_registry(path),
        NOW,
        scan_note=None,
        model=None,
        now_epoch=time.time(),
        retention_days=0,
        handoffs_dir=handoffs,
    )

    assert (plan.handoff_paths, _phases(plan).handoffs_purged) == ((), 0)
    assert old.exists()


def _registry_locked(path):
    with open(f"{path}.lock", "a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def test_a_sweep_batch_skips_a_running_middle_session_and_archives_the_others(tmp_path):
    path = _write(tmp_path, [_thread(tmp_path, f"fin-{i}", finish=True) for i in range(3)])
    plan = _plan(path)
    held = []

    def presence():
        held.append(_registry_locked(path))
        return frozenset({("claude", "fin-1")})

    result = _phases(plan, presence=presence, unknown="refuse")

    assert (result.swept, result.errors) == (2, 0)
    assert [(item.identity[1], reason) for item, reason in result.skipped] == [
        ("fin-1", "fin-1-title is running — finish it from inside with the ringleader-finish skill")
    ]
    assert sorted(a["sessionId"] for a in registry.load_registry(path)["archived"]) == [
        "fin-0",
        "fin-2",
    ]
    assert held == [True]


def test_an_interrupted_sweep_batch_records_what_it_already_archived(tmp_path, monkeypatch):
    path = _write(tmp_path, [_thread(tmp_path, f"fin-{i}", finish=True) for i in range(3)])
    plan = _plan(path)
    archive = roundup_api.archive_cmd.archive_thread

    def interrupted(reg, thread, **options):
        if thread["sessionId"] == "fin-1":
            raise KeyboardInterrupt
        return archive(reg, thread, **options)

    monkeypatch.setattr(roundup_api.archive_cmd, "archive_thread", interrupted)

    with pytest.raises(KeyboardInterrupt):
        _phases(plan)

    saved = registry.load_registry(path)
    assert [a["sessionId"] for a in saved["archived"]] == ["fin-0"]
    assert sorted(t["sessionId"] for t in saved["threads"]) == ["fin-1", "fin-2"]
    assert not os.path.exists(tmp_path / "projects" / "fin-0.jsonl")
