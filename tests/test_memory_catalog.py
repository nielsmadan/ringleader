import json
from dataclasses import replace
from pathlib import Path

import pytest
from ringleader import memory_catalog, memory_scan, registry
from ringleader.commands import mem_scan
from ringleader.memory_sources.claude_code import ClaudeMemorySource, ClaudeRoots
from ringleader.memory_types import MemoryScan
from ringleader.projects import memory_project_rows

T1 = "2026-11-17T12:00:00+00:00"
T2 = "2026-11-17T12:01:00+00:00"
T3 = "2026-11-17T12:02:00+00:00"


def setup_native(root):
    source = ClaudeMemorySource(ClaudeRoots(root / "claude", root / "projects", root))
    note = root / "projects" / "repo" / "memory" / "topic.md"
    note.parent.mkdir(parents=True)
    note.write_text("private raw content")
    return source, note


def commit(path, reg, scan, started):
    out = registry.reconcile_native_memory(reg, scan, started)
    registry.commit_registry(path, out)
    return out


def test_legacy_migration_concurrent_legacy_edit_and_status_snapshot(tmp_path):
    source, note = setup_native(tmp_path)
    path = tmp_path / "registry.json"
    reg = registry.empty_registry()
    reg.update(
        lastScan="session-clock",
        memoryEntries=[{"path": str(note), "mtime": T1, "futureField": 41}],
    )
    path.write_text(json.dumps(reg))
    scan = source.scan()
    base = registry.load_registry(path)
    old_writer = registry.Registry(reg, path=path, baseline=reg.copy())
    old_writer["memoryEntries"] = [{**reg["memoryEntries"][0], "reviewedAtMtime": T1}]
    committed = commit(path, base, scan, T2)
    registry.commit_registry(path, old_writer)
    (row,) = old_writer["memoryEntries"]
    assert row["reviewedAtMtime"] == T1 and row["futureField"] == 41
    assert memory_catalog.select_entry(old_writer, str(note)) == memory_catalog.select_entry(
        old_writer, row["memoryId"][:8]
    )
    assert memory_catalog.bind_record(
        source.read_selected(scan.records[0].ref), row
    ).ref == memory_catalog.reviewed_ref(row)
    key = memory_catalog.store_key("claude-code", str(note.parent))
    assert (
        committed["memoryScannedAt"][key],
        committed["lastScan"],
        committed["memoryStores"][key]["state"],
        committed["memorySources"]["claude-code"]["status"],
    ) == (T2, "session-clock", "ready", "ready")
    assert "private raw content" not in path.read_text()


def test_absent_store_clears_rows_unobserved_and_partial_stores_keep_rows(tmp_path, monkeypatch):
    source, note = setup_native(tmp_path)
    projects = source.roots.projects
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(projects))
    broken = projects / "unreadable/memory/topic.md"
    broken.parent.mkdir(parents=True)
    broken.write_text("old")
    external = tmp_path / "explicit/topic.md"
    external.parent.mkdir()
    external.write_text("old")
    cfg = Path(tmp_path / "config/settings.json")
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"autoMemoryDirectory": str(external.parent)}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg.parent))
    reg = registry.empty_registry()
    initial = memory_scan.scan(memory_scan.new_sources(reg))
    reg = registry.reconcile_native_memory(reg, initial, T1)
    note.unlink()
    note.parent.rmdir()
    note.parent.parent.rmdir()
    broken.write_bytes(b"\xff")
    cfg.write_text("{}")
    next_scan = memory_scan.scan(memory_scan.new_sources(reg))
    reg = registry.reconcile_native_memory(reg, next_scan, T2)
    assert {r["path"] for r in reg["memoryEntries"]} == {str(broken), str(external)}
    assert [r["path"] for r in memory_project_rows(reg)] == [str(broken)]
    with pytest.raises(memory_catalog.UnknownMemoryEntry):
        memory_catalog.select_entry(reg, str(external))
    store_states = {s.store_id: (s.state, s.completeness) for s in next_scan.stores}
    assert store_states[str(note.parent)] == ("absent", "complete")
    assert store_states[str(broken.parent)] == ("unreadable", "partial")


def test_stale_scan_cannot_resurrect_removed_entry_recreation_opens_new_cycle(tmp_path):
    source, note = setup_native(tmp_path)
    path = tmp_path / "registry.json"
    scan = source.scan()
    first = commit(path, registry.load_registry(path), scan, T1)
    reviewed = memory_catalog.reviewed_ref(first["memoryEntries"][0])
    pending = registry.reconcile_native_memory(registry.load_registry(path), scan, T2)

    def remove(reg):
        reg["memoryEntries"].clear()
        reg["memoryDecisions"] = {
            "op": {
                "source": reviewed.source,
                "memoryId": reviewed.memory_id,
                "sourceState": "removed",
            }
        }

    registry.mutate_registry(path, remove)
    registry.commit_registry(path, pending)
    assert pending["memoryEntries"] == []
    fresh = commit(path, registry.load_registry(path), source.scan(), T3)
    (row,) = fresh["memoryEntries"]
    assert memory_catalog.native_ref(row) == reviewed
    cycle = memory_catalog.bind_record(source.read_selected(scan.records[0].ref), row).ref
    assert cycle.incarnation != reviewed.incarnation
    again = commit(path, registry.load_registry(path), source.scan(), T3)
    assert memory_catalog.reviewed_ref(again["memoryEntries"][0]) == cycle
    replacement = note.with_suffix(".new")
    replacement.write_bytes(note.read_bytes())
    replacement.replace(note)
    with pytest.raises(ValueError, match="changed"):
        memory_catalog.bind_record(source.read_selected(source.scan().records[0].ref), row)


