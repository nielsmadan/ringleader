from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from ringleader import registry
from ringleader.portability.adapters import builtin_adapters
from ringleader.portability.capabilities import Adapter, RunResult, SourceRequest, TargetRequest
from ringleader.portability.compare import compare
from ringleader.portability.model import Identity, PreservationError, ToolResult
from ringleader.portability.operations import OperationStore
from ringleader.portability.pi import PROFILE, PiAdapter
from ringleader.portability.service import ConversionRequest, ConversionService
from tests.portability.test_codex import initialized
from tests.portability.test_native_adapters import roots, shared
from tests.portability.test_opencode import roots as opencode_roots


class OfflineRunner:
    def run(self, request):
        versions = {
            "pi": b"0.85.1",
            "claude": b"2.1.278 (Claude Code)",
            "codex": b"codex-cli 0.155.1",
        }
        assert request.argv[-1] == "--version"
        return RunResult(0, versions[request.argv[0]], b"")


class Crash(BaseException):
    pass


def source(tmp_path):
    original = shared()
    history = tuple(
        replace(
            message,
            content=tuple(
                replace(item, is_error=False) if isinstance(item, ToolResult) else item
                for item in message.content
            ),
        )
        for message in original.history
    )
    original = replace(original, history=history, context=history)
    native_roots = roots(tmp_path / "source")
    native_roots.cwd.mkdir(parents=True)
    staged = PiAdapter().stage(
        original,
        TargetRequest(
            Identity("pi", str(uuid4())),
            native_roots,
            PROFILE,
            original.title,
        ),
    )
    for item in staged.files:
        path = native_roots.store / item.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item.data)
    return SourceRequest(staged.identity, native_roots, staged.source_ref)


def service(tmp_path, *, fault=None, adapters=None):
    return ConversionService(
        adapters or builtin_adapters(),
        OfflineRunner(),
        operations_path=tmp_path / "state" / "conversion",
        registry_path=tmp_path / "state" / "registry.json",
        fault=fault,
    )


def request(tmp_path, agent="pi", *, target=None):
    return ConversionRequest(source(tmp_path), agent, target or roots(tmp_path / "target"))


def crash_at(point):
    def fault(name, operation):
        if name == point:
            raise Crash(operation.id)

    return fault


def operation_after_crash(tmp_path, conversion, point):
    worker = service(tmp_path, fault=crash_at(point))
    with pytest.raises(Crash):
        worker.convert(conversion)
    restarted = service(tmp_path)
    operation = restarted.transactions()[0]
    return restarted, operation, restarted.store.artifacts(operation).stage.native()


@pytest.mark.parametrize("agent", ["claude-code", "pi", "codex", "opencode"])
@pytest.mark.parametrize("compact", [False, True])
def test_actual_adapter_publication_and_repeat(tmp_path, agent, compact):
    target = (
        initialized(tmp_path / "target")
        if agent == "codex"
        else opencode_roots.__wrapped__(tmp_path / "target")
        if agent == "opencode"
        else roots(tmp_path / "target")
    )
    conversion = replace(request(tmp_path, agent, target=target), compact=compact)
    worker = service(tmp_path)
    source_bytes = (conversion.source.roots.store / conversion.source.source_ref).read_bytes()
    result = worker.convert(conversion)
    assert result.lifecycle == "active"
    assert (
        conversion.source.roots.store / conversion.source.source_ref
    ).read_bytes() == source_bytes
    repeated = service(tmp_path).convert(conversion)
    assert (repeated.operation_id, repeated.target, repeated.reused) == (
        result.operation_id,
        result.target,
        True,
    )
    operation = worker.transactions()[0]
    artifacts = worker.store.artifacts(operation)
    staged = artifacts.stage.native()
    assert staged.roots.cwd == conversion.source.roots.cwd.resolve()
    compare(
        artifacts.conversation,
        worker.adapters.reader(agent)
        .snapshot(
            SourceRequest(
                result.target,
                staged.roots,
                str(staged.roots.store / staged.source_ref),
            )
        )
        .conversation,
        worker.adapters.writer(agent).normalizations(staged.profile),
    )
    lineage = worker.lineage(conversion.source.identity)
    assert lineage[0]["source"] == {"agent": "pi", "nativeId": conversion.source.identity.native_id}
    record = registry.find_session(
        registry.load_registry(worker.registry_path), result.target.native_id, source=agent
    )
    assert (record["status"], record["keptAt"], record["lineage"]["operationId"]) == (
        "unclassified",
        None,
        operation.id,
    )
    assert record["messageCount"] == len(artifacts.conversation.history)
    shutil.rmtree(conversion.source.roots.store)
    readback = worker.adapters.reader(agent).snapshot(
        SourceRequest(result.target, staged.roots, staged.source_ref)
    )
    compare(
        artifacts.conversation,
        readback.conversation,
        worker.adapters.writer(agent).normalizations(staged.profile),
    )
    assert worker.resume(operation.id).lifecycle == "active"
    assert worker.store.directory(operation.id).stat().st_mode & 0o777 == 0o700
    assert (worker.store.directory(operation.id) / "artifacts.json").stat().st_mode & 0o777 == 0o600


def tree(root):
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("agent", ["pi", "codex", "opencode"])
def test_dry_run_writes_nothing_and_snapshots_once(tmp_path, agent):
    target = (
        initialized(tmp_path / "target")
        if agent == "codex"
        else opencode_roots.__wrapped__(tmp_path / "target")
        if agent == "opencode"
        else roots(tmp_path / "target")
    )
    conversion = request(tmp_path, agent, target=target)
    worker = service(tmp_path)
    before = tree(tmp_path)
    result = worker.convert(conversion, dry_run=True)
    assert (result.dry_run, result.operation_id, result.lifecycle) == (True, None, "preview")
    assert tree(tmp_path) == before
    assert worker.transactions() == ()


