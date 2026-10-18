import fcntl
import json
import os
import time
from pathlib import Path

import pytest
from ringleader import batch, decisions, harvests, overview, registry, revalidate, stateio
from ringleader.decisions import (
    ArchiveOp,
    ClassifyOp,
    DeleteOp,
    HarvestSaveOp,
    KeepGoingOp,
    KeepOp,
    OpContext,
    PruneOp,
    RoundupPhasesOp,
    SnoozeOp,
    StampRoundupOp,
)
from ringleader.enrichment import Classification, Classified
from ringleader.roundup import RoundupPlan
from ringleader.sources.base import stat_fingerprint

NOW = "2026-06-30T00:00:00+00:00"
MTIME = "2026-06-01T00:00:00+00:00"
UNTIL = "2026-07-07T00:00:00+00:00"
MARKDOWN = "# fix\n\n## Summary\n\nfixed the parser\n"


def _claude_dir(tmp_path):
    return Path(os.environ["SESSION_TRACKER_PROJECTS_DIR"])


def _thread(tmp_path, sid="s1", **fields):
    transcript = _claude_dir(tmp_path) / "-w-x" / f"{sid}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text('{"type":"user","message":{"content":"fix the parser"}}\n')
    when = time.time() - 600
    os.utime(transcript, (when, when))
    return {
        "sessionId": sid,
        "source": "claude-code",
        "file": str(transcript),
        "project": "/w/x",
        "title": f"{sid}-title",
        "mtime": MTIME,
        "messageCount": 3,
        "status": "unfinished",
        "decision": None,
        "fileFingerprint": stat_fingerprint(transcript.stat()),
        **fields,
    }


def _write(tmp_path, threads):
    reg = registry.empty_registry()
    reg["threads"] = list(threads)
    path = tmp_path / "registry.json"
    registry.save_registry(path, reg)
    return str(path)


def _load(path):
    return json.loads(Path(path).read_text())


def _ctx(path, tmp_path, *, live=frozenset(), unknown="refuse", cancelled=lambda: False):
    return OpContext(
        registry_path=path,
        archive_root_path=str(tmp_path / "archive"),
        now_iso=lambda: NOW,
        now_epoch=time.time,
        presence=lambda: live,
        unknown=unknown,
        cancelled=cancelled,
        progress=lambda update: None,
    )


def _seen(thread):
    return revalidate.seen(thread)


def _append(thread):
    with open(thread["file"], "a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant"}\n')


def test_archive_moves_the_transcript_and_records_the_tombstone(tmp_path):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread])
    outcome = decisions.execute(ArchiveOp(_seen(thread)), _ctx(path, tmp_path))
    dest = tmp_path / "archive" / "x" / "s1.jsonl"
    assert (outcome.ok, outcome.wrote, outcome.message, outcome.path) == (
        True,
        True,
        "archived s1-title",
        str(dest),
    )
    assert dest.exists()
    assert [a["sessionId"] for a in _load(path)["archived"]] == ["s1"]


def test_archive_refuses_a_transcript_that_gained_a_line(tmp_path):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread])
    _append(thread)
    outcome = decisions.execute(ArchiveOp(_seen(thread)), _ctx(path, tmp_path))
    assert (outcome.ok, outcome.wrote, outcome.message) == (
        False,
        False,
        "s1-title: changed since you looked",
    )
    assert outcome.refused == ((_seen(thread), "changed since you looked"),)
    assert Path(thread["file"]).exists()
    assert [t["sessionId"] for t in _load(path)["threads"]] == ["s1"]


def test_archive_refuses_a_running_session(tmp_path):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread])
    ctx = _ctx(path, tmp_path, live=frozenset({("claude", "s1")}))
    outcome = decisions.execute(ArchiveOp(_seen(thread)), ctx)
    assert outcome.message == (
        "s1-title is running — finish it from inside with the ringleader-finish skill"
    )
    assert Path(thread["file"]).exists()


