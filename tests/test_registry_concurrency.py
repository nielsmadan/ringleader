from __future__ import annotations

import json
import select
import subprocess
import sys
import threading
from copy import deepcopy

import pytest
from ringleader import lifecycle, refresh, registry
from ringleader.cli import main
from ringleader.commands import archive, enrich, keep, mark_mem_reviewed, mem_scan
from ringleader.config import Config
from ringleader.memory import MemoryEntry
from ringleader.sources.base import NormalizedSession, stat_fingerprint

NOW = "2026-09-21T12:00:00+00:00"
LATER = "2026-09-21T13:00:00+00:00"


@pytest.fixture(autouse=True)
def _stores_at_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(tmp_path))


def seed(tmp_path, *, collision=False):
    reg = registry.empty_registry()
    sources = ("claude-code", "codex") if collision else ("claude-code",)
    for source in sources:
        file = tmp_path / f"{source}.jsonl"
        file.write_text("{}\n")
        reg["threads"].append(
            {
                "sessionId": "same",
                "source": source,
                "file": str(file),
                "fileFingerprint": stat_fingerprint(file.stat()),
                "mtime": NOW,
                "project": str(tmp_path),
                "status": "unclassified",
                "decision": None,
                "lineage": {"parent": "origin"},
                "harvestPath": None,
            }
        )
    reg["futureVersion"] = {"operations": ["pending"]}
    reg["memoryEntries"] = [{"path": str(tmp_path / "note.md"), "mtime": NOW, "future": True}]
    path = tmp_path / "registry.json"
    registry.save_registry(path, reg)
    return path


def scanned(tmp_path, **values):
    fields = {
        "source": "claude-code",
        "session_id": "same",
        "cwd": str(tmp_path),
        "mtime": LATER,
        "size_bytes": 2,
        "custom_title": "scanned",
        "summary": None,
        "last_prompt": None,
        "file": str(tmp_path / "claude-code.jsonl"),
    }
    fields.update(values)
    return NormalizedSession(**fields)


def test_processes_commit_independent_changes_from_same_snapshot(tmp_path):
    path = seed(tmp_path)
    script = """
import sys
from ringleader import registry
reg = registry.load_registry(sys.argv[1])
print("ready", flush=True)
input()
reg["projectsCleanedAt"][sys.argv[2]] = sys.argv[3]
registry.commit_registry(sys.argv[1], reg)
"""
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(path), str(n), NOW],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(4)
    ]
    try:
        for worker in workers:
            assert select.select([worker.stdout], [], [], 10)[0]
            assert worker.stdout.readline().strip() == "ready"
        for worker in workers:
            worker.stdin.write("go\n")
            worker.stdin.flush()
        for worker in workers:
            output, errors = worker.communicate(timeout=10)
            assert worker.returncode == 0, (output, errors)
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.communicate()
    assert registry.load_registry(path)["projectsCleanedAt"] == {str(n): NOW for n in range(4)}


def test_refresh_preserves_interleaved_keep_memory_new_session_and_relocation(
    tmp_path, monkeypatch
):
    path = seed(tmp_path)

    def scan(**_):
        assert keep.cmd_keep(["same", "--yes", "--registry", str(path)]) == 0
        assert (
            mark_mem_reviewed.cmd_mark_mem_reviewed(
                [str(tmp_path / "note.md"), "--registry", str(path)]
            )
            == 0
        )
        current = registry.load_registry(path)
        registry.pin_thread_location(current["threads"][0], "/relocated")
        current["threads"][0]["file"] = "/relocated/session.jsonl"
        current["threads"].append(
            {"sessionId": "new", "source": "pi", "lineage": {"parent": "same"}}
        )
        registry.commit_registry(path, current)
        return [scanned(tmp_path, cwd="/stale-scan", file="/stale-scan/session.jsonl")]

    monkeypatch.setattr(refresh.scanner, "scan", scan)
    result, _ = refresh.auto_refresh(path, LATER, sources=[], name_filters=[])
    thread = registry.find_session(result, "same")
    assert thread["decision"] == "keep"
    assert thread["project"] == "/relocated"
    assert thread["file"] == "/relocated/session.jsonl"
    assert thread["lineage"] == {"parent": "origin"}
    assert registry.find_session(result, "pi:new")["lineage"] == {"parent": "same"}
    assert result["memoryEntries"][0]["reviewedAtMtime"] == NOW
    assert result["futureVersion"] == {"operations": ["pending"]}