@pytest.mark.parametrize(
    "point",
    [
        "after:create",
        "before:prepare:0:intent",
        "after:prepare:0:intent",
        "before:prepare:0:action",
        "after:prepare:0:action",
        "before:prepare:0:done",
        "after:prepare:0:done",
        "before:file:0:intent",
        "after:file:0:action",
        "before:file:0:done",
        "after:file:0:done",
        "before:registry:intent",
        "after:registry:intent",
        "before:registry:action",
        "after:registry:action",
        "before:registry:done",
        "after:registry:done",
        "before:complete",
        "after:complete",
    ],
)
def test_file_and_registry_boundary_restart(tmp_path, point):
    conversion = request(tmp_path)
    restarted, operation, staged = operation_after_crash(tmp_path, conversion, point)
    result = restarted.resume(operation.id)
    assert (result.lifecycle, result.target) == ("active", operation.target)
    assert (staged.roots.store / staged.source_ref).read_bytes() == staged.files[0].data
    assert restarted.transactions()[0].state == "complete"
    assert len(registry.load_registry(restarted.registry_path)["threads"]) == 1


@pytest.mark.parametrize("point", ["after:file:0:intent", "before:file:0:action"])
def test_unobservable_file_intent_is_diagnostic_not_recreated(tmp_path, point):
    conversion = request(tmp_path)
    restarted, operation, staged = operation_after_crash(tmp_path, conversion, point)
    with pytest.raises(PreservationError, match="never recreate"):
        restarted.resume(operation.id)
    assert list(staged.roots.store.rglob("*.jsonl")) == []
    with pytest.raises(PreservationError, match="forward recovery"):
        restarted.rollback(operation.id)


@pytest.mark.parametrize(
    "point",
    [
        "before:index:intent",
        "after:index:action",
        "before:index:done",
        "after:index:done",
        "before:database:intent",
        "after:database:action",
        "before:database:done",
        "after:database:done",
    ],
)
def test_codex_index_database_boundaries(tmp_path, point):
    conversion = request(tmp_path, "codex", target=initialized(tmp_path / "target"))
    restarted, operation, staged = operation_after_crash(tmp_path, conversion, point)
    result = restarted.resume(operation.id)
    assert result.lifecycle == "active"
    index = (staged.roots.store / "session_index.jsonl").read_text().splitlines()
    assert len(index) == 1
    with sqlite3.connect(staged.roots.store / "state_5.sqlite") as db:
        assert db.execute("SELECT id,tokens_used FROM threads").fetchall() == [
            (result.target.native_id, 0)
        ]


@pytest.mark.parametrize(
    "point",
    [
        "after:index:intent",
        "before:index:action",
        "after:database:intent",
        "before:database:action",
    ],
)
def test_ambiguous_index_or_database_intent_refuses_recreation(tmp_path, point):
    conversion = request(tmp_path, "codex", target=initialized(tmp_path / "target"))
    restarted, operation, staged = operation_after_crash(tmp_path, conversion, point)
    before = tree(staged.roots.store)
    with pytest.raises(PreservationError, match=r"never reappend|never recreate"):
        restarted.resume(operation.id)
    assert tree(staged.roots.store) == before
    with pytest.raises(PreservationError, match="forward recovery"):
        restarted.rollback(operation.id)


def test_codex_index_only_failure_preserves_later_rename_and_unrelated_append(tmp_path):
    conversion = request(tmp_path, "codex", target=initialized(tmp_path / "target"))
    restarted, operation, staged = operation_after_crash(tmp_path, conversion, "after:index:action")
    path = staged.roots.store / "session_index.jsonl"
    with path.open("a") as stream:
        stream.write(
            json.dumps(
                {"id": operation.target.native_id, "thread_name": "Renamed", "updated_at": "later"}
            )
            + "\n"
        )
        stream.write('{"id":"unrelated","thread_name":"Elsewhere"}\n')
    before = path.read_bytes()
    assert restarted.resume(operation.id).lifecycle == "active"
    assert path.read_bytes() == before


@pytest.mark.parametrize("agent", ["codex", "opencode"])
def test_committed_database_before_receipt_preserves_native_edits(tmp_path, agent):
    target = (
        initialized(tmp_path / "target")
        if agent == "codex"
        else opencode_roots.__wrapped__(tmp_path / "target")
    )
    conversion = request(tmp_path, agent, target=target)
    restarted, operation, staged = operation_after_crash(
        tmp_path, conversion, "after:database:action"
    )
    if agent == "codex":
        with (target.store / staged.source_ref).open("ab") as stream:
            stream.write(b'{"native":"appended"}\n')
        with sqlite3.connect(target.store / "state_5.sqlite") as db:
            db.execute(
                "UPDATE threads SET name='Renamed', tokens_used=123 WHERE id=?",
                (operation.target.native_id,),
            )
    else:
        with sqlite3.connect(target.store / "opencode.db") as db:
            db.execute(
                "UPDATE session SET title='Renamed' WHERE id=?", (operation.target.native_id,)
            )
            db.execute(
                "UPDATE part SET data=? WHERE session_id=?",
                ('{"type":"text","text":"edited"}', operation.target.native_id),
            )
    before = tree(target.store)
    assert restarted.resume(operation.id).lifecycle == "active"
    assert tree(target.store) == before