def test_archive_refuses_when_presence_is_unknown(tmp_path):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread])
    outcome = decisions.execute(ArchiveOp(_seen(thread)), _ctx(path, tmp_path, live=None))
    assert outcome.message == (
        "can't tell whether s1-title is running — install presence with rl data presence install"
    )
    assert Path(thread["file"]).exists()


def test_archive_refuses_an_opencode_session(tmp_path):
    thread = {
        "sessionId": "o1",
        "source": "opencode",
        "title": "oc",
        "mtime": MTIME,
        "fileFingerprint": "opencode:abc",
    }
    path = _write(tmp_path, [thread])
    outcome = decisions.execute(ArchiveOp(_seen(thread)), _ctx(path, tmp_path))
    assert (outcome.ok, outcome.wrote) == (False, False)
    assert "no filesystem transcript lifecycle" in outcome.message
    assert [t["sessionId"] for t in _load(path)["threads"]] == ["o1"]


def test_delete_unlinks_the_transcript_and_its_companions(tmp_path):
    thread = _thread(tmp_path)
    companion = Path(thread["file"]).with_suffix("")
    companion.mkdir()
    (companion / "subagent.jsonl").write_text("{}\n")
    path = _write(tmp_path, [thread])
    outcome = decisions.execute(DeleteOp(_seen(thread)), _ctx(path, tmp_path))
    assert (outcome.ok, outcome.wrote, outcome.message) == (
        True,
        True,
        "deleted s1-title — nothing harvested",
    )
    assert (Path(thread["file"]).exists(), companion.exists()) == (False, False)
    assert [(d["sessionId"], d["reason"]) for d in _load(path)["deleted"]] == [("s1", "deleted")]


def test_delete_refuses_a_changed_transcript(tmp_path):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread])
    _append(thread)
    outcome = decisions.execute(DeleteOp(_seen(thread)), _ctx(path, tmp_path))
    assert (outcome.ok, outcome.message) == (False, "s1-title: changed since you looked")
    assert Path(thread["file"]).exists()
    assert [t["sessionId"] for t in _load(path)["threads"]] == ["s1"]


def test_delete_refuses_a_running_session(tmp_path):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread])
    ctx = _ctx(path, tmp_path, live=frozenset({("claude", "s1")}))
    outcome = decisions.execute(DeleteOp(_seen(thread)), ctx)
    assert (outcome.ok, outcome.message) == (
        False,
        "s1-title is running — finish it from inside with the ringleader-finish skill",
    )
    assert Path(thread["file"]).exists()
    assert [t["sessionId"] for t in _load(path)["threads"]] == ["s1"]


def test_delete_refuses_when_presence_is_unknown(tmp_path):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread])
    outcome = decisions.execute(DeleteOp(_seen(thread)), _ctx(path, tmp_path, live=None))
    assert (outcome.ok, outcome.message) == (
        False,
        "can't tell whether s1-title is running — install presence with rl data presence install",
    )
    assert Path(thread["file"]).exists()
    assert [t["sessionId"] for t in _load(path)["threads"]] == ["s1"]


def test_delete_refuses_a_session_without_a_fingerprint(tmp_path):
    thread = _thread(tmp_path, fileFingerprint=None)
    path = _write(tmp_path, [thread])
    outcome = decisions.execute(DeleteOp(_seen(thread)), _ctx(path, tmp_path))
    assert (outcome.ok, outcome.message) == (
        False,
        "s1-title: changed since you looked: no fingerprint, rescan",
    )
    assert Path(thread["file"]).exists()
    assert [t["sessionId"] for t in _load(path)["threads"]] == ["s1"]


def _stray_thread(tmp_path):
    notes = tmp_path / "work" / "notes.jsonl"
    notes.parent.mkdir()
    notes.write_text("{}\n")
    (tmp_path / "work" / "notes").mkdir()
    (tmp_path / "work" / "notes" / "keep.md").write_text("mine\n")
    return _thread(tmp_path, file=str(notes), fileFingerprint=stat_fingerprint(notes.stat())), notes


