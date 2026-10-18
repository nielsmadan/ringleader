import os
import time
from pathlib import Path

import pytest
from ringleader import registry
from ringleader.prune import (
    DEFAULT_MAX_TURNS,
    DELETE_BATCH,
    archive_purge_candidates,
    delete_sessions,
    handoff_purge_candidates,
    prune_candidates,
    purge_archived,
    purge_handoffs,
)
from ringleader.sources.base import stat_fingerprint

NOW = "2026-06-30T00:00:00+00:00"
OLD = "2026-06-23T00:00:00+00:00"  # exactly 7 days before NOW
RECENT = "2026-06-28T00:00:00+00:00"  # 2 days before NOW


def _t(sid, mtime=OLD, size=200_000, msgs=50, decision=None):
    return {
        "sessionId": sid,
        "mtime": mtime,
        "sizeBytes": size,
        "messageCount": msgs,
        "decision": decision,
        "status": "blocked",
        "title": None,
        "suggestedName": f"{sid}-name",
    }


def _reg(threads):
    return {"version": 1, "lastScan": NOW, "threads": threads, "archived": [], "deleted": []}


def _ids(cands):
    return sorted(c["sessionId"] for c in cands)


def test_recent_is_never_a_candidate_even_if_tiny():
    reg = _reg([_t("a", mtime=RECENT, size=10, msgs=1)])
    assert prune_candidates(reg, NOW) == []


def test_age_boundary_exactly_threshold_is_included():
    # exactly 7 days old + tiny → included (>= threshold)
    reg = _reg([_t("a", mtime=OLD, size=10, msgs=1)])
    assert _ids(prune_candidates(reg, NOW)) == ["a"]


def test_tininess_is_message_count_driven_not_size():
    # Sources vary wildly in bytes-per-message (e.g. Codex embeds full request/
    # response payloads), so size never gates eligibility — only messageCount does.
    reg = _reg(
        [
            _t("small_size_many_msgs", size=10_000, msgs=999),  # tiny bytes, too many msgs
            _t("huge_size_few_msgs", size=5_000_000, msgs=3),  # huge bytes, few msgs
        ]
    )
    assert _ids(prune_candidates(reg, NOW)) == ["huge_size_few_msgs"]


def test_message_count_threshold_is_strict():
    # Strict `<`, not `<=`: a session with exactly max_turns messages must NOT be
    # pruned, even at a large size — widening this to inclusive would make the new
    # (size-dropped) eligibility set bigger than the old OR-based one, which used a
    # strict `<` on the message-count path. That would newly delete real transcripts.
    reg = _reg(
        [
            _t("at_turns", msgs=DEFAULT_MAX_TURNS, size=5_000_000),  # == threshold → not small
            _t("under_turns", msgs=DEFAULT_MAX_TURNS - 1, size=5_000_000),  # under → small
            _t("over_turns", msgs=DEFAULT_MAX_TURNS + 1),  # over → not small
        ]
    )
    assert _ids(prune_candidates(reg, NOW)) == ["under_turns"]


def test_keep_and_harvested_protected_other_decisions_eligible():
    reg = _reg(
        [
            _t("keep", size=10, msgs=1, decision="keep"),
            _t("harvested", size=10, msgs=1, decision="harvested"),
            _t("abandon", size=10, msgs=1, decision="abandon"),
            _t("none", size=10, msgs=1, decision=None),
        ]
    )
    assert _ids(prune_candidates(reg, NOW)) == ["abandon", "none"]


def test_custom_thresholds():
    reg = _reg([_t("a", msgs=15)])  # not tiny under the default max_turns=12
    assert prune_candidates(reg, NOW) == []
    got = prune_candidates(reg, NOW, max_turns=20)
    assert _ids(got) == ["a"]


ANCIENT = "2020-01-01T00:00:00+00:00"


def _archived_ts(sid, archived_at, *, archive_path=None):
    return {
        "sessionId": sid,
        "archivedAt": archived_at,
        "archivePath": archive_path,
        "harvestPath": None,
        "summary": "s",
        "project": "repo",
    }