@pytest.mark.parametrize("mutation", ["append", "edit", "replace", "delete"])
def test_file_intent_recovery_checks_inode_and_preserves_changes(tmp_path, mutation):
    conversion = request(tmp_path)
    restarted, operation, staged = operation_after_crash(
        tmp_path, conversion, "after:file:0:action"
    )
    path = staged.roots.store / staged.source_ref
    if mutation == "append":
        with path.open("ab") as stream:
            stream.write(b'{"new":"turn"}\n')
    elif mutation == "edit":
        path.write_bytes(b"native edit")
    elif mutation == "replace":
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
    else:
        path.unlink()
    before = path.read_bytes() if path.exists() else None
    if mutation == "append":
        assert restarted.resume(operation.id).lifecycle == "active"
    else:
        with pytest.raises(PreservationError, match="never recreate"):
            restarted.resume(operation.id)
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize(
    "lifecycle", ["archived", "deleted", "purged", "removed", "native_deleted", "native_changed"]
)
def test_completed_retry_keeps_lifecycle(tmp_path, lifecycle):
    conversion = request(tmp_path)
    worker = service(tmp_path)
    result = worker.convert(conversion)
    selector = f"pi:{result.target.native_id}"
    if lifecycle == "archived":
        registry.mutate_registry(
            worker.registry_path,
            lambda reg: registry.archive_session(reg, selector, "archived", "now"),
        )
    elif lifecycle == "deleted":
        registry.mutate_registry(
            worker.registry_path, lambda reg: registry.prune_session(reg, selector, "now")
        )
    elif lifecycle == "purged":
        registry.mutate_registry(
            worker.registry_path,
            lambda reg: registry.archive_session(reg, selector, "archived", "now"),
        )
        registry.mutate_registry(
            worker.registry_path,
            lambda reg: registry.purge_archived_session(reg, selector, "later"),
        )
    elif lifecycle == "removed":
        registry.mutate_registry(worker.registry_path, lambda reg: reg.update(threads=[]))
    else:
        path = conversion.target_roots.store / result.source_ref
        if lifecycle == "native_deleted":
            path.unlink()
        else:
            path.write_bytes(b"edited target")
    before = tree(conversion.target_roots.store)
    repeated = service(tmp_path).convert(conversion)
    expected = {
        "purged": "deleted",
        "removed": "missing",
        "native_deleted": "missing",
        "native_changed": "changed",
    }.get(lifecycle, lifecycle)
    assert (repeated.operation_id, repeated.lifecycle) == (result.operation_id, expected)
    assert tree(conversion.target_roots.store) == before
    assert worker.lineage(result.target)[0]["operationId"] == result.operation_id


@pytest.mark.parametrize("agent", ["codex", "opencode"])
def test_native_archive_and_delete_are_returned_without_registry_tombstone(tmp_path, agent):
    target = (
        initialized(tmp_path / "target")
        if agent == "codex"
        else opencode_roots.__wrapped__(tmp_path / "target")
    )
    conversion = request(tmp_path, agent, target=target)
    worker = service(tmp_path)
    result = worker.convert(conversion)
    database = target.store / ("state_5.sqlite" if agent == "codex" else "opencode.db")
    table = "threads" if agent == "codex" else "session"
    assignment = "archived=1" if agent == "codex" else "time_archived=1"
    with sqlite3.connect(database) as db:
        db.execute(f"UPDATE {table} SET {assignment} WHERE id=?", (result.target.native_id,))
    assert service(tmp_path).convert(conversion).lifecycle == "archived"
    with sqlite3.connect(database) as db:
        db.execute(f"DELETE FROM {table} WHERE id=?", (result.target.native_id,))
    assert service(tmp_path).convert(conversion).lifecycle == "missing"


def test_forks_and_identity_bindings(tmp_path):
    conversion = request(tmp_path)
    worker = service(tmp_path)
    first = worker.convert(conversion)
    fork = worker.convert(replace(conversion, fork=True))
    fork2 = worker.convert(replace(conversion, fork=True))
    alternate_store = worker.convert(replace(conversion, target_roots=roots(tmp_path / "other")))
    workspace = worker.convert(
        replace(
            conversion,
            source=replace(
                conversion.source,
                roots=replace(conversion.source.roots, cwd=tmp_path / "retargeted"),
            ),
        )
    )
    compact_result = worker.convert(replace(conversion, compact=True))
    renamed = worker.convert(replace(conversion, title="New source title"))
    assert (
        len(
            {
                item.target
                for item in (
                    first,
                    fork,
                    fork2,
                    alternate_store,
                    workspace,
                    compact_result,
                    renamed,
                )
            }
        )
        == 7
    )
    assert worker.convert(conversion).target == first.target
    path = conversion.source.roots.store / conversion.source.source_ref
    parent = json.loads(path.read_text().splitlines()[-1])["id"]
    with path.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "session_info",
                    "id": "bbbbbbbb",
                    "parentId": parent,
                    "name": "Source rename",
                }
            )
            + "\n"
        )
    changed = worker.convert(conversion)
    assert changed.target != first.target


def test_source_effective_workspace_replaces_native_header_workspace(tmp_path):
    conversion = request(tmp_path)
    effective = tmp_path / "moved" / "workspace"
    conversion = replace(
        conversion,
        source=replace(conversion.source, roots=replace(conversion.source.roots, cwd=effective)),
    )
    worker = service(tmp_path)
    result = worker.convert(conversion)
    path = conversion.target_roots.store / result.source_ref
    assert json.loads(path.read_text().splitlines()[0])["cwd"] == str(effective)
    record = registry.find_session(
        registry.load_registry(worker.registry_path), result.target.native_id, source="pi"
    )
    assert record["project"] == str(effective)