def test_delete_refuses_a_transcript_outside_the_session_store(tmp_path):
    thread, notes = _stray_thread(tmp_path)
    path = _write(tmp_path, [thread])
    outcome = decisions.execute(DeleteOp(_seen(thread)), _ctx(path, tmp_path))
    reason = (
        f"cannot delete claude-code session s1: {notes} is outside the claude-code session store"
    )
    assert (outcome.ok, outcome.message) == (False, f"s1-title: {reason}")
    assert (notes.exists(), (notes.parent / "notes" / "keep.md").exists()) == (True, True)


def test_prune_skips_a_transcript_outside_the_session_store(tmp_path):
    thread, notes = _stray_thread(tmp_path)
    path = _write(tmp_path, [thread])
    outcome = decisions.execute(PruneOp((_seen(thread),)), _ctx(path, tmp_path))
    assert outcome.message == "deleted 0 tiny sessions"
    assert [reason for _, reason in outcome.refused] == [
        f"cannot prune claude-code session s1: {notes} is outside the claude-code session store"
    ]
    assert (notes.exists(), (notes.parent / "notes" / "keep.md").exists()) == (True, True)


def test_prune_skips_a_session_without_a_fingerprint(tmp_path):
    thread = _thread(tmp_path, fileFingerprint=None)
    path = _write(tmp_path, [thread])
    outcome = decisions.execute(PruneOp((_seen(thread),)), _ctx(path, tmp_path))
    assert outcome.message == "deleted 0 tiny sessions"
    assert [reason for _, reason in outcome.refused] == [
        "changed since you looked: no fingerprint, rescan"
    ]
    assert Path(thread["file"]).exists()


def test_keep_going_marks_reviewed_at_the_seen_mtime(tmp_path):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread, _thread(tmp_path, "s2")])
    outcome = decisions.execute(KeepGoingOp(_seen(thread)), _ctx(path, tmp_path))
    threads = _load(path)["threads"]
    assert (outcome.ok, outcome.message) == (True, "kept going: s1-title")
    assert threads[0]["reviewedAtMtime"] == MTIME
    assert [t["sessionId"] for t in overview.triage_inbox(threads, NOW)] == ["s2"]


def test_snooze_records_when_and_until(tmp_path):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread])
    outcome = decisions.execute(SnoozeOp(_seen(thread), UNTIL), _ctx(path, tmp_path))
    [stored] = _load(path)["threads"]
    assert outcome.message == "snoozed s1-title until 2026-07-07"
    assert (stored["snoozedAt"], stored["snoozedUntil"]) == (NOW, UNTIL)


def test_keep_marks_the_session_kept(tmp_path):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread])
    outcome = decisions.execute(KeepOp(_seen(thread)), _ctx(path, tmp_path))
    [stored] = _load(path)["threads"]
    assert outcome.message == "kept long-term: s1-title"
    assert (stored["decision"], stored["keptAt"]) == ("keep", NOW)


def test_a_vanished_session_is_not_ok(tmp_path):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [])
    ctx = _ctx(path, tmp_path)
    outcomes = [
        decisions.execute(op, ctx)
        for op in (ArchiveOp(_seen(thread)), DeleteOp(_seen(thread)), KeepOp(_seen(thread)))
    ]
    assert [(o.ok, o.wrote, o.message) for o in outcomes] == [
        (False, False, "s1-title: no longer in the registry"),
        (False, False, "s1-title: no longer in the registry"),
        (False, False, "s1-title: no longer in the registry"),
    ]


def test_harvest_save_writes_the_harvest_and_archives(tmp_path):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread])
    outcome = decisions.execute(HarvestSaveOp(_seen(thread), MARKDOWN), _ctx(path, tmp_path))
    [stored] = harvests.load_harvests()
    [archived] = _load(path)["archived"]
    assert (outcome.ok, outcome.wrote) == (True, True)
    assert outcome.message == f"harvested s1-title → {stored.path}"
    assert "fixed the parser" in stored.content
    assert (archived["sessionId"], archived["thread"]["decision"]) == ("s1", "harvested")
    assert (tmp_path / "archive" / "x" / "s1.jsonl").exists()