def test_enrich_preserves_keep_during_model_call(tmp_path, monkeypatch):
    path = seed(tmp_path)

    class Model:
        name = "fixture"

        def complete_json(self, *_):
            keep.cmd_keep(["same", "--yes", "--registry", str(path)])
            return {"status": "unfinished", "whatItIs": "work", "suggestedName": "task"}

    monkeypatch.setattr(enrich, "active_model", lambda _cfg, _task: Model())
    monkeypatch.setattr(enrich, "load_config", Config)
    monkeypatch.setattr(refresh.scanner, "active_sources", lambda **_: [])
    monkeypatch.setattr(refresh.scanner, "scan", lambda **_: [scanned(tmp_path)])
    assert enrich.cmd_enrich(["--registry", str(path)]) == 0
    thread = registry.load_registry(path)["threads"][0]
    assert (thread["decision"], thread["status"], thread["whatItIs"]) == (
        "keep",
        "unfinished",
        "work",
    )


def test_memory_scan_preserves_review_and_unknown_metadata(tmp_path, monkeypatch):
    path = seed(tmp_path)
    note = str(tmp_path / "note.md")

    def scan(_, **__):
        mark_mem_reviewed.cmd_mark_mem_reviewed([note, "--registry", str(path)])
        keep.cmd_keep(["same", "--yes", "--registry", str(path)])
        return [MemoryEntry(note, "encoded", str(tmp_path), LATER, "note", None, None, "body")]

    monkeypatch.setattr(mem_scan.memory, "scan_memory", scan)
    assert mem_scan.cmd_mem_scan(["--registry", str(path)]) == 0
    reg = registry.load_registry(path)
    assert reg["memoryEntries"][0]["reviewedAtMtime"] == NOW
    assert reg["memoryEntries"][0]["future"] is True
    assert reg["threads"][0]["decision"] == "keep"


def test_archive_preserves_metadata_written_while_moving_and_recovers_scan_gc(
    tmp_path, monkeypatch
):
    path = seed(tmp_path)
    real_move = lifecycle.shutil.move

    def move(src, dest):
        result = real_move(src, dest)
        current = registry.load_registry(path)
        current["threads"][0]["lineage"]["operation"] = "conversion"
        registry.commit_registry(path, current)
        current = registry.reconcile(registry.load_registry(path), [], LATER)
        registry.commit_registry(path, current)
        return result

    monkeypatch.setattr(lifecycle.shutil, "move", move)
    assert (
        archive.cmd_archive(
            ["same", "--registry", str(path), "--archive-root", str(tmp_path / "archive")]
        )
        == 0
    )
    reg = registry.load_registry(path)
    assert reg["archived"][0]["thread"]["lineage"] == {
        "parent": "origin",
        "operation": "conversion",
    }
    assert reg["threads"] == []
    assert reg["deleted"] == []


def test_conflict_preserves_disk_and_releases_lock(tmp_path):
    path = seed(tmp_path)
    first, stale = registry.load_registry(path), registry.load_registry(path)
    registry.apply_name(first, "same", "first")
    registry.apply_name(stale, "same", "stale")
    registry.commit_registry(path, first)
    original = path.read_bytes()
    with pytest.raises(registry.RegistryConflict, match="changed concurrently"):
        registry.commit_registry(path, stale)
    assert path.read_bytes() == original
    registry.mutate_registry(path, lambda reg: registry.keep_thread(reg, "same", NOW))
    assert registry.load_registry(path)["threads"][0]["decision"] == "keep"


def test_mutation_exception_preserves_state(tmp_path):
    path = seed(tmp_path)
    original = path.read_bytes()

    def fail(reg):
        reg["threads"].clear()
        raise RuntimeError("failed mutation")

    with pytest.raises(RuntimeError, match="failed mutation"):
        registry.mutate_registry(path, fail)
    assert path.read_bytes() == original
    registry.mutate_registry(path, lambda reg: registry.keep_thread(reg, "same", NOW))
    assert registry.load_registry(path)["threads"][0]["decision"] == "keep"


def test_source_collision_refuses_unqualified_delete_before_touching_files(tmp_path, capsys):
    path = seed(tmp_path, collision=True)
    before = path.read_bytes()
    assert main(["session", "delete", "same", "--registry", str(path)]) == 1
    assert "ambiguous session" in capsys.readouterr().err
    assert path.read_bytes() == before
    assert (tmp_path / "claude-code.jsonl").read_text() == "{}\n"
    assert (tmp_path / "codex.jsonl").read_text() == "{}\n"
    assert main(["session", "delete", "claude:same", "--registry", str(path)]) == 0
    reg = registry.load_registry(path)
    assert [registry.identity(row) for row in reg["threads"]] == [("codex", "same")]
    assert registry.identity(reg["deleted"][0]) == ("claude-code", "same")
    assert reg["deleted"][0]["thread"]["lineage"] == {"parent": "origin"}