def test_registry_mutations_and_scan_between_publication_and_receipt_survive(tmp_path):
    conversion = request(tmp_path)
    worker = service(tmp_path)

    def interleave(name, operation):
        if name == "before:registry:action":

            def mutation(reg):
                reg["threads"].append(
                    {"source": "pi", "sessionId": "other", "appliedName": "Keep me"}
                )
                reg["memoryEntries"].append({"path": "memory", "reviewedAt": "now"})
                reg["projectsCleanedAt"]["other"] = "today"

            registry.mutate_registry(worker.registry_path, mutation)

    worker.fault = interleave
    result = worker.convert(conversion)
    loaded = registry.load_registry(worker.registry_path)
    assert registry.find_session(loaded, "pi:other")["appliedName"] == "Keep me"
    assert loaded["memoryEntries"] == [{"path": "memory", "reviewedAt": "now"}]
    assert loaded["projectsCleanedAt"] == {"other": "today"}
    assert len(worker.lineage(result.target)) == 1


def test_partial_batch_retry_retains_successes(tmp_path):
    good = request(tmp_path)
    bad = replace(good, target_agent="unknown")
    results = service(tmp_path).batch((good, bad))
    assert results[0].result.lifecycle == "active"
    assert "No portability adapter" in results[1].error
    retry = service(tmp_path).batch((good, bad))
    assert retry[0].result.target == results[0].result.target
    assert retry[0].result.reused is True
    assert retry[1].error == results[1].error


@pytest.mark.parametrize("point", ["after:create", "before:file:0:intent"])
def test_private_rollback_is_repeatable_and_new_conversion_is_fresh(tmp_path, point):
    conversion = request(tmp_path)
    restarted, operation, staged = operation_after_crash(tmp_path, conversion, point)
    rolled_back = restarted.rollback(operation.id)
    assert (rolled_back.state, restarted.rollback(operation.id).state) == (
        "rolled_back",
        "rolled_back",
    )
    with pytest.raises(PreservationError, match="rolled back"):
        restarted.resume(operation.id)
    assert list(staged.roots.store.rglob("*.pending")) == []
    assert service(tmp_path).convert(conversion).target != operation.target


def test_atomic_complete_file_and_collision(tmp_path):
    conversion = request(tmp_path)
    worker = service(tmp_path)

    def collision(name, operation):
        if name == "before:file:0:action":
            staged = worker.store.artifacts(operation).stage.native()
            path = staged.roots.store / staged.source_ref
            path.write_bytes(b"unrelated collision")

    worker.fault = collision
    with pytest.raises(FileExistsError):
        worker.convert(conversion)
    operation = worker.transactions()[0]
    staged = worker.store.artifacts(operation).stage.native()
    assert (staged.roots.store / staged.source_ref).read_bytes() == b"unrelated collision"
    with pytest.raises(PreservationError, match="never recreate"):
        service(tmp_path).resume(operation.id)


def test_hardlink_uses_target_filesystem_and_never_exposes_partial_bytes(tmp_path, monkeypatch):
    conversion = request(tmp_path)
    link = os.link
    seen = []

    def inspect(source_path, target_path):
        assert source_path.parent == target_path.parent
        assert source_path.suffix == ".pending"
        payload = source_path.read_bytes()
        assert json.loads(payload.splitlines()[0])["type"] == "session"
        assert json.loads(payload.splitlines()[-1])["type"] == "session_info"
        link(source_path, target_path)
        assert target_path.read_bytes() == payload
        seen.append(target_path)

    monkeypatch.setattr(os, "link", inspect)
    assert service(tmp_path).convert(conversion).lifecycle == "active"
    assert len(seen) == 1


def test_configuration_drift_prevents_first_external_publication(tmp_path):
    target = opencode_roots.__wrapped__(tmp_path / "target")
    conversion = request(tmp_path, "opencode", target=target)
    config = target.config / "opencode" / "opencode.json"
    before = tree(target.store)

    def drift(point, operation):
        if point == "after:create":
            config.write_text('{"model":"anthropic/another-real-configured-model"}')

    with pytest.raises(PreservationError, match="configuration changed"):
        service(tmp_path, fault=drift).convert(conversion)
    assert tree(target.store) == before
    first = service(tmp_path).transactions()[0]
    result = service(tmp_path).convert(conversion)
    assert result.operation_id != first.id


def test_snapshot_called_once_and_preservation_refusal_precedes_journal(tmp_path):
    conversion = request(tmp_path)
    adapters = builtin_adapters()
    delegate = PiAdapter()

    class CountingReader:
        calls = 0

        def snapshot(self, source_request):
            self.calls += 1
            return delegate.snapshot(source_request)

    class BrokenWriter:
        def inspect_target(self, *args):
            return delegate.inspect_target(*args)

        def stage(self, *args):
            return delegate.stage(*args)

        def read_stage(self, stage):
            return replace(delegate.read_stage(stage), title="Wrong title")

        def normalizations(self, profile):
            return delegate.normalizations(profile)

    reader = CountingReader()
    adapters._adapters["pi"] = Adapter("pi", reader, BrokenWriter())
    worker = service(tmp_path, adapters=adapters)
    before = tree(tmp_path)
    with pytest.raises(PreservationError, match="Preservation mismatch"):
        worker.convert(conversion)
    assert reader.calls == 1
    assert tree(tmp_path) == before
    assert worker.transactions() == ()