def test_an_already_harvested_session_is_only_archived(tmp_path):
    thread = _thread(tmp_path, decision="harvested")
    path = _write(tmp_path, [thread])
    outcome = decisions.execute(HarvestSaveOp(_seen(thread), None), _ctx(path, tmp_path))
    assert (outcome.ok, outcome.message) == (True, "archived s1-title")
    assert [a["sessionId"] for a in _load(path)["archived"]] == ["s1"]


def test_an_archive_collision_after_the_save_reports_harvested_not_archived(tmp_path):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread])
    blocker = tmp_path / "archive" / "x" / "s1.jsonl"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("older copy\n")
    outcome = decisions.execute(HarvestSaveOp(_seen(thread), MARKDOWN), _ctx(path, tmp_path))
    [stored] = _load(path)["threads"]
    assert (outcome.ok, outcome.wrote) == (False, True)
    assert outcome.message.startswith("harvested, not archived: ")
    assert stored["decision"] == "harvested"
    assert Path(thread["file"]).exists()


def test_harvest_save_refuses_a_running_session_before_saving(tmp_path):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread])
    ctx = _ctx(path, tmp_path, live=frozenset({("claude", "s1")}))
    outcome = decisions.execute(HarvestSaveOp(_seen(thread), MARKDOWN), ctx)
    [stored] = _load(path)["threads"]
    assert (outcome.ok, outcome.wrote) == (False, False)
    assert stored["decision"] is None


class FakeModel:
    name = "claude"

    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        self.prompts.append(prompt)
        return self.answer


def test_harvest_markdown_is_none_for_an_empty_answer(tmp_path):
    empty = {"summary": "  ", "gotchas": [], "decisions": [""], "whatChanged": []}
    assert decisions.harvest_markdown(_thread(tmp_path), FakeModel(empty)) is None


def test_harvest_markdown_renders_a_real_answer(tmp_path):
    answer = {"summary": "fixed it", "gotchas": ["watch the cache"], "decisions": []}
    model = FakeModel(answer)
    markdown = decisions.harvest_markdown(_thread(tmp_path), model)
    assert markdown is not None
    assert "fixed it" in markdown
    assert "- watch the cache" in markdown
    assert "fix the parser" in model.prompts[0]


def test_prune_deletes_planned_tiny_sessions(tmp_path):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread])
    outcome = decisions.execute(PruneOp((_seen(thread),)), _ctx(path, tmp_path))
    assert (outcome.ok, outcome.wrote, outcome.message) == (True, True, "deleted 1 tiny session")
    assert [d["sessionId"] for d in _load(path)["deleted"]] == ["s1"]


def test_roundup_phases_refuse_while_another_roundup_runs(tmp_path):
    path = _write(tmp_path, [])
    plan = RoundupPlan(path, NOW, None, (), (), (), (), (), None, None, 1)
    with stateio.roundup_lock() as acquired:
        outcome = decisions.execute(RoundupPhasesOp(plan), _ctx(path, tmp_path))
    assert acquired
    assert (outcome.ok, outcome.message) == (False, "roundup: another roundup is running")


def test_classify_and_stamp_write_the_registry(tmp_path):
    thread = _thread(tmp_path, status="unclassified")
    path = _write(tmp_path, [thread])
    ctx = _ctx(path, tmp_path)
    result = Classified(("claude-code", "s1"), MTIME, Classification("finished", "done", "wip"))
    classified = decisions.execute(ClassifyOp((result,)), ctx)
    stamped = decisions.execute(StampRoundupOp(NOW), ctx)
    stored = _load(path)
    assert [(o.ok, o.wrote) for o in (classified, stamped)] == [(True, True), (True, True)]
    assert (stored["threads"][0]["status"], stored["lastRoundupAt"]) == ("finished", NOW)


def test_identities_cover_each_op_kind(tmp_path):
    first, second = _seen(_thread(tmp_path)), _seen(_thread(tmp_path, "s2"))
    plan = RoundupPlan("r", NOW, None, (first,), (second,), (), (), (), None, None, 1)
    assert [
        decisions.identities(op)
        for op in (DeleteOp(first), PruneOp((first, second)), RoundupPhasesOp(plan))
    ] == [
        (first.identity,),
        (first.identity, second.identity),
        (first.identity, second.identity),
    ]
    assert decisions.is_delete(DeleteOp(first))