def test_archived_collision_does_not_hide_other_source_on_scan(tmp_path):
    path = seed(tmp_path, collision=True)
    reg = registry.load_registry(path)
    registry.archive_session(reg, "claude:same", "/archive", NOW)
    registry.commit_registry(path, reg)
    result = registry.reconcile(reg, [scanned(tmp_path, source="codex")], LATER)
    registry.commit_registry(path, result)
    assert [registry.identity(row) for row in result["threads"]] == [("codex", "same")]
    with pytest.raises(registry.AmbiguousSession):
        registry.keep_thread(result, "same", NOW)
    registry.keep_thread(result, "codex:same", NOW)
    assert result["threads"][0]["decision"] == "keep"


def test_stale_scan_does_not_resurrect_archived_thread(tmp_path):
    path = seed(tmp_path)
    stale = registry.reconcile(registry.load_registry(path), [scanned(tmp_path)], LATER)
    current = registry.load_registry(path)
    registry.archive_session(current, "same", "/archive", NOW)
    registry.commit_registry(path, current)
    registry.commit_registry(path, stale)
    assert stale["threads"] == []
    assert stale["archived"][0]["thread"]["lineage"] == {"parent": "origin"}


def test_harvest_reconciliation_qualifies_and_refuses_ambiguous_legacy_reference(tmp_path):
    path = seed(tmp_path, collision=True)
    reg = registry.load_registry(path)
    before = deepcopy(reg)
    with pytest.raises(registry.AmbiguousSession):
        registry.reconcile_harvest_paths(reg, {"same": ["docs/note.md"]})
    assert reg == before
    registry.reconcile_harvest_paths(reg, {"codex:same": ["docs/note.md"]})
    assert registry.find_session(reg, "codex:same")["harvestPath"] == ["docs/note.md"]
    assert registry.find_session(reg, "claude:same")["harvestPath"] is None


def test_prune_preserves_concurrent_lineage_and_memory_changes(tmp_path):
    from ringleader import prune

    path = seed(tmp_path)
    stale = registry.load_registry(path)
    current = registry.load_registry(path)
    current["threads"][0]["lineage"]["operation"] = "conversion"
    registry.mark_memory_reviewed(current, str(tmp_path / "note.md"))
    registry.commit_registry(path, current)
    assert prune.delete_sessions(stale, list(stale["threads"]), LATER) == 1
    registry.commit_registry(path, stale)
    saved = registry.load_registry(path)
    assert saved["deleted"][0]["thread"]["lineage"] == {
        "parent": "origin",
        "operation": "conversion",
    }
    assert saved["memoryEntries"][0]["reviewedAtMtime"] == NOW


def test_stale_scan_cannot_regress_metadata_from_newer_scan(tmp_path):
    path = seed(tmp_path)
    stale = registry.reconcile(
        registry.load_registry(path), [scanned(tmp_path, custom_title="stale")], NOW
    )
    current = registry.load_registry(path)
    current["threads"][0]["mtime"] = "2026-09-22T12:00:00+00:00"
    current["threads"][0]["sizeBytes"] = 100
    registry.commit_registry(path, current)
    registry.commit_registry(path, stale)
    thread = registry.load_registry(path)["threads"][0]
    assert thread["mtime"] == "2026-09-22T12:00:00+00:00"
    assert thread["sizeBytes"] == 100
    assert thread.get("title") is None


def test_legacy_and_qualified_harvest_references_merge_for_same_source(tmp_path):
    path = seed(tmp_path)
    reg = registry.load_registry(path)
    registry.reconcile_harvest_paths(reg, {"same": ["legacy.md"], "claude-code:same": ["new.md"]})
    assert reg["threads"][0]["harvestPath"] == ["legacy.md", "new.md"]


def test_stale_memory_review_reports_removed_entry_without_resurrecting(tmp_path):
    path = seed(tmp_path)
    stale = registry.load_registry(path)
    registry.mark_memory_reviewed(stale, str(tmp_path / "note.md"))
    registry.mutate_registry(path, lambda reg: reg["memoryEntries"].clear())
    before = path.read_bytes()
    with pytest.raises(registry.RegistryConflict, match="memory entry disappeared"):
        registry.commit_registry(path, stale)
    assert path.read_bytes() == before


def test_registry_alias_uses_same_target_and_preserves_symlink(tmp_path):
    path = seed(tmp_path)
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    first, second = registry.load_registry(path), registry.load_registry(alias)
    registry.keep_thread(first, "same", NOW)
    registry.mark_memory_reviewed(second, str(tmp_path / "note.md"))
    registry.commit_registry(path, first)
    registry.commit_registry(alias, second)
    assert alias.is_symlink()
    current = registry.load_registry(path)
    assert current["threads"][0]["decision"] == "keep"
    assert current["memoryEntries"][0]["reviewedAtMtime"] == NOW