@pytest.mark.parametrize("mutation", ["version", "unknown", "steps", "symlink", "artifacts"])
def test_malformed_journals_cannot_redirect_cleanup(tmp_path, mutation):
    conversion = request(tmp_path)
    worker, operation, _staged = operation_after_crash(tmp_path, conversion, "after:create")
    directory = worker.store.directory(operation.id)
    journal_path = directory / "journal.json"
    journal = json.loads(journal_path.read_bytes())
    outside = tmp_path / "precious"
    outside.write_bytes(b"preserve")
    if mutation == "version":
        journal["version"] = 999
    elif mutation == "unknown":
        journal["private_path"] = str(outside)
    elif mutation == "steps":
        journal["steps"] = [
            {"type": "Step", "name": "prepare:999", "phase": "done", "device": 0, "inode": 0}
        ]
    elif mutation == "symlink":
        (directory / "artifacts.json").unlink()
        (directory / "artifacts.json").symlink_to(outside)
    else:
        (directory / "artifacts.json").write_bytes(b"modified")
    journal_path.write_text(json.dumps(journal))
    with pytest.raises(PreservationError):
        worker.resume(operation.id)
    assert outside.read_bytes() == b"preserve"
    with pytest.raises(PreservationError):
        OperationStore(worker.store.root).load("../precious")


@pytest.mark.parametrize("agent", ["codex", "opencode"])
@pytest.mark.parametrize("point", ["before:database:action", "after:database:action"])
def test_unrelated_same_id_database_collision_is_refused(tmp_path, agent, point):
    target = (
        initialized(tmp_path / "target")
        if agent == "codex"
        else opencode_roots.__wrapped__(tmp_path / "target")
    )
    conversion = request(tmp_path, agent, target=target)
    restarted, operation, staged = operation_after_crash(tmp_path, conversion, point)
    adapter = restarted.adapters.get(agent)
    if point == "before:database:action":
        adapter.registration.register_stage(staged)
    if agent == "codex":
        with sqlite3.connect(target.store / "state_5.sqlite") as db:
            db.execute(
                "UPDATE threads SET cwd='/unrelated' WHERE id=?", (operation.target.native_id,)
            )
    else:
        with sqlite3.connect(target.store / "opencode.db") as db:
            db.execute("UPDATE session SET metadata='{}' WHERE id=?", (operation.target.native_id,))
    before = tree(target.store)
    with pytest.raises(PreservationError, match="conflict"):
        restarted.resume(operation.id)
    assert tree(target.store) == before


@pytest.mark.parametrize(
    "point", ["after:database:intent", "after:database:action", "after:database:done"]
)
def test_opencode_database_restart_boundaries(tmp_path, point):
    target = opencode_roots.__wrapped__(tmp_path / "target")
    conversion = request(tmp_path, "opencode", target=target)
    restarted, operation, _staged = operation_after_crash(tmp_path, conversion, point)
    if point == "after:database:intent":
        with pytest.raises(PreservationError, match="never recreate"):
            restarted.resume(operation.id)
    else:
        assert restarted.resume(operation.id).lifecycle == "active"
    with sqlite3.connect(target.store / "opencode.db") as db:
        expected = 0 if point == "after:database:intent" else 1
        assert db.execute("SELECT count(*) FROM session").fetchone() == (expected,)


def test_parallel_converts_share_one_operation(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    conversion = request(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: service(tmp_path).convert(conversion), range(4)))
    assert len({result.target for result in results}) == 1
    assert sorted(result.reused for result in results) == [False, True, True, True]
    assert len(service(tmp_path).transactions()) == 1


def test_refresh_scanned_target_and_user_fields_survive_registry_receipt(tmp_path):
    conversion = request(tmp_path)
    restarted, operation, staged = operation_after_crash(
        tmp_path, conversion, "after:file:0:action"
    )
    from ringleader.sources.base import NormalizedSession

    def scan(reg):
        result = registry.reconcile(
            reg,
            [
                NormalizedSession(
                    source="pi",
                    session_id=operation.target.native_id,
                    cwd=str(staged.roots.cwd),
                    mtime="now",
                    size_bytes=len(staged.files[0].data),
                    custom_title="User renamed",
                    summary=None,
                    last_prompt=None,
                    file=str(staged.roots.store / staged.source_ref),
                )
            ],
            "now",
        )
        result["threads"][0]["keptAt"] = "user decided"
        reg.update(result)

    registry.mutate_registry(restarted.registry_path, scan)
    assert restarted.resume(operation.id).lifecycle == "active"
    record = registry.load_registry(restarted.registry_path)["threads"][0]
    assert (record["title"], record["keptAt"], record["lineage"]["operationId"]) == (
        "User renamed",
        "user decided",
        operation.id,
    )


@pytest.mark.parametrize("stage", ["before", "after"])
def test_initial_artifact_durable_write_crash_has_private_rollback(tmp_path, monkeypatch, stage):
    from ringleader.portability import operations

    conversion = request(tmp_path)
    write = operations.atomic_write_bytes

    def interrupted(path, data, **kwargs):
        if path.name == "artifacts.json" and stage == "before":
            raise Crash()
        write(path, data, **kwargs)
        if path.name == "artifacts.json":
            raise Crash()

    monkeypatch.setattr(operations, "atomic_write_bytes", interrupted)
    with pytest.raises(Crash):
        service(tmp_path).convert(conversion)
    monkeypatch.setattr(operations, "atomic_write_bytes", write)
    restarted = service(tmp_path)
    operation = restarted.transactions()[0]
    assert operation.state == "prepared"
    assert restarted.rollback(operation.id).state == "rolled_back"
    assert list(conversion.target_roots.store.rglob("*.jsonl")) == []