def _conflicting_commit(monkeypatch):
    def commit(path, reg):
        raise registry.RegistryConflict("changed concurrently")

    monkeypatch.setattr(registry, "commit_registry", commit)


def test_an_archive_whose_registry_write_fails_reports_the_move(tmp_path, monkeypatch):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread])

    def failing_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(registry, "_write_registry", failing_write)
    outcome = decisions.execute(ArchiveOp(_seen(thread)), _ctx(path, tmp_path))
    assert (outcome.ok, outcome.wrote) == (False, True)
    assert outcome.message == (
        "archived s1-title on disk, but the registry was not updated: disk full"
    )
    assert outcome.path == str(tmp_path / "archive" / "x" / "s1.jsonl")
    assert (tmp_path / "archive" / "x" / "s1.jsonl").exists()


def test_archiving_a_harvested_session_whose_commit_conflicts_reports_the_move(
    tmp_path, monkeypatch
):
    thread = _thread(tmp_path, decision="harvested")
    path = _write(tmp_path, [thread])
    _conflicting_commit(monkeypatch)
    outcome = decisions.execute(HarvestSaveOp(_seen(thread), None), _ctx(path, tmp_path))
    assert (outcome.ok, outcome.wrote) == (False, True)
    assert outcome.message.startswith("archived s1-title on disk, but the registry")


def test_a_harvest_whose_save_conflicts_reports_the_saved_file(tmp_path, monkeypatch):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread])
    _conflicting_commit(monkeypatch)
    outcome = decisions.execute(HarvestSaveOp(_seen(thread), MARKDOWN), _ctx(path, tmp_path))
    assert (outcome.ok, outcome.wrote) == (False, True)
    assert (
        outcome.message == "harvest saved, but the registry was not updated: changed concurrently"
    )


def test_a_delete_whose_registry_write_fails_reports_the_unlink(tmp_path, monkeypatch):
    thread = _thread(tmp_path)
    path = _write(tmp_path, [thread])

    def failing_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(registry, "_write_registry", failing_write)
    outcome = decisions.execute(DeleteOp(_seen(thread)), _ctx(path, tmp_path))
    assert (outcome.ok, outcome.wrote) == (False, True)
    assert (
        outcome.message == "deleted s1-title on disk, but the registry was not updated: disk full"
    )
    assert not Path(thread["file"]).exists()


def test_a_programming_error_in_a_bulk_op_is_not_mapped_to_a_refusal(tmp_path, monkeypatch):
    path = _write(tmp_path, [])

    def broken(*args, **kwargs):
        raise KeyError("bug")

    monkeypatch.setattr(decisions.roundup, "prune_planned", broken)
    with pytest.raises(KeyError):
        decisions.execute(PruneOp(()), _ctx(path, tmp_path))


def _counting_writes(monkeypatch):
    write = registry.atomic_write_text
    writes = []

    def counted(target, text, **options):
        writes.append(target)
        write(target, text, **options)

    monkeypatch.setattr(registry, "atomic_write_text", counted)
    return writes


def _counting_presence(ctx):
    probes = []

    def probe():
        probes.append(1)
        return frozenset()

    return OpContext(**{**ctx.__dict__, "presence": probe}), probes


def test_a_mark_group_applies_each_op_in_one_registry_write(tmp_path, monkeypatch):
    threads = [_thread(tmp_path, sid) for sid in ("s1", "s2", "s3")]
    path = _write(tmp_path, threads[:2])
    writes = _counting_writes(monkeypatch)
    ops = [
        KeepOp(_seen(threads[0])),
        SnoozeOp(_seen(threads[1]), UNTIL),
        KeepGoingOp(_seen(threads[2])),
    ]

    results = decisions.execute_group(ops, _ctx(path, tmp_path))

    assert [(r.ok, r.message) for r in results] == [
        (True, "kept long-term: s1-title"),
        (True, "snoozed s2-title until 2026-07-07"),
        (False, "s3-title: no longer in the registry"),
    ]
    assert len(writes) == 1
    saved = {t["sessionId"]: t for t in _load(path)["threads"]}
    assert (saved["s1"]["decision"], saved["s2"]["snoozedUntil"]) == ("keep", UNTIL)


