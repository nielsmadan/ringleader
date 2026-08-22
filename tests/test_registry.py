from ringleader.registry import (
    empty_registry,
    load_registry,
    reconcile,
    save_registry,
)
from ringleader.sources.base import NormalizedSession


def _meta(sid, mtime, project="/p", title=None, git_label=None):
    return NormalizedSession(
        source="claude-code",
        session_id=sid,
        file=f"/x/{sid}.jsonl",
        cwd=project,
        custom_title=title,
        ai_title=None,
        summary=None,
        last_prompt="lp",
        mtime=mtime,
        size_bytes=10,
        message_count=2,
        tail_text="USER: hi",
        git_project_key=git_label,
        git_project_label=git_label,
    )


NOW = "2026-06-07T12:00:00+00:00"


def test_reconcile_adds_new_threads_as_unclassified():
    reg = empty_registry()
    reg2 = reconcile(reg, [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    assert reg2["lastScan"] == NOW
    assert len(reg2["threads"]) == 1
    t = reg2["threads"][0]
    assert t["sessionId"] == "s1"
    assert t["status"] == "unclassified"
    assert t["classifiedAtMtime"] is None
    assert t["decision"] is None


def test_reconcile_refreshes_metadata_but_keeps_classification_and_decision():
    reg = empty_registry()
    reg = reconcile(reg, [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    # simulate a prior classification + user decision
    reg["threads"][0].update(
        status="unfinished",
        whatItIs="thing",
        suggestedName="thing-x",
        classifiedAtMtime="2026-06-01T00:00:00+00:00",
        decision="keep",
    )
    # same session re-scanned with a NEWER mtime + new title
    reg2 = reconcile(reg, [_meta("s1", "2026-06-05T00:00:00+00:00", title="named")], NOW)
    t = reg2["threads"][0]
    assert t["mtime"] == "2026-06-05T00:00:00+00:00"  # metadata refreshed
    assert t["title"] == "named"
    assert t["status"] == "unfinished"  # classification preserved
    assert t["decision"] == "keep"  # decision preserved
    # mtime now differs from classifiedAtMtime → will need reclassification
    assert t["classifiedAtMtime"] == "2026-06-01T00:00:00+00:00"


def test_reconcile_tombstones_vanished_sessions():
    reg = empty_registry()
    reg = reconcile(reg, [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    reg["threads"][0]["harvestPath"] = "harvests/s1.md"
    # next scan no longer includes s1
    reg2 = reconcile(reg, [], "2026-06-08T00:00:00+00:00")
    assert reg2["threads"] == []
    assert len(reg2["deleted"]) == 1
    ts = reg2["deleted"][0]
    assert ts["sessionId"] == "s1"
    assert ts["harvestPath"] == "harvests/s1.md"
    assert ts["deletedAt"] == "2026-06-08T00:00:00+00:00"


def test_reconcile_preserves_last_roundup_at():
    reg = empty_registry()
    reg["lastRoundupAt"] = "2026-06-01T00:00:00+00:00"
    reg2 = reconcile(reg, [], NOW)
    assert reg2["lastRoundupAt"] == "2026-06-01T00:00:00+00:00"


def test_reconcile_preserves_git_project_when_cwd_vanishes():
    reg = reconcile(
        empty_registry(), [_meta("s1", "2026-06-01T00:00:00+00:00", git_label="proj")], NOW
    )
    assert reg["threads"][0]["gitProjectLabel"] == "proj"
    # next scan can't resolve git (cwd gone) → git fields None, but cached value kept
    reg2 = reconcile(reg, [_meta("s1", "2026-06-05T00:00:00+00:00")], NOW)
    assert reg2["threads"][0]["gitProjectLabel"] == "proj"
    assert reg2["threads"][0]["gitProjectKey"] == "proj"


def test_load_save_roundtrip(tmp_path):
    p = tmp_path / "registry.json"
    assert load_registry(p) == empty_registry()  # missing file → empty
    reg = reconcile(empty_registry(), [_meta("s1", NOW)], NOW)
    save_registry(p, reg)
    assert load_registry(p)["threads"][0]["sessionId"] == "s1"


import pytest
from ringleader.registry import (
    apply_classification,
    needs_classification,
    sessions_needing_classification,
)


def _reg_with_one(sid="s1", mtime="2026-06-01T00:00:00+00:00"):
    return reconcile(empty_registry(), [_meta(sid, mtime)], NOW)


def test_new_thread_needs_classification():
    reg = _reg_with_one()
    assert needs_classification(reg["threads"][0]) is True
    assert [t["sessionId"] for t in sessions_needing_classification(reg)] == ["s1"]


def test_apply_classification_marks_done_until_mtime_changes():
    reg = _reg_with_one()
    apply_classification(
        reg,
        "s1",
        status="finished",
        what_it_is="did the thing",
        suggested_name="thing",
        confidence=0.9,
    )
    t = reg["threads"][0]
    assert t["status"] == "finished"
    assert t["whatItIs"] == "did the thing"
    assert t["suggestedName"] == "thing"
    assert t["statusConfidence"] == 0.9
    assert t["classifiedAtMtime"] == t["mtime"]
    assert needs_classification(t) is False
    assert sessions_needing_classification(reg) == []


def test_changed_mtime_reopens_classification():
    reg = _reg_with_one()
    apply_classification(reg, "s1", status="finished", what_it_is="x", suggested_name="x")
    # session touched again → newer mtime
    reg = reconcile(reg, [_meta("s1", "2026-06-09T00:00:00+00:00")], NOW)
    assert needs_classification(reg["threads"][0]) is True


def test_apply_unknown_session_raises():
    reg = _reg_with_one()
    with pytest.raises(KeyError):
        apply_classification(reg, "nope", status="finished", what_it_is="x", suggested_name="x")


def test_reconcile_does_not_mutate_input_registry():
    """Fix 1: reconcile must copy-on-write — the input registry's thread dicts must be unchanged."""
    reg = empty_registry()
    reg = reconcile(reg, [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    original_thread = reg["threads"][0]
    original_mtime = original_thread["mtime"]

    # run reconcile again with a newer mtime
    reg2 = reconcile(reg, [_meta("s1", "2026-06-05T00:00:00+00:00")], NOW)

    # the returned registry should have the updated mtime
    assert reg2["threads"][0]["mtime"] == "2026-06-05T00:00:00+00:00"
    # the original dict must be untouched
    assert original_thread["mtime"] == original_mtime


def test_apply_classification_does_not_overwrite_user_fields():
    """Fix 5 guard: apply_classification must not touch decision, harvestPath, or tags."""
    reg = _reg_with_one()
    reg["threads"][0]["decision"] = "keep"
    reg["threads"][0]["harvestPath"] = "harvests/s1.md"
    reg["threads"][0]["tags"] = ["important"]

    apply_classification(
        reg,
        "s1",
        status="finished",
        what_it_is="did the thing",
        suggested_name="thing",
        confidence=0.8,
    )

    t = reg["threads"][0]
    assert t["decision"] == "keep"
    assert t["harvestPath"] == "harvests/s1.md"
    assert t["tags"] == ["important"]


from ringleader.registry import apply_name


def test_new_thread_has_null_name_provenance():
    reg = reconcile(empty_registry(), [_meta("s1", NOW)], NOW)
    t = reg["threads"][0]
    assert t["appliedName"] is None
    assert t["namedAtMtime"] is None


def test_apply_name_sets_title_and_provenance():
    reg = reconcile(empty_registry(), [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    apply_name(reg, "s1", "login-button")
    t = reg["threads"][0]
    assert t["title"] == "login-button"
    assert t["appliedName"] == "login-button"
    assert t["namedAtMtime"] == t["mtime"]


def test_apply_name_unknown_session_raises():
    reg = reconcile(empty_registry(), [_meta("s1", NOW)], NOW)
    with pytest.raises(KeyError):
        apply_name(reg, "nope", "x")


def test_reconcile_preserves_name_provenance():
    reg = reconcile(empty_registry(), [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    apply_name(reg, "s1", "thing-x")
    # rescan with a newer mtime → metadata refreshes, provenance preserved
    reg2 = reconcile(reg, [_meta("s1", "2026-06-05T00:00:00+00:00")], NOW)
    t = reg2["threads"][0]
    assert t["mtime"] == "2026-06-05T00:00:00+00:00"
    assert t["appliedName"] == "thing-x"
    assert t["namedAtMtime"] == "2026-06-01T00:00:00+00:00"


from ringleader.registry import prune_session


def test_prune_session_removes_thread_and_tombstones():
    reg = reconcile(empty_registry(), [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    reg["threads"][0]["harvestPath"] = "harvests/s1.md"
    prune_session(reg, "s1", "2026-06-13T00:00:00+00:00")
    assert reg["threads"] == []
    assert len(reg["deleted"]) == 1
    ts = reg["deleted"][0]
    assert ts["sessionId"] == "s1"
    assert ts["deletedAt"] == "2026-06-13T00:00:00+00:00"
    assert ts["reason"] == "prune"
    assert ts["harvestPath"] == "harvests/s1.md"


def test_prune_session_is_idempotent():
    reg = reconcile(empty_registry(), [_meta("s1", NOW), _meta("s2", NOW)], NOW)
    prune_session(reg, "s1", NOW)
    prune_session(reg, "s1", NOW)  # second call: already gone + tombstoned
    assert [t["sessionId"] for t in reg["threads"]] == ["s2"]  # s2 untouched
    assert sum(1 for ts in reg["deleted"] if ts["sessionId"] == "s1") == 1


from ringleader.registry import archive_session, mark_harvested, mark_reviewed


def test_new_thread_has_null_reviewed_marker():
    reg = reconcile(empty_registry(), [_meta("s1", NOW)], NOW)
    assert reg["threads"][0]["reviewedAtMtime"] is None


def test_reconcile_preserves_reviewed_marker():
    reg = reconcile(empty_registry(), [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    reg["threads"][0]["reviewedAtMtime"] = "2026-06-01T00:00:00+00:00"
    reg2 = reconcile(reg, [_meta("s1", "2026-06-05T00:00:00+00:00")], NOW)
    assert reg2["threads"][0]["reviewedAtMtime"] == "2026-06-01T00:00:00+00:00"


def test_mark_harvested_sets_decision_and_paths():
    reg = reconcile(empty_registry(), [_meta("s1", NOW)], NOW)
    mark_harvested(reg, "s1", ["docs/tech/auth.md"])
    t = reg["threads"][0]
    assert t["decision"] == "harvested"
    assert t["harvestPath"] == ["docs/tech/auth.md"]
    mark_harvested(reg, "s1", [])  # nothing found → None
    assert reg["threads"][0]["harvestPath"] is None


def test_mark_reviewed_records_current_mtime():
    reg = reconcile(empty_registry(), [_meta("s1", "2026-06-05T00:00:00+00:00")], NOW)
    mark_reviewed(reg, "s1")
    assert reg["threads"][0]["reviewedAtMtime"] == "2026-06-05T00:00:00+00:00"


def test_archive_session_removes_thread_and_tombstones():
    reg = reconcile(empty_registry(), [_meta("s1", NOW)], NOW)
    reg["threads"][0]["harvestPath"] = ["docs/x.md"]
    archive_session(reg, "s1", "archive/enc/s1.jsonl", "2026-06-23T00:00:00+00:00")
    assert reg["threads"] == []
    ts = reg["archived"][0]
    assert ts["sessionId"] == "s1"
    assert ts["archivePath"] == "archive/enc/s1.jsonl"
    assert ts["harvestPath"] == ["docs/x.md"]
    assert ts["archivedAt"] == "2026-06-23T00:00:00+00:00"


def test_mark_helpers_raise_for_unknown_id():
    reg = reconcile(empty_registry(), [_meta("s1", NOW)], NOW)
    for fn in (lambda: mark_harvested(reg, "nope", []), lambda: mark_reviewed(reg, "nope")):
        with pytest.raises(KeyError):
            fn()


def test_archive_session_stores_summary_and_project():
    reg = reconcile(empty_registry(), [_meta("s1", NOW)], NOW)
    archive_session(
        reg,
        "s1",
        "archive/enc/s1.jsonl",
        "2026-06-24T00:00:00+00:00",
        summary="fixed the thing",
        project="splashdown",
    )
    ts = reg["archived"][0]
    assert ts["summary"] == "fixed the thing"
    assert ts["project"] == "splashdown"


def test_mark_reviewed_accepts_explicit_mtime():
    reg = reconcile(empty_registry(), [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    mark_reviewed(reg, "s1", "2026-06-20T00:00:00+00:00")  # live mtime override
    assert reg["threads"][0]["reviewedAtMtime"] == "2026-06-20T00:00:00+00:00"
    mark_reviewed(reg, "s1")  # falls back to thread mtime
    assert reg["threads"][0]["reviewedAtMtime"] == "2026-06-01T00:00:00+00:00"


from ringleader.registry import mark_project_cleaned


def test_mark_project_cleaned_sets_and_overwrites():
    reg = empty_registry()
    mark_project_cleaned(reg, "k1", "2026-06-26T00:00:00+00:00")
    assert reg["projectsCleanedAt"]["k1"] == "2026-06-26T00:00:00+00:00"
    mark_project_cleaned(reg, "k1", "2026-06-27T00:00:00+00:00")
    assert reg["projectsCleanedAt"]["k1"] == "2026-06-27T00:00:00+00:00"


def test_reconcile_preserves_projects_cleaned_at():
    reg = reconcile(empty_registry(), [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    mark_project_cleaned(reg, "k1", "2026-06-05T00:00:00+00:00")
    reg2 = reconcile(reg, [_meta("s1", "2026-06-06T00:00:00+00:00")], NOW)
    assert reg2["projectsCleanedAt"]["k1"] == "2026-06-05T00:00:00+00:00"


from ringleader.registry import relocate_thread


def test_relocate_thread_pins_location():
    reg = reconcile(
        empty_registry(), [_meta("s1", "2026-06-01T00:00:00+00:00", project="/old")], NOW
    )
    relocate_thread(
        reg, "s1", "/new/path", {"key": "k", "label": "newrepo", "root": "/new/path", "remote": "r"}
    )
    t = reg["threads"][0]
    assert t["project"] == "/new/path"
    assert t["gitProjectLabel"] == "newrepo" and t["gitProjectKey"] == "k"
    assert t["locationPinned"] is True


def test_reconcile_preserves_pinned_location():
    reg = reconcile(
        empty_registry(), [_meta("s1", "2026-06-01T00:00:00+00:00", project="/old")], NOW
    )
    relocate_thread(
        reg, "s1", "/new/path", {"key": "k", "label": "newrepo", "root": "/new/path", "remote": "r"}
    )
    # a later scan still reports the OLD cwd (baked into the transcript) + old git label
    reg2 = reconcile(
        reg, [_meta("s1", "2026-06-05T00:00:00+00:00", project="/old", git_label="oldrepo")], NOW
    )
    t = reg2["threads"][0]
    assert t["project"] == "/new/path"  # pinned — not reverted to /old
    assert t["gitProjectLabel"] == "newrepo"  # pinned git identity kept
    assert t["mtime"] == "2026-06-05T00:00:00+00:00"  # other metadata still refreshes


def test_relocate_unknown_id_raises():
    import pytest

    reg = empty_registry()
    with pytest.raises(KeyError):
        relocate_thread(reg, "nope", "/x", None)


from ringleader.registry import snooze_thread, wake_thread


def test_snooze_and_wake():
    reg = reconcile(empty_registry(), [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    assert reg["threads"][0]["snoozedAt"] is None
    snooze_thread(reg, "s1", "2026-07-01T00:00:00+00:00")
    assert reg["threads"][0]["snoozedAt"] == "2026-07-01T00:00:00+00:00"
    wake_thread(reg, "s1")
    assert reg["threads"][0]["snoozedAt"] is None


def test_reconcile_preserves_snoozed():
    reg = reconcile(empty_registry(), [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    snooze_thread(reg, "s1", "2026-07-01T00:00:00+00:00")
    reg2 = reconcile(reg, [_meta("s1", "2026-06-05T00:00:00+00:00")], NOW)
    assert reg2["threads"][0]["snoozedAt"] == "2026-07-01T00:00:00+00:00"


from ringleader import registry
from ringleader.memory import MemoryEntry


def _entry(path, cwd, mtime, body="x", signals=None):
    return MemoryEntry(
        path=path,
        encoded_dir="-enc",
        project=cwd,
        mtime=mtime,
        name="n",
        description="d",
        mem_type="feedback",
        body=body,
        git_project_key="k",
        git_project_label="proj",
        git_root=cwd,
        git_remote="r",
        obsolete_signals=signals or [],
    )


def test_reconcile_memory_upserts_and_preserves_review():
    reg = registry.empty_registry()
    reg = registry.reconcile_memory(
        reg, [_entry("/m/a.md", "/p", "2026-01-01T00:00:00+00:00")], "now"
    )
    registry.mark_memory_reviewed(reg, "/m/a.md", "2026-01-01T00:00:00+00:00")
    # re-scan, same mtime -> review survives, not needing review
    reg = registry.reconcile_memory(
        reg, [_entry("/m/a.md", "/p", "2026-01-01T00:00:00+00:00")], "now2"
    )
    assert registry.memory_needing_review(reg) == []
    # file changed -> mtime newer -> needs review again
    reg = registry.reconcile_memory(
        reg, [_entry("/m/a.md", "/p", "2026-02-01T00:00:00+00:00")], "now3"
    )
    assert [e["path"] for e in registry.memory_needing_review(reg)] == ["/m/a.md"]


def test_reconcile_memory_drops_gone_entries_no_tombstone():
    reg = registry.empty_registry()
    reg = registry.reconcile_memory(reg, [_entry("/m/a.md", "/p", "t")], "now")
    reg = registry.reconcile_memory(reg, [], "now2")  # a.md no longer scanned
    assert reg["memoryEntries"] == []
    assert reg["deleted"] == []


def test_session_reconcile_preserves_memory_keys():
    reg = registry.empty_registry()
    reg = registry.reconcile_memory(reg, [_entry("/m/a.md", "/p", "t")], "now")
    reg = registry.mark_memory_project_cleaned(reg, "k", "when")
    reg = registry.reconcile(reg, [], "now2")  # session scan must not drop memory
    assert len(reg["memoryEntries"]) == 1
    assert reg["memoryProjectsCleanedAt"] == {"k": "when"}


def _ns(**kw):
    from ringleader.sources.base import NormalizedSession

    base = {
        "source": "claude-code",
        "session_id": "s1",
        "cwd": "/p",
        "mtime": "t",
        "size_bytes": 10,
        "custom_title": None,
        "summary": None,
        "last_prompt": None,
        "file": "/p/s1.jsonl",
    }
    base.update(kw)
    return NormalizedSession(**base)


def test_reconcile_persists_source():
    from ringleader import registry

    reg = registry.empty_registry()
    reg = registry.reconcile(reg, [_ns(source="codex", session_id="c1")], "now")
    assert reg["threads"][0]["source"] == "codex"


def test_reconcile_backfills_source_on_legacy_thread():
    from ringleader import registry

    reg = registry.empty_registry()
    reg["threads"].append({"sessionId": "s1", "file": "/p/s1.jsonl"})  # legacy: no source
    reg = registry.reconcile(reg, [_ns(source="claude-code", session_id="s1")], "now")
    # matched (not duplicated / tombstoned) and stamped with source
    assert len(reg["threads"]) == 1
    assert reg["threads"][0]["source"] == "claude-code"
    assert reg["deleted"] == []


def test_reconcile_distinguishes_same_id_across_sources():
    from ringleader import registry

    reg = registry.empty_registry()
    reg = registry.reconcile(
        reg,
        [_ns(source="claude-code", session_id="x"), _ns(source="codex", session_id="x")],
        "now",
    )
    assert len(reg["threads"]) == 2
    assert {t["source"] for t in reg["threads"]} == {"claude-code", "codex"}


def test_apply_name_targets_source_when_ids_collide():
    from ringleader import registry

    reg = registry.empty_registry()
    reg = registry.reconcile(
        reg,
        [_ns(source="claude-code", session_id="x"), _ns(source="codex", session_id="x")],
        "now",
    )

    registry.apply_name(reg, "x", "codex-name", source="codex")

    by_source = {thread["source"]: thread for thread in reg["threads"]}
    assert by_source["claude-code"]["title"] is None
    assert by_source["codex"]["title"] == "codex-name"


def test_select_and_clear_for_day():
    from ringleader import registry

    reg = {"threads": [{"sessionId": "s1"}], "archived": [], "deleted": []}
    registry.select_for_day(reg, "s1", "2026-01-01")
    assert reg["threads"][0]["selectedForDay"] == "2026-01-01"
    assert [t["sessionId"] for t in registry.selected_today(reg, "2026-01-01")] == ["s1"]
    assert registry.selected_today(reg, "2026-01-02") == []
    registry.clear_selected(reg, "s1")
    assert reg["threads"][0]["selectedForDay"] is None