def _reg_ts(archived):
    return {"version": 1, "lastScan": NOW, "threads": [], "archived": archived, "deleted": []}


def test_archive_purge_disabled_when_retention_zero():
    reg = _reg_ts([_archived_ts("a", ANCIENT)])
    assert archive_purge_candidates(reg, NOW, retention_days=0) == []


def test_archive_purge_boundary_exactly_retention_included():
    reg = _reg_ts([_archived_ts("a", OLD)])  # exactly 7 days before NOW
    got = archive_purge_candidates(reg, NOW, retention_days=7)
    assert [ts["sessionId"] for ts in got] == ["a"]


def test_archive_purge_recent_excluded():
    reg = _reg_ts([_archived_ts("a", RECENT)])  # 2 days before NOW
    assert archive_purge_candidates(reg, NOW, retention_days=7) == []


def test_archive_purge_only_considers_archived_records():
    """Deleted/pruned/gc'd sessions live in a separate list and can never be purge
    candidates — the list itself is the discriminator, not a `reason` field."""
    reg = _reg_ts([_archived_ts("a", OLD)])
    reg["deleted"] = [
        {"sessionId": "pruned", "reason": "prune", "deletedAt": OLD},
        {"sessionId": "gced", "reason": "gc", "deletedAt": OLD},
    ]
    got = archive_purge_candidates(reg, NOW, retention_days=7)
    assert [ts["sessionId"] for ts in got] == ["a"]


def test_archive_purge_skips_missing_or_unparseable_date():
    reg = _reg_ts(
        [
            {"sessionId": "no-date", "archivePath": None},
            _archived_ts("bad-date", "not-a-timestamp"),
        ]
    )
    assert archive_purge_candidates(reg, NOW, retention_days=1) == []


def test_purge_archived_deletes_files_and_flips_tombstone(tmp_path):
    slug = tmp_path / "archive" / "repo"
    slug.mkdir(parents=True)
    jsonl = slug / "a.jsonl"
    jsonl.write_text("x")
    sidecar = slug / "a"
    sidecar.mkdir()
    (sidecar / "n.txt").write_text("s")
    md = slug / "a.md"
    md.write_text("summary")
    ts = _archived_ts("a", OLD, archive_path=str(jsonl))
    reg = _reg_ts([ts])

    n = purge_archived(reg, [ts], NOW, archive_root_path=str(tmp_path / "archive"))

    assert n == 1
    assert not jsonl.exists() and not sidecar.exists() and not md.exists()
    assert not slug.exists()  # empty slug dir removed
    assert ts["reason"] == "purged" and ts["purgedAt"] == NOW
    assert ts["archivedAt"] == OLD  # archive date preserved


