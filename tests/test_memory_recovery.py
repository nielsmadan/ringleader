import os
from dataclasses import replace
from datetime import timedelta

import pytest
from ringleader import harvests, memory_operations, publish_state, registry
from ringleader.memory_sources import transactions
from ringleader.presence import PresenceResult
from ringleader.stateio import publish_lock

from memory_helpers import NOW, claude_world, set_file_identity
from test_publish_state import _clean_checkout


@pytest.mark.parametrize("strength", ["birth", "inode"])
@pytest.mark.parametrize("phase", ["approved", "queued", "snapshot", "intent", "project", "done"])
def test_interruption_through_service_journal(tmp_path, monkeypatch, phase, strength):
    set_file_identity(monkeypatch, strength)
    w, store = claude_world(tmp_path)
    original = memory_operations.Operation.write
    project = memory_operations.Operation.project_registry
    queue = harvests.write_harvest_unlocked

    def stop_write(op):
        original(op)
        reached = {
            "approved": op.revision == 1,
            "snapshot": "snapshot" in op.payload,
            "intent": "intent" in op.payload,
            "done": op.done,
        }
        if reached.get(phase):
            raise InterruptedError("simulated crash after durable journal")

    def stop_queue(**kwargs):
        result = queue(**kwargs)
        if phase == "queued":
            raise InterruptedError("simulated crash after durable harvest")
        return result

    def stop_project(op, path):
        if phase == "project" and op.source_state == "removed":
            raise InterruptedError("simulated crash before registry projection")
        project(op, path)

    with monkeypatch.context() as mp:
        mp.setattr(memory_operations.Operation, "write", stop_write)
        mp.setattr(memory_operations.Operation, "project_registry", stop_project)
        mp.setattr(harvests, "write_harvest_unlocked", stop_queue)
        out = w.decide("exact approved text", retention_days=0)
        assert out.state == "failed"
    if phase in ("approved", "queued", "snapshot", "intent"):
        assert (store / "note.md").read_bytes() == b"native private text\r\n"
    outcome = w.recover(out.operation_id)
    if strength == "inode" and phase in {"approved", "queued", "snapshot", "intent"}:
        assert outcome.state == "refused" and "fresh review" in outcome.message
        outcome = w.decide("exact approved text")
    assert outcome.state == "removed", outcome
    assert memory_operations.load(out.operation_id, w.path).purged
    assert [item.content for item in harvests.snapshot_harvests().items] == ["exact approved text"]


def test_weak_replay_after_exact_file_was_staged(tmp_path, monkeypatch):
    from ringleader.memory_sources import base, claude_code, claude_mutation

    def weak(stat):
        return "inode:" + str(stat.st_ino)

    for module in (base, claude_code, claude_mutation, transactions):
        monkeypatch.setattr(module, "file_identity", weak)
    w, _ = claude_world(tmp_path)
    original = transactions._stage

    def crash(*args):
        original(*args)
        raise InterruptedError("power loss after staging")

    with monkeypatch.context() as mp:
        mp.setattr(transactions, "_stage", crash)
        out = w.decide(decision="discard")
    assert out.state == "failed"
    assert w.recover(out.operation_id).state == "removed"


@pytest.mark.parametrize("interrupt", [False, True])
def test_late_write_to_staged_topic_survives_checkpoint_and_replay(
    tmp_path, monkeypatch, interrupt
):
    w, store = claude_world(tmp_path)
    note = store / "note.md"
    original = memory_operations.Operation.component
    added = b"native writer's unreviewed content"

    with note.open("ab", buffering=0) as writer:

        def checkpoint(op, component):
            original(op, component)
            if component == "topic":
                writer.write(added)
                os.fsync(writer.fileno())
                if interrupt:
                    raise InterruptedError("crash after staged file changed")

        with monkeypatch.context() as mp:
            mp.setattr(memory_operations.Operation, "component", checkpoint)
            result = w.decide("approved knowledge", retention_days=0)

    assert result.state == ("failed" if interrupt else "refused"), result
    replay = w.recover(result.operation_id)
    assert replay.state == "refused" and "Staged native file differs" in replay.message
    (staged,) = store.glob(".ringleader-*.stage")
    assert staged.read_bytes() == b"native private text\r\n" + added
    assert str(staged) in replay.message
    assert memory_operations.load(result.operation_id, w.path).progress == ["topic"]
    assert harvests.snapshot_harvests().items[0].content == "approved knowledge"


def test_merge_distinct_decisions_and_independent_authorities(tmp_path):
    path = tmp_path / "registry.json"
    first, second = (registry.load_registry(path) for _ in range(2))
    first["memoryDecisions"] = {"one": {"sourceState": "present", "lifecycleRevision": 1}}
    second["memoryDecisions"] = {"two": {"sourceState": "present", "lifecycleRevision": 1}}
    registry.commit_registry(path, first)
    registry.commit_registry(path, second)
    publish, native = (registry.load_registry(path) for _ in range(2))
    publish["memoryDecisions"]["one"]["destinationProject"] = "/new"
    native["memoryDecisions"]["one"].update(sourceState="removed", lifecycleRevision=2)
    registry.commit_registry(path, publish)
    registry.commit_registry(path, native)
    assert registry.load_registry(path)["memoryDecisions"] == {
        "one": {"sourceState": "removed", "lifecycleRevision": 2, "destinationProject": "/new"},
        "two": {"sourceState": "present", "lifecycleRevision": 1},
    }
    left, right = (registry.load_registry(path) for _ in range(2))
    left["memoryDecisions"]["one"].update(sourceState="restored", lifecycleRevision=3)
    right["memoryDecisions"]["one"].update(sourceState="present", lifecycleRevision=3)
    registry.commit_registry(path, left)
    with pytest.raises(registry.RegistryConflict):
        registry.commit_registry(path, right)