def test_failed_atomic_publication_never_streams_to_final_filename(tmp_path, monkeypatch):
    from ringleader.portability import service as service_module

    conversion = request(tmp_path)
    real_fsync = service_module.os.fsync

    def crash_on_bytes(fd):
        real_fsync(fd)
        stat = os.fstat(fd)
        if stat.st_size > 1000:
            raise Crash()

    worker = service(tmp_path)

    def hook(point, operation):
        if point == "before:prepare:0:action":
            monkeypatch.setattr(service_module.os, "fsync", crash_on_bytes)

    worker.fault = hook
    with pytest.raises(Crash):
        worker.convert(conversion)
    monkeypatch.setattr(service_module.os, "fsync", real_fsync)
    assert list(conversion.target_roots.store.rglob("*.jsonl")) == []
    restarted = service(tmp_path)
    assert restarted.resume(restarted.transactions()[0].id).lifecycle == "active"


def test_attachment_and_branch_are_part_of_identity(tmp_path):
    conversion = request(tmp_path)
    worker = service(tmp_path)
    initial = worker.convert(conversion)
    path = conversion.source.roots.store / conversion.source.source_ref
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    first_user = next(row for row in rows if row.get("type") == "message")
    branch = worker.convert(
        replace(conversion, source=replace(conversion.source, branch_id=first_user["id"]))
    )
    assert branch.target != initial.target
    image = next(block for block in first_user["message"]["content"] if block["type"] == "image")
    image["data"] = "bmV3IGltYWdlIGJ5dGVz"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert worker.convert(conversion).target not in (initial.target, branch.target)


def test_rollback_crash_is_idempotent(tmp_path):
    conversion = request(tmp_path)
    restarted, operation, _staged = operation_after_crash(
        tmp_path, conversion, "before:file:0:intent"
    )
    restarted.fault = crash_at("after:rollback")
    with pytest.raises(Crash):
        restarted.rollback(operation.id)
    assert service(tmp_path).rollback(operation.id).state == "rolled_back"


@pytest.mark.parametrize("field", ["identity", "cwd", "title"])
def test_picker_metadata_mismatch_fails_before_journal(tmp_path, field):
    conversion = request(tmp_path)
    adapters = builtin_adapters()

    class WrongMetadata(PiAdapter):
        def stage_metadata(self, stage):
            value = super().stage_metadata(stage)
            wrong = {"identity": Identity("pi", "wrong"), "cwd": "/wrong", "title": "Wrong title"}
            return replace(value, **{field: wrong[field]})

    adapter = WrongMetadata()
    adapters._adapters["pi"] = Adapter("pi", adapter, adapter, adapter)
    before = tree(tmp_path)
    worker = service(tmp_path, adapters=adapters)
    with pytest.raises(PreservationError, match="picker metadata"):
        worker.convert(conversion)
    assert tree(tmp_path) == before
    assert worker.transactions() == ()


def test_native_codex_archive_moves_path_and_preserves_lifecycle(tmp_path):
    conversion = request(tmp_path, "codex", target=initialized(tmp_path / "target"))
    worker = service(tmp_path)
    result = worker.convert(conversion)
    store = conversion.target_roots.store
    old = store / result.source_ref
    archived = store / "archived_sessions" / old.name
    archived.parent.mkdir()
    old.rename(archived)
    with sqlite3.connect(store / "state_5.sqlite") as db:
        db.execute(
            "UPDATE threads SET archived=1,rollout_path=? WHERE id=?",
            (str(archived), result.target.native_id),
        )
    repeated = service(tmp_path).convert(conversion)
    assert (repeated.target, repeated.lifecycle) == (result.target, "archived")
    assert list(store.rglob("*.jsonl")) == [store / "session_index.jsonl", archived]


def test_private_rollback_cleanup_retries_after_receipt_crash(tmp_path):
    conversion = request(tmp_path)
    worker, operation, _staged = operation_after_crash(tmp_path, conversion, "after:create")
    worker.fault = crash_at("after:rollback")
    with pytest.raises(Crash):
        worker.rollback(operation.id)
    assert service(tmp_path).rollback(operation.id).state == "rolled_back"
    assert sorted(path.name for path in worker.store.directory(operation.id).iterdir()) == [
        "journal.json"
    ]


def test_ordinary_failure_is_durable_and_recovery_clears_diagnostic(tmp_path):
    conversion = request(tmp_path)
    worker = service(tmp_path)

    def fail_before_action(point, operation):
        if point == "before:registry:action":
            raise OSError("registry unavailable")

    worker.fault = fail_before_action
    with pytest.raises(OSError, match="registry unavailable"):
        worker.convert(conversion)
    restarted = service(tmp_path)
    operation = restarted.transactions()[0]
    assert (operation.last_error, operation.step("registry").phase) == (
        "registry unavailable",
        "intent",
    )
    assert restarted.resume(operation.id).lifecycle == "active"
    assert restarted.transactions()[0].last_error is None


def test_completed_journal_requires_persisted_index_boundary(tmp_path):
    conversion = request(tmp_path, "codex", target=initialized(tmp_path / "target"))
    worker = service(tmp_path)
    result = worker.convert(conversion)
    operation = worker.store.load(result.operation_id)
    worker.store.save(
        replace(operation, steps=tuple(step for step in operation.steps if step.name != "index"))
    )
    before = tree(conversion.target_roots.store)
    with pytest.raises(PreservationError, match="out of order"):
        service(tmp_path).resume(operation.id)
    assert tree(conversion.target_roots.store) == before