def test_purge_archived_is_idempotent(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    jsonl = root / "a.jsonl"
    jsonl.write_text("x")
    ts = _archived_ts("a", OLD, archive_path=str(jsonl))
    reg = _reg_ts([ts])

    purge_archived(
        reg, archive_purge_candidates(reg, NOW, retention_days=7), NOW, archive_root_path=str(root)
    )

    assert archive_purge_candidates(reg, NOW, retention_days=7) == []  # reason now "purged"


def test_purge_archived_tolerates_already_gone_files(tmp_path):
    root = tmp_path / "archive"
    ts = _archived_ts("a", OLD, archive_path=str(root / "a.jsonl"))
    reg = _reg_ts([ts])
    n = purge_archived(reg, [ts], NOW, archive_root_path=str(root))
    assert n == 1 and ts["reason"] == "purged"


def test_purge_archived_flips_even_without_archive_path(tmp_path):
    ts = _archived_ts("a", OLD, archive_path=None)
    reg = _reg_ts([ts])
    n = purge_archived(reg, [ts], NOW, archive_root_path=str(tmp_path / "archive"))
    assert n == 1 and ts["reason"] == "purged"


def test_purge_archived_preserves_sibling_in_same_slug(tmp_path):
    slug = tmp_path / "archive" / "repo"
    slug.mkdir(parents=True)
    a = slug / "a.jsonl"
    a.write_text("x")
    b = slug / "b.jsonl"
    b.write_text("y")
    ts_a = _archived_ts("a", OLD, archive_path=str(a))
    ts_b = _archived_ts("b", OLD, archive_path=str(b))
    reg = _reg_ts([ts_a, ts_b])

    n = purge_archived(reg, [ts_a], NOW, archive_root_path=str(tmp_path / "archive"))

    assert n == 1
    assert not a.exists()
    assert b.exists()  # sibling under the same slug survives
    assert slug.exists()  # non-empty slug dir kept
    assert ts_a["reason"] == "purged" and "purgedAt" not in ts_b


def test_purge_archived_refuses_path_outside_archive_root(tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_text("do not delete")
    ts = _archived_ts("a", OLD, archive_path=str(outside))
    reg = _reg_ts([ts])

    n = purge_archived(reg, [ts], NOW, archive_root_path=str(tmp_path / "archive"))

    assert n == 0
    assert outside.exists()  # outside the archive root -> never touched
    assert "purgedAt" not in ts  # not purged


def test_purge_archived_refuses_non_jsonl(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    other = root / "a.txt"
    other.write_text("keep")
    ts = _archived_ts("a", OLD, archive_path=str(other))
    reg = _reg_ts([ts])

    n = purge_archived(reg, [ts], NOW, archive_root_path=str(root))

    assert n == 0 and other.exists() and "purgedAt" not in ts


def _droid_slug(tmp_path):
    return tmp_path / "_factory" / ".factory" / "sessions" / "-w-api"


def _droid_thread(tmp_path, sid="d"):
    slug = _droid_slug(tmp_path)
    slug.mkdir(parents=True)
    transcript = slug / f"{sid}.jsonl"
    transcript.write_text("{}\n")
    (slug / f"{sid}.settings.json").write_text("{}")
    (slug / f"{sid}.settings.json.bak").write_text("{}")
    return {
        "sessionId": sid,
        "source": "droid",
        "file": str(transcript),
        "fileFingerprint": stat_fingerprint(transcript.stat()),
        "mtime": OLD,
    }


def test_deleting_a_droid_session_removes_its_settings_sidecars(tmp_path):
    thread = _droid_thread(tmp_path)
    reg = _reg([thread])

    assert delete_sessions(reg, [thread], NOW) == 1
    assert list(_droid_slug(tmp_path).iterdir()) == []


def test_purging_an_archived_droid_session_removes_its_settings_sidecars(tmp_path):
    slug = tmp_path / "archive" / "repo"
    slug.mkdir(parents=True)
    jsonl = slug / "d.jsonl"
    jsonl.write_text("x")
    (slug / "d.settings.json").write_text("{}")
    (slug / "d.settings.json.bak").write_text("{}")
    ts = _archived_ts("d", OLD, archive_path=str(jsonl))
    ts["source"] = "droid"
    reg = _reg_ts([ts])

    assert purge_archived(reg, [ts], NOW, archive_root_path=str(tmp_path / "archive")) == 1
    assert not slug.exists()


def test_delete_sessions_routes_skip_messages_to_report(tmp_path):
    thread = _droid_thread(tmp_path)
    reg = _reg([thread])
    messages = []

    deleted = delete_sessions(
        reg, [thread], NOW, report=messages.append, guard=lambda t, live: "busy"
    )

    assert (deleted, messages) == (0, ["skipped d: busy"])


def test_delete_sessions_refuses_a_transcript_changed_since_the_scan(tmp_path):
    thread = _droid_thread(tmp_path)
    reg = _reg([thread])
    with open(thread["file"], "a", encoding="utf-8") as handle:
        handle.write("{}\n")
    messages = []

    assert delete_sessions(reg, [thread], NOW, report=messages.append) == 0
    assert messages == ["skipped d: session changed since selection"]
    assert Path(thread["file"]).exists()


def test_delete_sessions_batches_and_reports_progress(tmp_path):
    threads = [{**_t(sid), "source": "opencode", "file": None} for sid in ("a", "b", "c")]
    reg = _reg(threads)
    events = []

    delete_sessions(
        reg, threads, NOW, report=lambda m: None, progress=lambda *e: events.append(e), batch_size=2
    )

    assert events == [(2, 3), (3, 3)]


def _file(path, *, age_days=0):
    path.write_text(path.stem)
    when = time.time() - age_days * 86_400
    os.utime(path, (when, when))
    return path


def _handoff_dir(tmp_path):
    d = tmp_path / "handoffs"
    d.mkdir()
    old = _file(d / "a-1.md", age_days=40)
    _file(d / "b-2.md")
    _file(d / "notes.txt", age_days=40)
    return d, old


def test_handoff_candidates_are_markdown_exports_older_than_retention(tmp_path):
    d, old = _handoff_dir(tmp_path)
    now = time.time()
    assert handoff_purge_candidates(d, now, retention_days=30) == [old]
    assert handoff_purge_candidates(d, now, retention_days=0) == []
    assert handoff_purge_candidates(tmp_path / "missing", now, retention_days=30) == []


def test_purging_handoffs_rechecks_age_and_stays_inside_the_directory(tmp_path):
    d, _ = _handoff_dir(tmp_path)
    outside = _file(tmp_path / "elsewhere.md", age_days=40)
    rewritten = _file(d / "c-3.md", age_days=40)
    candidates = [*handoff_purge_candidates(d, time.time(), retention_days=30), outside]
    _file(rewritten)

    purged = purge_handoffs(d, candidates, time.time(), retention_days=30)

    assert purged == 1
    assert sorted(p.name for p in d.iterdir()) == ["b-2.md", "c-3.md", "notes.txt"]
    assert outside.exists()


def test_a_changed_session_mid_batch_is_skipped_and_the_rest_are_deleted(tmp_path):
    slug = _droid_slug(tmp_path)
    slug.mkdir(parents=True)
    threads = []
    for sid in ("a", "b", "c"):
        transcript = slug / f"{sid}.jsonl"
        transcript.write_text("{}\n")
        threads.append(
            {
                "sessionId": sid,
                "source": "droid",
                "file": str(transcript),
                "fileFingerprint": stat_fingerprint(transcript.stat()),
                "mtime": OLD,
            }
        )
    path = tmp_path / "registry.json"
    registry.save_registry(path, _reg(threads))
    with open(threads[1]["file"], "a", encoding="utf-8") as handle:
        handle.write("{}\n")
    reg = registry.load_registry(path)
    messages = []

    deleted = delete_sessions(
        reg, list(reg["threads"]), NOW, report=messages.append, batch_size=DELETE_BATCH
    )

    saved = registry.load_registry(path)
    assert (deleted, messages) == (2, ["skipped b: session changed since selection"])
    assert [t["sessionId"] for t in saved["threads"]] == ["b"]
    assert sorted(d["sessionId"] for d in saved["deleted"]) == ["a", "c"]
    assert sorted(p.name for p in slug.iterdir()) == ["b.jsonl"]


def test_delete_sessions_rejects_a_batch_size_below_one():
    with pytest.raises(ValueError, match="batch_size must be at least 1, not 0"):
        delete_sessions(_reg([]), [], NOW, batch_size=0)


def test_an_aged_symlinked_handoff_is_neither_a_candidate_nor_purged(tmp_path):
    d = tmp_path / "handoffs"
    d.mkdir()
    target = _file(tmp_path / "target.md", age_days=40)
    link = d / "linked.md"
    link.symlink_to(target)
    when = time.time() - 40 * 86_400
    os.utime(link, (when, when), follow_symlinks=False)

    assert handoff_purge_candidates(d, time.time(), retention_days=30) == []
    assert purge_handoffs(d, [link], time.time(), retention_days=30) == 0
    assert (link.is_symlink(), target.read_text()) == (True, "target")


def test_purging_handoffs_with_retention_off_deletes_nothing(tmp_path):
    d, old = _handoff_dir(tmp_path)

    assert purge_handoffs(d, [old], time.time(), retention_days=0) == 0
    assert old.exists()