@pytest.mark.parametrize("later_commits_first", [True, False])
def test_per_store_scan_generation_beats_mtime_and_commit_order(tmp_path, later_commits_first):
    source, note = setup_native(tmp_path)
    path = tmp_path / "registry.json"
    old_base, new_base = (registry.load_registry(path) for _ in range(2))
    old = registry.reconcile_native_memory(old_base, source.scan(), T1)
    note.unlink()
    new = registry.reconcile_native_memory(new_base, source.scan(), T2)
    for reg in (new, old) if later_commits_first else (old, new):
        registry.commit_registry(path, reg)
    saved = registry.load_registry(path)
    assert saved["memoryEntries"] == []
    assert saved["memoryScannedAt"][memory_catalog.store_key("claude-code", str(note.parent))] == T2


def test_different_store_clocks_merge_and_unavailable_store_retains_metadata(tmp_path):
    source, _ = setup_native(tmp_path)
    scan = source.scan()
    rec = scan.records[0]
    store = next(s for s in scan.stores if s.store_id == rec.ref.store_id)
    path = tmp_path / "registry.json"
    base1, base2 = (registry.load_registry(path) for _ in range(2))
    other = replace(store, store_id="/unavailable", state="unreadable", completeness="unavailable")
    commit(path, base1, scan, T2)
    out = commit(path, base2, MemoryScan((), scan.statuses, (other,)), T1)
    assert out["memoryScannedAt"][memory_catalog.store_key("claude-code", other.store_id)] == T1
    assert memory_catalog.native_ref(out["memoryEntries"][0]) == rec.ref


def test_no_scan_triage_keeps_registry_bytes_and_does_not_read_store(tmp_path, monkeypatch):
    source, _ = setup_native(tmp_path)
    path = tmp_path / "registry.json"
    commit(path, registry.load_registry(path), source.scan(), T1)
    old = path.read_bytes()
    monkeypatch.setattr(
        memory_scan,
        "configured_sources",
        lambda *a, **k: pytest.fail("native read during cached view"),
    )
    assert mem_scan.cmd_mem_triage(["--no-scan", "--registry", str(path)]) == 0
    assert path.read_bytes() == old


@pytest.mark.parametrize("late_middle", [False, True])
def test_returning_to_observed_content_requires_new_review(tmp_path, late_middle):
    source, note = setup_native(tmp_path)
    path = tmp_path / "registry.json"
    first = commit(path, registry.load_registry(path), source.scan(), T1)
    original = memory_catalog.reviewed_ref(first["memoryEntries"][0])
    note.write_text("second version")
    middle = registry.reconcile_native_memory(registry.load_registry(path), source.scan(), T2)
    note.write_text("private raw content")
    latest = registry.reconcile_native_memory(registry.load_registry(path), source.scan(), T3)
    for snapshot in (latest, middle) if late_middle else (middle, latest):
        registry.commit_registry(path, snapshot)
    (row,) = registry.load_registry(path)["memoryEntries"]
    ref = memory_catalog.reviewed_ref(row)
    assert (ref.fingerprint, row["nativeIncarnation"]) == (
        original.fingerprint,
        original.incarnation,
    )
    assert ref.incarnation != original.incarnation
    again = commit(path, registry.load_registry(path), source.scan(), T3)
    assert memory_catalog.reviewed_ref(again["memoryEntries"][0]) == ref


def test_known_codex_root_change_hides_prior_store_and_unknown_root_preserves_it(tmp_path):
    from contextlib import closing

    from ringleader.memory_sources.codex import CodexMemorySource

    from memory_helpers import add_row, database

    roots = tmp_path / "one", tmp_path / "two"
    for root in roots:
        with closing(database(root / "memories_1.sqlite")) as db:
            add_row(db)
    path = tmp_path / "registry.json"
    first = commit(
        path, registry.load_registry(path), CodexMemorySource(roots[0], roots[0]).scan(), T1
    )
    unknown = CodexMemorySource(roots[0], None, root_error="Choose SQLite home").scan()
    retained = commit(path, first, unknown, T2)
    assert [r["storeId"] for r in memory_project_rows(retained)] == [
        str(roots[0] / "memories_1.sqlite")
    ]
    changed = commit(path, retained, CodexMemorySource(roots[1], roots[1]).scan(), T3)
    assert [r["storeId"] for r in memory_project_rows(changed)] == [
        str(roots[1] / "memories_1.sqlite")
    ]