def test_direct_evidence_journal_recovered_without_a_queue_item(tmp_path, monkeypatch):
    w, store = claude_world(tmp_path)
    doc = tmp_path / "design.md"
    doc.write_text("integrated exact evidence")
    orig = memory_operations.Operation.write

    def crash(op):
        orig(op)
        raise InterruptedError("crash after saving attestation")

    with monkeypatch.context() as mp:
        mp.setattr(memory_operations.Operation, "write", crash)
        out = w.decide("approved", remove=False, direct=[doc])
    assert out.state == "failed" and w.recover(out.operation_id).state == "harvested"
    assert w.decide().state == "removed"
    assert harvests.snapshot_harvests().items == ()
    assert (
        w.recover(out.operation_id, restore=True, now=NOW + timedelta(days=1)).state == "restored"
    )
    assert (store / "note.md").read_bytes() == b"native private text\r\n"


def test_save_only_cannot_execute_an_earlier_pending_removal(tmp_path):
    w, store = claude_world(tmp_path)
    w.presence = replace(w.quiet, sessions=PresenceResult(available=False))
    pending = w.decide("first")
    assert pending.state == "removal-pending"
    (item,) = harvests.snapshot_harvests().items
    w.presence = w.quiet
    retried = w.decide("first", remove=False, expected=item.token)
    assert retried.state == "harvested"
    assert (store / "note.md").read_bytes() == b"native private text\r\n"
    assert w.decide().state == "removed"


def test_repeat_cannot_finish_or_expire_an_interrupted_restore(tmp_path, monkeypatch):
    from ringleader.memory_lifecycle import prune_memory_recovery

    w, store = claude_world(tmp_path)
    old = w.decide(decision="discard", retention_days=1)
    original = memory_operations.Operation.component

    def crash(op, name):
        original(op, name)
        raise InterruptedError("restore interrupted")

    with monkeypatch.context() as mp:
        mp.setattr(memory_operations.Operation, "component", crash)
        assert w.recover(old.operation_id, restore=True).state == "failed"
    assert w.decide(decision="discard").state == "refused"
    assert (
        prune_memory_recovery(registry_path=w.path, now=NOW + timedelta(days=3), delete=True) == ()
    )
    assert w.recover(old.operation_id, now=NOW + timedelta(days=4)).state == "restored"
    assert (store / "note.md").read_bytes() == b"native private text\r\n"


@pytest.mark.parametrize("retry", ["recover", "complete", "resubmit"])
@pytest.mark.parametrize("matching", [True, False])
def test_interrupted_replacement_reconciles_real_assigned_publication(
    tmp_path, monkeypatch, retry, matching
):
    w, store = claude_world(tmp_path)
    saved = w.decide("earlier approval", remove=False)
    (first,) = harvests.snapshot_harvests().items
    rewrite = harvests.rewrite_memory_harvest_unlocked

    def durably_replace_then_crash(*args, **kwargs):
        rewrite(*args, **kwargs)
        raise InterruptedError("crash after replacing draft")

    approved = "\n  approved body\r\n\n"
    with monkeypatch.context() as mp:
        mp.setattr(harvests, "rewrite_memory_harvest_unlocked", durably_replace_then_crash)
        interrupted = w.decide(approved, remove=False, expected=first.token)
    assert interrupted.state == "failed" and interrupted.operation_id == saved.operation_id
    (replacement,) = harvests.snapshot_harvests().items
    assert replacement.content == approved
    decision = registry.load_registry(w.path)["memoryDecisions"][saved.operation_id]
    assert decision["approvalHash"] == first.body_digest
    if not matching:
        with publish_lock():
            replacement = rewrite(
                replacement,
                folder_path=None,
                repo_url=None,
                clone_url=None,
                content="different body",
            )

    checkout, branch, base = _clean_checkout(tmp_path)
    assigned = publish_state.assign_memory_harvest(
        replacement.token, project=checkout, registry_path=w.path
    )
    assert assigned.token != replacement.token and assigned.origin == first.origin
    receipt = publish_state.begin_publish(
        repo_key=assigned.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base,
        registry_path=str(w.path),
        tokens=[assigned.token],
        bundle_path=tmp_path / "bundle",
    )
    publish_state.prepare_publish(receipt.receipt_id, outcome="no-change")
    assert publish_state.consume_publish(receipt.receipt_id, commit=None).memory_ids == (
        w.ref.memory_id,
    )
    evidence = registry.load_registry(w.path)["memoryPublications"][first.harvest_id]
    assert (evidence["approvedBodyDigest"], evidence["token"]) == (
        assigned.body_digest,
        assigned.token,
    )

    if retry == "recover":
        result = w.recover(saved.operation_id)
        if matching:
            assert result.state == "harvested"
            result = w.decide()
    elif retry == "resubmit":
        result = w.decide(approved, expected=first.token)
    else:
        result = w.decide()
    if matching:
        assert result.state == "removed", result
        assert w.decide().operation_id == saved.operation_id
        assert sorted(p.name for p in store.iterdir()) == ["MEMORY.md", "other.md"]
    else:
        assert result.state == "refused" and "approved Markdown" in result.message
        assert (store / "note.md").read_bytes() == b"native private text\r\n"
    assert harvests.snapshot_harvests().items == ()