@pytest.mark.parametrize(
    "change",
    [
        {"decision": "keep"},
        {"decision": "harvested"},
        {"mtime": "2026-10-01T12:00:00+00:00"},
        {"messageCount": 100},
    ],
)
def test_prune_revalidates_current_eligibility_before_deletion(tmp_path, change):
    from ringleader import prune

    path = seed(tmp_path)
    stale = registry.load_registry(path)
    now = "2026-10-01T12:00:00+00:00"
    candidates = prune.prune_candidates(stale, now)
    assert len(candidates) == 1
    registry.mutate_registry(path, lambda current: current["threads"][0].update(change))
    assert prune.delete_sessions(stale, candidates, now) == 0
    registry.commit_registry(path, stale)
    saved = registry.load_registry(path)
    assert {key: saved["threads"][0][key] for key in change} == change
    assert (tmp_path / "claude-code.jsonl").read_text() == "{}\n"
    assert saved["deleted"] == []


def test_prune_confirmation_cannot_undo_intervening_keep(tmp_path, monkeypatch):
    from ringleader.commands import prune as command

    path = seed(tmp_path)
    monkeypatch.setattr(command, "_now", lambda: "2026-10-01T12:00:00+00:00")

    def confirm(_):
        assert keep.cmd_keep(["same", "--yes", "--registry", str(path)]) == 0
        return "yes"

    monkeypatch.setattr("builtins.input", confirm)
    assert command.cmd_prune(["--delete", "--registry", str(path)]) == 0
    assert registry.load_registry(path)["threads"][0]["decision"] == "keep"
    assert (tmp_path / "claude-code.jsonl").read_text() == "{}\n"


def test_prune_serializes_metadata_writers_through_unlink(tmp_path, monkeypatch):
    from ringleader import prune

    path = seed(tmp_path)
    stale = registry.load_registry(path)
    started, entered = threading.Event(), threading.Event()

    def update(current):
        entered.set()
        registry.mark_memory_reviewed(current, str(tmp_path / "note.md"))

    def writer():
        started.set()
        registry.mutate_registry(path, update)

    worker = threading.Thread(target=writer)

    def hide(_):
        worker.start()
        assert started.wait(2)
        assert not entered.wait(0.05)
        assert (tmp_path / "claude-code.jsonl").is_file()

    monkeypatch.setattr(prune.scanner, "hide_from_picker", hide)
    try:
        assert prune.delete_sessions(stale, list(stale["threads"]), LATER) == 1
    finally:
        worker.join(3)
    assert not worker.is_alive()
    assert entered.is_set()
    saved = registry.load_registry(path)
    assert saved["deleted"][0]["sessionId"] == "same"
    assert saved["memoryEntries"][0]["reviewedAtMtime"] == NOW


def test_refresh_emits_valid_json_when_another_writer_registers_pending_session(
    tmp_path, monkeypatch, capsys
):
    from ringleader.commands import sync

    path = tmp_path / "registry.json"
    registry.save_registry(path, registry.empty_registry())

    def scan(**_):
        current = registry.reconcile(
            registry.load_registry(path), [scanned(tmp_path, source="pi", session_id="new")], NOW
        )
        registry.commit_registry(path, current)
        return []

    monkeypatch.setattr(sync.scanner, "active_sources", lambda **_: [])
    monkeypatch.setattr(sync.scanner, "scan", scan)
    assert sync.cmd_refresh(["--registry", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"pendingCount": 0, "total": 1, "pending": []}
    assert registry.find_session(registry.load_registry(path), "pi:new")["status"] == "unclassified"


def test_search_distinguishes_sources_while_collapsing_same_source_duplicates():
    from ringleader.search import search

    reg = registry.empty_registry()
    reg["threads"] = [{"sessionId": "same", "source": "codex"}]
    reg["archived"] = [
        {"sessionId": "same", "source": source} for source in ("claude-code", "codex")
    ]
    results = search(reg, "same")
    assert {(registry.identity(item.thread), item.archived) for item in results} == {
        (("codex", "same"), False),
        (("claude-code", "same"), True),
    }


def test_a_concurrent_lifecycle_action_does_not_revive_a_source_exclusion(tmp_path):
    path = seed(tmp_path)
    stale = registry.load_registry(path)
    excluded = registry.reconcile(
        registry.load_registry(path),
        [],
        LATER,
        partial=True,
        excluded_sessions=frozenset({("claude-code", "same")}),
    )
    registry.commit_registry(path, excluded)
    registry.prune_session(stale, "same", LATER)
    with pytest.raises(registry.RegistryConflict, match="session lifecycle changed concurrently"):
        registry.commit_registry(path, stale)
    saved = registry.load_registry(path)
    assert [(row["sessionId"], row["reason"]) for row in saved["deleted"]] == [("same", "excluded")]