@pytest.mark.parametrize("boundary", ["publisher", "index"])
def test_recovery_fsyncs_native_bytes_interrupted_before_first_data_fsync(
    tmp_path, monkeypatch, boundary
):
    conversion = (
        request(tmp_path, "codex", target=initialized(tmp_path / "target"))
        if boundary == "index"
        else request(tmp_path)
    )
    real_fsync = os.fsync
    affected = []

    def interrupt(fd):
        paths = (
            list(conversion.target_roots.store.rglob("*.pending"))
            if boundary == "publisher"
            else [conversion.target_roots.store / "session_index.jsonl"]
        )
        stat = os.fstat(fd)
        for path in paths:
            if path.exists() and (path.stat().st_dev, path.stat().st_ino) == (
                stat.st_dev,
                stat.st_ino,
            ):
                affected.append((stat.st_dev, stat.st_ino))
                raise Crash()
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", interrupt)
    with pytest.raises(Crash):
        service(tmp_path).convert(conversion)
    assert len(affected) == 1
    synced = []

    def track(fd):
        stat = os.fstat(fd)
        synced.append((stat.st_dev, stat.st_ino))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", track)
    worker = service(tmp_path)
    operation = worker.transactions()[0]

    def check_receipt(point, current):
        receipt = "before:index:done" if boundary == "index" else "before:prepare:0:done"
        if point == receipt:
            assert affected[0] in synced

    worker.fault = check_receipt
    assert worker.resume(operation.id).lifecycle == "active"
    assert affected[0] in synced


@pytest.mark.parametrize("lifecycle", ["archived", "deleted", "purged"])
def test_terminal_registry_lifecycle_before_receipt_reconciles_lineage(tmp_path, lifecycle):
    conversion = request(tmp_path)
    worker, operation, staged = operation_after_crash(
        tmp_path, conversion, "before:registry:intent"
    )
    path = staged.roots.store / staged.source_ref
    archived = tmp_path / "archived.jsonl"

    def scanned_then_retired(reg):
        reg["threads"].append(
            {
                "source": "pi",
                "sessionId": operation.target.native_id,
                "file": str(path),
                "project": str(staged.roots.cwd),
            }
        )
        selector = f"pi:{operation.target.native_id}"
        if lifecycle in ("archived", "purged"):
            registry.archive_session(reg, selector, str(archived), "now")
            if lifecycle == "purged":
                registry.purge_archived_session(reg, selector, "later")
        else:
            registry.prune_session(reg, selector, "now")

    registry.mutate_registry(worker.registry_path, scanned_then_retired)
    if lifecycle == "archived":
        path.rename(archived)
    else:
        path.unlink()
    before = tree(staged.roots.store)
    result = service(tmp_path).resume(operation.id)
    assert result.lifecycle == ("archived" if lifecycle == "archived" else "deleted")
    assert tree(staged.roots.store) == before
    assert service(tmp_path).store.load(operation.id).state == "complete"
    assert service(tmp_path).lineage(operation.target)[0]["operationId"] == operation.id
    reg = registry.load_registry(worker.registry_path)
    assert reg["threads"] == []
    bucket = "archived" if lifecycle == "archived" else "deleted"
    assert reg[bucket][0]["thread"]["lineage"]["operationId"] == operation.id


@pytest.mark.parametrize("point", ["after:retire", "after:registry:action", "after:registry:done"])
def test_native_archive_before_first_registry_receipt_completes_after_crashes(tmp_path, point):
    conversion = request(
        tmp_path, "opencode", target=opencode_roots.__wrapped__(tmp_path / "target")
    )
    worker, operation, staged = operation_after_crash(tmp_path, conversion, "after:database:action")
    with sqlite3.connect(staged.roots.store / "opencode.db") as db:
        db.execute("UPDATE session SET time_archived=1 WHERE id=?", (operation.target.native_id,))
    before = tree(staged.roots.store)
    worker.fault = crash_at(point)
    with pytest.raises(Crash):
        worker.resume(operation.id)
    restarted = service(tmp_path)
    assert restarted.resume(operation.id).lifecycle == "archived"
    assert tree(staged.roots.store) == before
    assert registry.load_registry(worker.registry_path)["threads"] == []
    assert (
        restarted.lineage(operation.source)[0]["target"]["nativeId"] == operation.target.native_id
    )
    assert restarted.store.load(operation.id).state == "complete"


@pytest.mark.parametrize("continued", ["registered", "appended", "both"])
def test_codex_continuation_before_database_intent_finishes_forward(tmp_path, continued):
    conversion = request(tmp_path, "codex", target=initialized(tmp_path / "target"))
    worker, operation, staged = operation_after_crash(tmp_path, conversion, "after:file:0:action")
    adapter = worker.adapters.get("codex")
    if continued in ("registered", "both"):
        adapter.registration.register_stage(staged)
        with sqlite3.connect(staged.roots.store / "state_5.sqlite") as db:
            db.execute(
                "UPDATE threads SET name='Native rename',tokens_used=55 WHERE id=?",
                (operation.target.native_id,),
            )
    path = staged.roots.store / staged.source_ref
    if continued in ("appended", "both"):
        with path.open("ab") as stream:
            stream.write(
                b'{"type":"event_msg","payload":{"type":"agent_message","message":"Continued natively"}}\n'
            )
    before = path.read_bytes()
    assert service(tmp_path).resume(operation.id).lifecycle == "active"
    assert path.read_bytes() == before
    with sqlite3.connect(staged.roots.store / "state_5.sqlite") as db:
        row = db.execute(
            "SELECT name,tokens_used FROM threads WHERE id=?", (operation.target.native_id,)
        ).fetchone()
    assert row == (
        ("Native rename", 55) if continued in ("registered", "both") else ("Imported example", 0)
    )
    assert worker.lineage(operation.target)[0]["operationId"] == operation.id
    assert service(tmp_path).store.load(operation.id).state == "complete"