def test_a_delete_group_probes_presence_once_and_rechecks_each_session(tmp_path, monkeypatch):
    threads = [_thread(tmp_path, sid) for sid in ("s1", "s2", "s3")]
    path = _write(tmp_path, threads)
    ops = [DeleteOp(_seen(thread)) for thread in threads]
    _append(threads[1])
    writes = _counting_writes(monkeypatch)
    ctx, probes = _counting_presence(_ctx(path, tmp_path))

    results = decisions.execute_group(ops, ctx)

    assert [r.message for r in results] == [
        "deleted s1-title — nothing harvested",
        "s2-title: changed since you looked",
        "deleted s3-title — nothing harvested",
    ]
    assert (len(probes), len(writes)) == (1, 1)
    assert [t["sessionId"] for t in _load(path)["threads"]] == ["s2"]
    assert sorted(d["reason"] for d in _load(path)["deleted"]) == ["deleted", "deleted"]
    assert Path(threads[1]["file"]).exists()


def test_a_delete_group_whose_registry_write_fails_reports_each_unlink(tmp_path, monkeypatch):
    threads = [_thread(tmp_path, sid) for sid in ("s1", "s2")]
    path = _write(tmp_path, threads)

    def failing_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(registry, "_write_registry", failing_write)
    results = decisions.execute_group([DeleteOp(_seen(t)) for t in threads], _ctx(path, tmp_path))

    assert [(r.ok, r.wrote, r.message) for r in results] == [
        (False, True, "deleted s1-title on disk, but the registry was not updated: disk full"),
        (False, True, "deleted s2-title on disk, but the registry was not updated: disk full"),
    ]


def test_an_archive_group_probes_presence_once_and_writes_once(tmp_path, monkeypatch):
    threads = [_thread(tmp_path, sid) for sid in ("s1", "s2")]
    path = _write(tmp_path, threads)
    writes = _counting_writes(monkeypatch)
    ctx, probes = _counting_presence(_ctx(path, tmp_path))

    results = decisions.execute_group([ArchiveOp(_seen(t)) for t in threads], ctx)

    assert [(r.ok, r.message) for r in results] == [
        (True, "archived s1-title"),
        (True, "archived s2-title"),
    ]
    assert (len(probes), len(writes)) == (1, 1)
    assert sorted(a["sessionId"] for a in _load(path)["archived"]) == ["s1", "s2"]


def test_an_archive_group_refuses_a_running_session_and_archives_the_rest(tmp_path):
    threads = [_thread(tmp_path, sid) for sid in ("s1", "s2")]
    path = _write(tmp_path, threads)
    ctx = _ctx(path, tmp_path, live=frozenset({("claude", "s1")}))

    results = decisions.execute_group([ArchiveOp(_seen(t)) for t in threads], ctx)

    assert [r.ok for r in results] == [False, True]
    assert results[0].refused[0][1].startswith("s1-title is running")
    assert [t["sessionId"] for t in _load(path)["threads"]] == ["s1"]


def test_an_archive_group_whose_registry_write_fails_reports_each_move(tmp_path, monkeypatch):
    threads = [_thread(tmp_path, sid) for sid in ("s1", "s2")]
    path = _write(tmp_path, threads)

    def failing_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(registry, "_write_registry", failing_write)
    results = decisions.execute_group([ArchiveOp(_seen(t)) for t in threads], _ctx(path, tmp_path))

    assert [(r.wrote, r.message) for r in results] == [
        (True, "archived s1-title on disk, but the registry was not updated: disk full"),
        (True, "archived s2-title on disk, but the registry was not updated: disk full"),
    ]
    assert results[0].path == str(tmp_path / "archive" / "x" / "s1.jsonl")