@pytest.mark.parametrize("finish", ["resume", "rollback"])
def test_owned_partial_private_write_is_recoverable_or_cleaned(tmp_path, finish):
    conversion = request(tmp_path)
    worker = service(tmp_path)

    def partial(point, operation):
        if point == "after:prepare:0:owner":
            staged = worker.store.artifacts(operation).stage.native()
            publisher = worker._publisher(operation, staged.roots.store / staged.source_ref, 0)
            publisher.write_bytes(staged.files[0].data[:71])
            raise Crash()

    worker.fault = partial
    with pytest.raises(Crash):
        worker.convert(conversion)
    restarted = service(tmp_path)
    operation = restarted.transactions()[0]
    assert operation.step("prepare:0").inode > 0
    if finish == "resume":
        assert restarted.resume(operation.id).lifecycle == "active"
    else:
        assert restarted.rollback(operation.id).state == "rolled_back"
        assert list(conversion.target_roots.store.rglob("*.pending")) == []
        assert restarted.convert(conversion).target != operation.target


def test_private_creation_without_inode_receipt_can_be_abandoned_safely(tmp_path, monkeypatch):
    conversion = request(tmp_path)
    real_open = os.open
    leftovers = []

    def interrupt(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if str(path).endswith(".pending"):
            os.write(fd, b"partial bytes before ownership receipt")
            os.close(fd)
            leftovers.append(Path(path))
            raise Crash()
        return fd

    monkeypatch.setattr(os, "open", interrupt)
    with pytest.raises(Crash):
        service(tmp_path).convert(conversion)
    monkeypatch.setattr(os, "open", real_open)
    worker = service(tmp_path)
    operation = worker.transactions()[0]
    assert operation.step("prepare:0").inode == 0
    result = worker.rollback(operation.id)
    assert result.state == "rolled_back"
    assert result.last_error == f"Unverified private artifacts retained: {leftovers[0]}"
    assert leftovers[0].read_bytes() == b"partial bytes before ownership receipt"
    assert worker.rollback(operation.id).last_error == result.last_error
    assert worker.convert(conversion).target != operation.target


def test_complete_private_bytes_before_done_receipt_can_be_rolled_back(tmp_path):
    conversion = request(tmp_path)
    worker, operation, _staged = operation_after_crash(
        tmp_path, conversion, "before:prepare:0:done"
    )
    assert worker.rollback(operation.id).state == "rolled_back"
    assert list(conversion.target_roots.store.rglob("*.pending")) == []


def test_archived_file_before_publication_receipt_recovers_from_owned_inode(tmp_path):
    conversion = request(tmp_path)
    worker, operation, staged = operation_after_crash(tmp_path, conversion, "after:file:0:action")
    path = staged.roots.store / staged.source_ref
    archived = tmp_path / "archive" / "session.jsonl"
    archived.parent.mkdir()
    path.rename(archived)

    def archive(reg):
        reg["threads"].append(
            {"source": "pi", "sessionId": operation.target.native_id, "file": str(path)}
        )
        registry.archive_session(reg, f"pi:{operation.target.native_id}", str(archived), "now")

    registry.mutate_registry(worker.registry_path, archive)
    original = archived.read_bytes()
    assert service(tmp_path).resume(operation.id).lifecycle == "archived"
    assert archived.read_bytes() == original
    assert service(tmp_path).store.load(operation.id).state == "complete"
    assert worker.lineage(operation.target)[0]["operationId"] == operation.id


@pytest.mark.parametrize("restart_after_abandon", [False, True])
def test_codex_unverified_private_creation_recovers_forward_after_index_append(
    tmp_path, monkeypatch, restart_after_abandon
):
    conversion = request(tmp_path, "codex", target=initialized(tmp_path / "target"))
    real_open = os.open
    leftovers = []

    def interrupted(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if str(path).endswith(".pending"):
            os.write(fd, b"private partial")
            os.close(fd)
            leftovers.append(Path(path))
            raise Crash()
        return fd

    monkeypatch.setattr(os, "open", interrupted)
    with pytest.raises(Crash):
        service(tmp_path).convert(conversion)
    monkeypatch.setattr(os, "open", real_open)
    worker = service(tmp_path)
    operation = worker.transactions()[0]
    assert operation.step("index").phase == "done"
    with pytest.raises(PreservationError, match="forward recovery"):
        worker.rollback(operation.id)
    if restart_after_abandon:
        worker.fault = crash_at("after:prepare:0:abandon")
        with pytest.raises(Crash):
            worker.resume(operation.id)
    result = service(tmp_path).resume(operation.id)
    assert (result.lifecycle, result.target) == ("active", operation.target)
    completed = worker.store.load(operation.id)
    assert (completed.state, completed.abandoned_private) == ("complete", (str(leftovers[0]),))
    assert result.diagnostics[-1] == f"Unverified private artifacts retained: {leftovers[0]}"
    assert leftovers[0].read_bytes() == b"private partial"
    index = conversion.target_roots.store / "session_index.jsonl"
    assert len(index.read_text().splitlines()) == 1
    assert len(list(conversion.target_roots.store.rglob("rollout-*.jsonl"))) == 1


def test_malformed_publisher_token_cannot_redirect_rollback(tmp_path):
    conversion = request(tmp_path)
    worker, operation, _staged = operation_after_crash(
        tmp_path, conversion, "after:prepare:0:owner"
    )
    prepared = operation.step("prepare:0")
    worker.store.save(operation.with_step(replace(prepared, publisher_token="../../precious")))
    before = tree(conversion.target_roots.store)
    with pytest.raises(PreservationError, match="publisher generation"):
        worker.rollback(operation.id)
    assert tree(conversion.target_roots.store) == before