def test_a_programming_error_in_a_group_is_returned_for_that_op_only(tmp_path, monkeypatch):
    threads = [_thread(tmp_path, sid) for sid in ("s1", "s2")]
    path = _write(tmp_path, threads)
    keep = registry.keep_thread

    def broken(reg, session_id, when):
        if session_id.endswith("s1"):
            raise ZeroDivisionError("bug")
        return keep(reg, session_id, when)

    monkeypatch.setattr(registry, "keep_thread", broken)
    first, second = decisions.execute_group(
        [KeepOp(_seen(t)) for t in threads], _ctx(path, tmp_path)
    )

    assert isinstance(first, ZeroDivisionError)
    assert second.message == "kept long-term: s2-title"


def test_group_keys_split_marks_archives_and_deletes(tmp_path):
    seen = _seen(_thread(tmp_path))
    assert [
        decisions.group_key(op)
        for op in (
            KeepOp(seen),
            SnoozeOp(seen, UNTIL),
            KeepGoingOp(seen),
            ArchiveOp(seen),
            DeleteOp(seen),
            HarvestSaveOp(seen, None),
            PruneOp(()),
        )
    ] == ["mark", "mark", "mark", "archive", "delete", None, None]


def _registry_locked(path):
    with open(f"{path}.lock", "a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def test_an_interrupted_delete_group_records_the_deletes_it_already_made(tmp_path, monkeypatch):
    threads = [_thread(tmp_path, sid) for sid in ("s1", "s2", "s3")]
    path = _write(tmp_path, threads)
    prune_session = registry.prune_session

    def interrupted(reg, session_id, when, **options):
        if session_id.endswith("s2"):
            raise KeyboardInterrupt
        return prune_session(reg, session_id, when, **options)

    monkeypatch.setattr(registry, "prune_session", interrupted)

    with pytest.raises(KeyboardInterrupt):
        decisions.execute_group([DeleteOp(_seen(t)) for t in threads], _ctx(path, tmp_path))

    saved = _load(path)
    assert [d["sessionId"] for d in saved["deleted"]] == ["s1"]
    assert [t["sessionId"] for t in saved["threads"]] == ["s2", "s3"]
    assert Path(threads[2]["file"]).exists()


def test_an_interrupted_archive_group_records_the_moves_it_already_made(tmp_path, monkeypatch):
    threads = [_thread(tmp_path, sid) for sid in ("s1", "s2")]
    path = _write(tmp_path, threads)
    archive = decisions.archive_cmd.archive_thread

    def interrupted(reg, thread, **options):
        if thread["sessionId"] == "s2":
            raise KeyboardInterrupt
        return archive(reg, thread, **options)

    monkeypatch.setattr(decisions.archive_cmd, "archive_thread", interrupted)

    with pytest.raises(KeyboardInterrupt):
        decisions.execute_group([ArchiveOp(_seen(t)) for t in threads], _ctx(path, tmp_path))

    assert [a["sessionId"] for a in _load(path)["archived"]] == ["s1"]


def test_a_group_probes_presence_under_the_registry_lock(tmp_path):
    threads = [_thread(tmp_path, sid) for sid in ("s1", "s2")]
    path = _write(tmp_path, threads)
    held = []

    def presence():
        held.append(_registry_locked(path))
        return frozenset()

    ctx = OpContext(**{**_ctx(path, tmp_path).__dict__, "presence": presence})
    decisions.execute_group([ArchiveOp(_seen(t)) for t in threads], ctx)

    assert held == [True]


def test_a_group_reprobes_presence_once_the_reading_is_too_old(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "PRESENCE_MAX_AGE", -1.0)
    threads = [_thread(tmp_path, sid) for sid in ("s1", "s2")]
    path = _write(tmp_path, threads)
    readings = iter([frozenset(), frozenset({("claude", "s2")})])
    ctx = OpContext(**{**_ctx(path, tmp_path).__dict__, "presence": lambda: next(readings)})

    first, second = decisions.execute_group([ArchiveOp(_seen(t)) for t in threads], ctx)

    assert (first.ok, second.ok) == (True, False)
    assert second.message.startswith("s2-title is running")
