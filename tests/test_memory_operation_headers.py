import json
import os
import shutil
import sqlite3
import weakref
from contextlib import closing
from dataclasses import fields, replace
from datetime import timedelta
from pathlib import Path

import pytest
from ringleader import (
    harvests,
    memory_catalog,
    memory_journal,
    memory_operations,
    memory_scan,
    publish_state,
    registry,
    stateio,
)
from ringleader.memory_lifecycle import prune_memory_recovery
from ringleader.memory_sources.transactions import pack
from ringleader.memory_types import origin_fields
from ringleader.presence import PresenceResult

from memory_helpers import KEY, NOW, STEM, SUMMARY, World, claude_world, codex_world
from test_publish_state import _clean_checkout


def journal(op):
    return memory_operations.root() / op / "journal.json"


def write_legacy(op, path):
    current = memory_operations.load(op, path)
    data = {f.name: getattr(current, f.name) for f in fields(current) if f.init}
    data.update(ref=vars(current.ref), native=vars(current.native))
    del data["physical_key"], data["shared_sequence"]
    journal(op).write_bytes(pack(operation=data))
    for body in journal(op).parent.glob("body-*.json"):
        body.unlink()


@pytest.mark.parametrize("legacy", [False, True])
def test_owner_survives_registry_replacement_and_symlink_alias(tmp_path, legacy):
    world, _ = claude_world(tmp_path)
    saved = world.decide("only this registry approved it", remove=False)
    (item,) = harvests.pending_snapshot(world.path).items
    if legacy:
        write_legacy(saved.operation_id, world.path)
        raw = item.path.read_text()
        legacy_path = item.path.with_name("previous-title-" + item.path.name)
        legacy_path.write_text(
            raw.replace(f"registryOwnerJson: {json.dumps(str(world.path))}\n", "")
        )
        item.path.unlink()
        item = harvests.load_harvest_path(legacy_path)
    replacement = tmp_path / "replacement"
    shutil.copyfile(world.path, replacement)
    replacement.replace(world.path)
    alias = tmp_path / "alias"
    alias.symlink_to(world.path)
    copied = tmp_path / "copy"
    shutil.copyfile(world.path, copied)
    assert [i.token for i in harvests.pending_snapshot(alias).items] == [item.token]
    assert harvests.pending_snapshot(copied).items == ()
    checkout, branch, base = _clean_checkout(tmp_path)
    with pytest.raises(publish_state.PublishStateError, match="unknown"):
        publish_state.assign_memory_harvest(item.token, project=checkout, registry_path=copied)
    assigned = publish_state.assign_memory_harvest(
        item.token, project=checkout, registry_path=alias
    )
    assert assigned.path == item.path
    with pytest.raises(publish_state.PublishStateError, match="unknown"):
        publish_state.begin_publish(
            repo_key=assigned.repo_key,
            checkout=str(checkout),
            branch=branch,
            base_commit=base,
            registry_path=str(copied),
            tokens=[assigned.token],
            bundle_path=tmp_path / "bundle",
        )
    assert world.service.review_removal(world.ref, alias, "harvest", 30).queue == (
        item.content,
        assigned.token,
    )


def test_full_title_survives_queue_and_bounded_header_then_expires(tmp_path):
    world, store = claude_world(tmp_path)
    title = "résumé" * 30000
    (store / "note.md").write_text(f"---\nname: {title}\n---\nNative note")
    world.ref = world.scan(NOW + timedelta(seconds=1))
    out = world.decide("Approved", retention_days=0)
    assert out.state == "removed", out
    (item,) = harvests.pending_snapshot(world.path).items
    assert (item.name, item.path.name) == (title, f"{item.harvest_id}.md")
    path = journal(out.operation_id)
    assert path.stat().st_size < memory_journal.HEADER_LIMIT
    assert [p.name for p in path.parent.iterdir()] == ["journal.json"]
    op = memory_operations.load(out.operation_id, world.path)
    assert (op.purged, op.payload, op.name) == (True, {}, world.ref.memory_id)


def test_queue_body_byte_limit_applies_before_approval_and_native_changes(tmp_path):
    world, store = claude_world(tmp_path)
    limit = "é" * (publish_state.MAX_BYTES // 2)
    over = limit + "x"
    refused = world.decide(over)
    assert (refused.state, refused.operation_id) == ("refused", None)
    assert str(publish_state.MAX_BYTES) in refused.message
    saved = world.decide(limit, remove=False)
    assert saved.state == "harvested", saved
    (item,) = harvests.pending_snapshot(world.path).items
    before = journal(saved.operation_id).read_bytes()
    assert world.decide(over, expected=item.token).state == "refused"
    assert journal(saved.operation_id).read_bytes() == before
    item.path.write_bytes(item.path.read_bytes() + b"x")
    assert harvests.pending_snapshot(world.path).items == ()
    assert world.decide(expected=harvests.load_harvest_path(item.path).token).state == "refused"
    assert (store / "note.md").read_bytes() == b"native private text\r\n"


@pytest.mark.parametrize(
    "options", [{"remove": False}, {"markdown": "draft"}, {"expected": "token"}, {"direct": []}]
)
def test_reject_inapplicable_discard_arguments(tmp_path, options):
    world, _ = claude_world(tmp_path)
    outcome = world.decide(decision="discard", **options)
    assert (outcome.state, outcome.operation_id, memory_operations.operations(world.path)) == (
        "refused",
        None,
        [],
    )


def test_return_only_durable_operation_ids(tmp_path, monkeypatch):
    world, _ = claude_world(tmp_path)
    missing = world.decide("draft", direct=[tmp_path / "missing"])
    assert (missing.state, missing.operation_id) == ("refused", None)

    def interrupted(**_):
        raise OSError("Unable to write approved queue item")

    monkeypatch.setattr(harvests, "write_harvest_unlocked", interrupted)
    failed = world.decide("draft")
    assert failed.state == "failed"
    assert memory_operations.load(failed.operation_id, world.path).payload["draft"] == "draft"


def test_prepared_codex_intent_rechecks_observed_review_incarnation(tmp_path, monkeypatch):
    world, _, db_path = codex_world(tmp_path)
    save = memory_operations.Operation.write

    def pause(op):
        save(op)
        if "intent" in op.payload and not op.progress:
            raise InterruptedError("Stopped after intent")

    with monkeypatch.context() as mp:
        mp.setattr(memory_operations.Operation, "write", pause)
        out = world.decide(decision="discard")
    assert out.state == "failed"
    with closing(sqlite3.connect(db_path)) as db:
        row = db.execute("SELECT * FROM stage1_outputs WHERE thread_id=?", (KEY,)).fetchone()
        db.execute("DELETE FROM stage1_outputs WHERE thread_id=?", (KEY,))
        db.commit()
        reg = registry.load_registry(world.path)
        missing = memory_scan.scan([world.source])
        registry.commit_registry(
            world.path,
            registry.reconcile_native_memory(
                reg, missing, (NOW + timedelta(seconds=1)).isoformat()
            ),
        )
        db.execute("INSERT INTO stage1_outputs VALUES (?,?,?,?,?,?,?,?,?,?)", row)
        db.commit()
    recreated = world.scan(NOW + timedelta(seconds=2))
    assert recreated.incarnation != world.ref.incarnation
    result = world.recover(out.operation_id)
    assert result.state == "refused" and "incarnation" in result.message
    with closing(sqlite3.connect(db_path)) as db:
        assert (
            db.execute("SELECT * FROM stage1_outputs WHERE thread_id=?", (KEY,)).fetchone() == row
        )


def test_cross_registry_supersession_and_terminal_payload_compaction(tmp_path):
    first, _, db_path = codex_world(tmp_path)
    first.presence = replace(first.quiet, sessions=PresenceResult(available=False))
    pending = first.decide(decision="discard")
    assert pending.state == "removal-pending"
    second_root = tmp_path / "second"
    second_root.mkdir()
    second = World(second_root, first.source, first.selector)
    removed = second.decide(decision="discard")
    assert removed.state == "removed"
    assert set(memory_operations.load(removed.operation_id, second.path).payload) == {"snapshot"}
    assert second.recover(removed.operation_id, restore=True).state == "restored"
    first.presence = first.quiet
    assert first.recover(pending.operation_id).state == "refused"
    listing = memory_operations.recovery_listing(first.path, NOW)
    assert [(r["operationId"], r["supersededBy"]) for r in listing.rows] == [
        (pending.operation_id, removed.operation_id)
    ]
    with closing(sqlite3.connect(db_path)) as db:
        assert db.execute(
            "SELECT raw_memory FROM stage1_outputs WHERE thread_id=?", (KEY,)
        ).fetchone() == ("source text",)

    write_legacy(removed.operation_id, second.path)
    write_legacy(pending.operation_id, first.path)
    result = first.recover(pending.operation_id)
    assert result.state == "refused" and "Cannot order legacy decisions" in result.message


def test_listing_reads_each_header_once_and_isolates_damaged_body(tmp_path, monkeypatch):
    world, store = claude_world(tmp_path)
    removed = world.decide(decision="discard")
    path = journal(removed.operation_id)
    (body,) = path.parent.glob("body-*.json")
    stamp = body.stat()
    raw = body.read_bytes()
    damaged = raw.replace(b"native private text", b"native amended text")
    assert damaged != raw and len(damaged) == len(raw)
    assert json.loads(damaged)["payload"]["snapshot"] != json.loads(raw)["payload"]["snapshot"]
    body.write_bytes(damaged)
    os.utime(body, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2_000_000_000))
    (path.parent.parent / ".DS_Store").write_bytes(b"metadata")
    second = next(
        row
        for row in registry.load_registry(world.path)["memoryEntries"]
        if row["path"] == str(store / "other.md")
    )
    result = world.decide(
        "another approved note", ref=memory_catalog.reviewed_ref(second), remove=False
    )
    assert result.state == "harvested", result
    seen = []
    read = memory_journal.read_header

    def count(path):
        seen.append(path.name)
        return read(path)

    def unexpected(*_):
        pytest.fail("Listing decoded raw content")

    monkeypatch.setattr(memory_journal, "read_header", count)
    with monkeypatch.context() as listing_check:
        listing_check.setattr(memory_journal, "read_body", unexpected)
        listing = memory_operations.recovery_listing(world.path, NOW)
    assert set(seen) == {removed.operation_id, result.operation_id} and len(seen) == 2
    (row,) = listing.rows
    assert (row["operationId"], row["restorable"], row["recoveryAvailable"]) == (
        removed.operation_id,
        False,
        False,
    )
    assert "digest changed" in row["diagnostic"]
    os.utime(body, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    restored = world.recover(removed.operation_id, restore=True)
    assert restored.state == "refused" and "digest changed" in restored.message
    assert {file.name for file in store.glob("*.md")} == {"MEMORY.md", "other.md"}
    later = NOW + timedelta(days=31)
    assert prune_memory_recovery(registry_path=world.path, now=later, delete=True) == (
        removed.operation_id,
    )
    assert [p.name for p in path.parent.iterdir()] == ["journal.json"]


def test_interrupted_header_publication_preserves_authoritative_body(tmp_path, monkeypatch):
    world, _ = claude_world(tmp_path)
    save = memory_journal._replace

    def interrupted(path, pending, destination, data):
        if destination == "journal.json":
            raise InterruptedError("Header not published")
        save(path, pending, destination, data)

    with monkeypatch.context() as mp:
        mp.setattr(memory_journal, "_replace", interrupted)
        initial = world.decide("first", remove=False)
    assert (initial.state, initial.operation_id) == ("failed", None)
    (incomplete,) = memory_operations.root().iterdir()
    before = {p.name: p.read_bytes() for p in incomplete.iterdir()}
    assert {"initial.pending", "body-1.json"} <= before.keys()
    assert memory_operations.operations(world.path) == []
    assert {p.name: p.read_bytes() for p in incomplete.iterdir()} == before
    memory_operations.cleanup_checkpoints()
    assert list(memory_operations.root().glob("*/*")) == []
    saved = world.decide("first", remove=False)
    (first,) = harvests.pending_snapshot(world.path).items
    with monkeypatch.context() as mp:
        mp.setattr(memory_journal, "_replace", interrupted)
        failed = world.decide("updated", remove=False, expected=first.token)
    assert (failed.state, failed.operation_id) == ("failed", saved.operation_id)
    memory_operations.cleanup_checkpoints()
    assert world.service.review_removal(world.ref, world.path, "harvest", 30).queue == (
        "first",
        first.token,
    )
    assert memory_operations.load(saved.operation_id, world.path).approval_hash == first.body_digest


def test_legacy_unknown_lineage_fails_closed(tmp_path):
    world, store = claude_world(tmp_path)
    broken = journal("a" * 32)
    broken.parent.mkdir(parents=True)
    broken.write_text("{")
    result = world.decide(decision="discard")
    assert result.state == "refused" and "restore the journal from backup" in result.message
    assert (store / "note.md").read_bytes() == b"native private text\r\n"


@pytest.mark.parametrize("proof", ["matching", "changed", "missing"])
def test_existing_foreign_receipt_requires_owner_origin_and_body_and_retries(
    tmp_path, monkeypatch, proof
):
    world, _ = claude_world(tmp_path)
    approval = world.decide("Published exactly as approved", remove=False)
    op = memory_operations.load(approval.operation_id, world.path)
    write_legacy(op.id, world.path)
    (item,) = harvests.pending_snapshot(world.path).items
    item.path.write_text(
        item.path.read_text().replace(f"registryOwnerJson: {json.dumps(str(world.path))}\n", "")
    )
    item = harvests.load_harvest_path(item.path)
    checkout, branch, base = _clean_checkout(tmp_path)
    assigned = publish_state.assign_memory_harvest(
        item.token, project=checkout, registry_path=world.path
    )
    receipt = publish_state.begin_publish(
        repo_key=assigned.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base,
        registry_path=str(world.path),
        tokens=[assigned.token],
        bundle_path=tmp_path / "bundle",
    )
    other_registry = tmp_path / "other-registry.json"
    receipt_path = publish_state._receipt_path(receipt.receipt_id)
    historic = json.loads(receipt_path.read_text())
    historic["registryPath"] = str(other_registry)
    if proof == "changed":
        historic["items"][0]["approvedBodyDigest"] = "a" * 64
    elif proof == "missing":
        del historic["items"][0]["approvedBodyDigest"]
    receipt_path.write_text(json.dumps(historic))
    publish_state.prepare_publish(receipt.receipt_id, outcome="no-change")
    if proof != "matching":
        with pytest.raises(publish_state.PublishStateError):
            publish_state.consume_publish(receipt.receipt_id, commit=None)
        assert harvests.load_harvest_path(item.path).token == assigned.token
        assert publish_state.load_receipt(receipt.receipt_id).state == "prepared"
        return

    commit_registry = registry.commit_registry

    def interrupted(path, reg):
        commit_registry(path, reg)
        if str(path) == str(world.path):
            raise InterruptedError("Owner publication proof has landed")

    with monkeypatch.context() as mp, pytest.raises(InterruptedError):
        mp.setattr(registry, "commit_registry", interrupted)
        publish_state.consume_publish(receipt.receipt_id, commit=None)
    assert publish_state.load_receipt(receipt.receipt_id).state == "finishing"
    retry = publish_state.consume_publish(receipt.receipt_id, commit=None)
    assert retry.memory_ids == (world.ref.memory_id,)
    owner = registry.load_registry(world.path)
    assert owner["memoryPublications"][op.harvest_id]["approvedBodyDigest"] == op.approval_hash
    assert owner["memoryDecisions"][op.id]["publicationReceiptId"] == receipt.receipt_id
    assert registry.load_registry(other_registry).get("memoryPublications", {}) == {}
    assert world.decide().state == "removed"


def test_save_takes_one_prewrite_queue_snapshot_and_one_afterward(tmp_path, monkeypatch):
    world, _ = claude_world(tmp_path)
    unrelated = harvests.write_harvest(
        session_id="fixture",
        name="Session",
        folder_path=None,
        repo_url=None,
        harvested_at=NOW.isoformat(),
        content="Session summary",
    )
    loads = []
    original = harvests._load_stamped

    def observe(path):
        loads.append(path)
        return original(path)

    monkeypatch.setattr(harvests, "_load_stamped", observe)
    result = world.decide("Approved local memory", remove=False)
    assert result.state == "harvested", result
    assert loads.count(unrelated) == 2
    assert (
        loads.count(
            unrelated.parent
            / (memory_operations.load(result.operation_id, world.path).harvest_id + ".md")
        )
        == 1
    )


def test_superseded_terminal_retry_uses_header_only_and_purge_reclaims_stranded_bodies(
    tmp_path, monkeypatch
):
    world, _ = claude_world(tmp_path)
    old = world.decide(decision="discard")
    assert world.recover(old.operation_id, restore=True).state == "restored"
    world.ref = world.scan(NOW + timedelta(seconds=1))
    assert world.decide("New approval", remove=False).state == "harvested"
    (body,) = journal(old.operation_id).parent.glob("body-*.json")
    body.write_text("damaged")

    def unexpected(*_):
        pytest.fail("Superseded terminal history decoded raw content")

    monkeypatch.setattr(memory_journal, "read_body", unexpected)
    assert world.recover(old.operation_id).state == "restored"
    later = NOW + timedelta(days=31)
    discard = memory_journal.discard_stranded

    def stop(path, keep):
        if (
            keep is None
            and path == body.parent
            and json.loads(journal(old.operation_id).read_bytes())["operation"]["purged"]
        ):
            raise InterruptedError("Purged header landed before cleanup")
        discard(path, keep)

    with monkeypatch.context() as mp, pytest.raises(InterruptedError):
        mp.setattr(memory_journal, "discard_stranded", stop)
        prune_memory_recovery(registry_path=world.path, now=later, delete=True)
    assert body.exists()
    memory_operations.cleanup_checkpoints()
    assert [p.name for p in body.parent.iterdir()] == ["journal.json"]


def test_projected_legacy_lineage_allows_an_unrelated_adapter_operation(tmp_path):
    first, _ = claude_world(tmp_path)
    out = first.decide(decision="discard")
    second, _, _ = codex_world(tmp_path)
    journal(out.operation_id).write_text("{")
    result = second.decide("Approved Codex record", remove=False)
    assert result.state == "harvested", result
    assert journal(out.operation_id).read_text() == "{"


def test_case_alias_names_the_same_registry_entry_but_new_hardlink_does_not(tmp_path):
    world, _ = claude_world(tmp_path)
    saved = world.decide("Owned draft", remove=False)
    hardlink = world.path.with_name("separate-registry.json")
    hardlink.hardlink_to(world.path)
    assert harvests.pending_snapshot(hardlink).items == ()
    alias = world.path.with_name(world.path.name.upper())
    if alias.exists():
        assert memory_operations.load(saved.operation_id, alias).id == saved.operation_id
        assert (
            world.service.review_removal(world.ref, alias, "harvest", 30).queue[0] == "Owned draft"
        )
        folded_link = hardlink.with_name(hardlink.name.upper())
        assert harvests.pending_snapshot(folded_link).items == ()
    else:
        alias.hardlink_to(world.path)
        assert harvests.pending_snapshot(alias).items == ()


def test_uncheckpointed_native_effect_blocks_foreign_approval_and_original_recovers(
    tmp_path, monkeypatch
):
    first, root, db = codex_world(tmp_path)
    second_root = tmp_path / "second"
    second_root.mkdir()
    second = World(second_root, first.source, first.selector)
    summary = root / "rollout_summaries" / f"{STEM}.md"

    def stop(op, component):
        raise InterruptedError("after file change, before its checkpoint")

    with monkeypatch.context() as fault:
        fault.setattr(memory_operations.Operation, "component", stop)
        pending = first.decide(decision="discard")
    assert pending.state == "failed"
    assert not summary.exists()
    op = memory_operations.load(pending.operation_id, first.path)
    assert "intent" in op.payload and op.progress == []
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute(
            "SELECT raw_memory FROM stage1_outputs WHERE thread_id=?", (KEY,)
        ).fetchone() == ("source text",)
    refused = second.decide("Later foreign approval", remove=False)
    assert (refused.state, refused.operation_id) == ("refused", None)
    assert "unfinished native operation" in refused.message
    assert first.recover(op.id).state == "removed"
    assert first.recover(op.id, restore=True).state == "restored"
    assert summary.read_text() == SUMMARY


@pytest.mark.parametrize("format", ["json", "old-raw", "old-trimmed"])
def test_whitespace_distinct_registry_ownership(tmp_path, format):
    world, _ = claude_world(tmp_path)
    trimmed = world.path
    world.path = trimmed.with_name(trimmed.name + " ")
    world.ref = world.scan()
    assert world.decide("Owner-only approved Markdown", remove=False).state == "harvested"
    (item,) = harvests.pending_snapshot(world.path).items
    if format.startswith("old"):
        spelling = world.path if format == "old-raw" else trimmed
        item.path.write_text(
            item.path.read_text().replace(
                f"registryOwnerJson: {json.dumps(str(world.path))}", f"registryOwner: {spelling}"
            )
        )
    (owned,) = harvests.pending_snapshot(world.path).items
    assert owned.registry_owner == str(world.path)
    assert harvests.pending_snapshot(trimmed).items == ()
    checkout, _, _ = _clean_checkout(tmp_path)
    with pytest.raises(publish_state.PublishStateError, match="unknown"):
        publish_state.assign_memory_harvest(owned.token, project=checkout, registry_path=trimmed)
    assigned = publish_state.assign_memory_harvest(
        owned.token, project=checkout, registry_path=world.path
    )
    assert harvests.load_harvest_path(assigned.path).registry_owner == str(world.path)


def test_missing_header_quarantines_only_unresolved_queue_owners(tmp_path, monkeypatch):
    world, _ = claude_world(tmp_path)
    world.decide("Local body", remove=False)
    (local,) = harvests.pending_snapshot(world.path).items
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign = World(foreign_root, world.source, world.selector)
    foreign.decide("Foreign body", remove=False)
    (other,) = harvests.pending_snapshot(foreign.path).items
    legacy = harvests.write_harvest(
        origin=local.origin,
        folder_path=None,
        repo_url=None,
        name="old",
        harvested_at=NOW.isoformat(),
        content="Legacy body",
    )
    broken = journal("a" * 32)
    broken.parent.mkdir()
    retained = broken.parent / "body-2.json"
    retained.write_text("retained content")
    checkout, branch, base = _clean_checkout(tmp_path)
    snapshot = harvests.pending_snapshot(world.path)
    assert [i.content for i in snapshot.items] == ["Local body"]
    assert [
        (p.path, "Missing memory operation journal" in p.message) for p in snapshot.problems
    ] == [(legacy, True)]
    with pytest.raises(
        memory_operations.InvalidOperation, match="Missing memory operation journal"
    ):
        memory_operations.recovery_listing(world.path, NOW)
    assert retained.read_text() == "retained content"
    with pytest.raises(publish_state.PublishStateError, match="unknown"):
        publish_state.assign_memory_harvest(other.token, project=checkout, registry_path=world.path)
    with pytest.raises(publish_state.PublishStateError, match="unknown"):
        publish_state.begin_publish(
            repo_key=str(checkout),
            checkout=str(checkout),
            branch=branch,
            base_commit=base,
            registry_path=str(world.path),
            tokens=[other.token],
            bundle_path=tmp_path / "bundle",
        )
    monkeypatch.setattr(
        harvests, "_load_stamped", lambda path: pytest.fail(f"unchanged queue item reread: {path}")
    )
    assert harvests.pending_snapshot(foreign.path, snapshot).items == (other,)
    reg = registry.load_registry(world.path)
    reg["memoryPublications"] = {local.harvest_id: {}}
    registry.commit_registry(world.path, reg)
    assert harvests.pending_snapshot(world.path, snapshot).items == ()


def _header_receipt(tmp_path, *, foreign_registry=True):
    world, _ = claude_world(tmp_path)
    saved = world.decide("Owner's approved knowledge", remove=False)
    (item,) = harvests.pending_snapshot(world.path).items
    checkout, branch, base = _clean_checkout(tmp_path)
    assigned = publish_state.assign_memory_harvest(
        item.token, project=checkout, registry_path=world.path
    )
    receipt = publish_state.begin_publish(
        repo_key=assigned.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base,
        registry_path=str(world.path),
        tokens=[assigned.token],
        bundle_path=tmp_path / "bundle",
    )
    foreign = tmp_path / "foreign.json"
    path = publish_state._receipt_path(receipt.receipt_id)
    data = json.loads(path.read_text())
    if foreign_registry:
        data["registryPath"] = str(foreign)
    path.write_text(json.dumps(data))
    publish_state.prepare_publish(receipt.receipt_id, outcome="no-change")

    return world, assigned, receipt, foreign, path, journal(saved.operation_id).parent


def _observe_memory_unlinks(monkeypatch, assigned, path, owner):
    unlink = Path.unlink
    observed = []

    def verify_before_unlink(target, *args, **kwargs):
        if target == assigned.path:
            data = json.loads(path.read_bytes())
            assert data["state"] == "finishing"
            assert data["items"] == [
                {
                    "harvestId": assigned.harvest_id,
                    "digest": assigned.digest,
                    "relativePath": assigned.path.name,
                    "token": assigned.token,
                    **origin_fields(assigned.origin),
                    "approvedBodyDigest": assigned.body_digest,
                    "publicationOwner": str(owner),
                }
            ]
            observed.append(assigned.token)
        return unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", verify_before_unlink)
    return observed


def _interrupt_after_unlink(monkeypatch, assigned, receipt, path, directory):
    def stop(*_):
        raise InterruptedError("before registry projection")

    with monkeypatch.context() as fault, pytest.raises(InterruptedError):
        fault.setattr(registry, "commit_registry", stop)
        publish_state.consume_publish(receipt.receipt_id, commit=None)
    assert not assigned.path.exists()
    assert [p.name for p in directory.iterdir()] == ["journal.json"]
    finishing = json.loads(path.read_text())
    return finishing


def test_unsynced_finishing_receipt_retries_sync_before_unlink(tmp_path, monkeypatch):
    world, assigned, receipt, _, path, _ = _header_receipt(tmp_path)
    observed = _observe_memory_unlinks(monkeypatch, assigned, path, world.path)
    sync = stateio.sync_directory

    def unavailable(directory, **kwargs):
        if directory == path.parent:
            raise InterruptedError("receipt directory sync")
        sync(directory, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(stateio, "sync_directory", unavailable)
        for _ in range(2):
            with pytest.raises(InterruptedError, match="receipt directory sync"):
                publish_state.consume_publish(receipt.receipt_id, commit=None)
            assert publish_state.load_receipt(receipt.receipt_id).state == "finishing"
            assert assigned.path.is_file()
            assert observed == []
    publish_state.consume_publish(receipt.receipt_id, commit=None)
    assert observed == [assigned.token]


def test_queue_owner_survives_loss_of_header(tmp_path, monkeypatch):
    world, assigned, receipt, _, path, directory = _header_receipt(tmp_path, foreign_registry=False)
    observed = _observe_memory_unlinks(monkeypatch, assigned, path, world.path)
    (directory / "journal.json").unlink()
    publish_state.consume_publish(receipt.receipt_id, commit=None)
    assert (
        registry.load_registry(world.path)["memoryPublications"][assigned.harvest_id]["receiptId"]
        == receipt.receipt_id
    )
    assert observed == [assigned.token]


def test_redirected_finishing_owner_requires_recovery(tmp_path, monkeypatch):
    world, assigned, receipt, foreign, path, directory = _header_receipt(tmp_path)
    observed = _observe_memory_unlinks(monkeypatch, assigned, path, world.path)
    finishing = _interrupt_after_unlink(monkeypatch, assigned, receipt, path, directory)
    assert observed == [assigned.token]
    (directory / "journal.json").unlink()
    owner_backup = world.path.with_suffix(".backup")
    world.path.rename(owner_backup)
    world.path.symlink_to(foreign)
    before = owner_backup.read_bytes()
    with pytest.raises(publish_state.PublishStateError, match="restore its original path"):
        publish_state.consume_publish(receipt.receipt_id, commit=None)
    assert json.loads(path.read_text()) == finishing
    assert owner_backup.read_bytes() == before
    assert not foreign.exists()


def test_old_matching_foreign_projection_cannot_prove_owner(tmp_path, monkeypatch):
    world, assigned, receipt, foreign, path, directory = _header_receipt(tmp_path)
    observed = _observe_memory_unlinks(monkeypatch, assigned, path, world.path)
    finishing = _interrupt_after_unlink(monkeypatch, assigned, receipt, path, directory)
    assert observed == [assigned.token]
    projection = registry.load_registry(foreign)
    publish_state._record_memory_publications(
        projection, publish_state.load_receipt(receipt.receipt_id)
    )
    registry.commit_registry(foreign, projection)
    finishing["items"][0].pop("publicationOwner")
    path.write_text(json.dumps(finishing))
    shutil.rmtree(directory)
    projections = (world.path.read_bytes(), foreign.read_bytes())
    with pytest.raises(publish_state.PublishStateError, match="restore its lifecycle journal"):
        publish_state.consume_publish(receipt.receipt_id, commit=None)
    assert json.loads(path.read_text()) == finishing
    assert (world.path.read_bytes(), foreign.read_bytes()) == projections


@pytest.mark.parametrize("checkpoint", ["bound", "legacy", "absent", "conflict"])
def test_header_loss_after_queue_unlink_uses_only_proven_owner(tmp_path, monkeypatch, checkpoint):
    world, assigned, receipt, foreign, path, directory = _header_receipt(tmp_path)
    observed = _observe_memory_unlinks(monkeypatch, assigned, path, world.path)
    finishing = _interrupt_after_unlink(monkeypatch, assigned, receipt, path, directory)
    assert observed == [assigned.token]
    if checkpoint == "bound":
        assert finishing["items"][0]["publicationOwner"] == str(world.path)
        (directory / "journal.json").unlink()
        publish_state.consume_publish(receipt.receipt_id, commit=None)
        evidence = registry.load_registry(world.path)["memoryPublications"][assigned.harvest_id]
        assert (evidence["approvedBodyDigest"], evidence["receiptId"]) == (
            assigned.body_digest,
            receipt.receipt_id,
        )
    else:
        if checkpoint in ("legacy", "absent"):
            finishing["items"][0].pop("publicationOwner", None)
            (directory / "journal.json").unlink()
            if checkpoint == "absent":
                directory.rmdir()
        else:
            finishing["items"][0]["publicationOwner"] = str(foreign)
        path.write_text(json.dumps(finishing))
        with pytest.raises(
            publish_state.PublishStateError,
            match=r"Missing memory operation journal|conflicting registry ownership|restore its lifecycle journal",
        ):
            publish_state.consume_publish(receipt.receipt_id, commit=None)
        assert publish_state.load_receipt(receipt.receipt_id).state == "finishing"
    assert registry.load_registry(foreign).get("memoryPublications", {}) == {}


def test_exact_old_owner_literal_can_be_read_without_journal(tmp_path):
    world, _ = claude_world(tmp_path)
    plain = world.path
    world.path = plain.with_name(plain.name + " ")
    world.ref = world.scan()
    saved = world.decide("Old exact", remove=False)
    (item,) = harvests.pending_snapshot(world.path).items
    item.path.write_text(
        item.path.read_text().replace(
            f"registryOwnerJson: {json.dumps(str(world.path))}", f"registryOwner: {world.path}"
        )
    )
    shutil.rmtree(journal(saved.operation_id).parent)
    assert harvests.pending_snapshot(world.path).items[0].registry_owner == str(world.path)
    assert harvests.pending_snapshot(plain).items == ()
    item.path.write_text(
        item.path.read_text().replace(f"registryOwner: {world.path}\n", f"registryOwner: {plain}\n")
    )
    blocked = harvests.pending_snapshot(plain)
    assert blocked.items == ()
    assert "no matching lifecycle approval" in blocked.problems[0].message


def test_unfinished_restore_blocks_approval_after_row_reappears(tmp_path, monkeypatch):
    first, root, _ = codex_world(tmp_path)
    removed = first.decide(decision="discard")
    checkpoint = memory_operations.Operation.component

    def pause(op, component):
        if op.restoring and component == "database":
            raise InterruptedError("restored row committed before journal checkpoint")
        checkpoint(op, component)

    with monkeypatch.context() as fault:
        fault.setattr(memory_operations.Operation, "component", pause)
        incomplete = first.recover(removed.operation_id, restore=True)
    assert incomplete.state == "failed"
    second_root = tmp_path / "second"
    second_root.mkdir()
    second = World(second_root, first.source, first.selector)
    refused = second.decide("Foreign approval during restore", remove=False)
    assert refused.state == "refused" and "unfinished native operation" in refused.message
    assert first.recover(removed.operation_id).state == "restored"
    assert (root / "rollout_summaries" / f"{STEM}.md").read_text() == SUMMARY


def test_legacy_owner_edit_replays_with_the_exact_reviewed_token(tmp_path, monkeypatch):
    world, _ = claude_world(tmp_path)
    trimmed = world.path
    world.path = trimmed.with_name(trimmed.name + " ")
    world.ref = world.scan()
    saved = world.decide("First approved text", remove=False)
    (item,) = harvests.pending_snapshot(world.path).items
    item.path.write_text(
        item.path.read_text().replace(
            f"registryOwnerJson: {json.dumps(str(world.path))}", f"registryOwner: {trimmed}"
        )
    )
    (old,) = harvests.pending_snapshot(world.path).items

    def interrupt(*args, **kwargs):
        raise InterruptedError("before queue replacement")

    with monkeypatch.context() as fault:
        fault.setattr(harvests, "rewrite_memory_harvest_unlocked", interrupt)
        interrupted = world.decide("Edited approval", expected=old.token, remove=False)
    assert interrupted.state == "failed"
    assert world.recover(saved.operation_id).state == "harvested"
    (updated,) = harvests.pending_snapshot(world.path).items
    assert (updated.path, updated.registry_owner, updated.content) == (
        old.path,
        str(world.path),
        "Edited approval",
    )
    assert harvests.pending_snapshot(trimmed).items == ()


@pytest.mark.parametrize(
    "replacement", ['"relative"', "null", '""', '"unfinished', '"a"\nregistryOwner: /other']
)
def test_invalid_owner_field_quarantines_body(tmp_path, replacement):
    world, _ = claude_world(tmp_path)
    world.decide("Do not expose", remove=False)
    (item,) = harvests.pending_snapshot(world.path).items
    item.path.write_text(item.path.read_text().replace(json.dumps(str(world.path)), replacement))
    snapshot = harvests.pending_snapshot(world.path)
    assert snapshot.items == ()
    assert [p.path for p in snapshot.problems] == [item.path]


@pytest.mark.parametrize("char", ["x", "é"])
def test_oversized_projection_reuses_observation_and_rechecks_size_on_change(
    tmp_path, monkeypatch, char
):
    world, _ = claude_world(tmp_path)
    world.decide("Approved", remove=False)
    (item,) = harvests.pending_snapshot(world.path).items
    raw = item.path.read_bytes()
    suffix = char * (publish_state.MAX_BYTES // len(char.encode()))
    item.path.write_bytes(raw + suffix.encode())
    first = harvests.pending_snapshot(world.path)
    import gc

    seen = set()
    todo = [first._observations]
    while todo:
        obj = todo.pop()
        if id(obj) in seen or isinstance(obj, type):
            continue
        seen.add(id(obj))
        if isinstance(obj, str | bytes):
            assert len(obj) < len(suffix)
        elif (
            isinstance(obj, (dict, list, tuple, harvests.Harvest))
            or type(obj).__module__ == harvests.__name__
        ):
            todo.extend(gc.get_referents(obj))
    with monkeypatch.context() as probe:
        probe.setattr(harvests, "_load_stamped", lambda _: pytest.fail("reread unchanged file"))
        probe.setattr(
            harvests, "_compact_pending", lambda _: pytest.fail("re-encoded unchanged body")
        )
        second = harvests.pending_snapshot(world.path, first)
    (complete,) = harvests.snapshot_harvests(second).items
    assert complete.content == item.content + suffix
    assert first.items == second.items == ()
    assert [(p.path, "exceeds" in p.message) for p in second.problems] == [(item.path, True)]
    item.path.write_bytes(raw)
    assert harvests.pending_snapshot(world.path, second).items == (item,)


@pytest.mark.parametrize("kinds", [("json", "json"), ("plain", "json"), ("json", "plain")])
def test_two_valid_owner_fields_quarantine_both_registry_projections(tmp_path, kinds):
    world, _ = claude_world(tmp_path)
    world.decide("Exact owner", remove=False)
    (item,) = harvests.pending_snapshot(world.path).items
    other = tmp_path / "other.json"
    registry.save_registry(other, registry.empty_registry())
    claims = [
        "registryOwnerJson: " + json.dumps(str(path))
        if kind == "json"
        else "registryOwner: " + str(path)
        for kind, path in zip(kinds, (world.path, other), strict=True)
    ]
    item.path.write_text(
        item.path.read_text().replace(
            "registryOwnerJson: " + json.dumps(str(world.path)), "\n".join(claims)
        )
    )
    for path in (world.path, other):
        snapshot = harvests.pending_snapshot(path)
        assert snapshot.items == ()
        assert [(problem.path, problem.message) for problem in snapshot.problems] == [
            (item.path, "invalid memory harvest registry owner")
        ]


@pytest.mark.parametrize(
    "interruption",
    ["empty-stage", "pending-marker", "stage-sync", "staged-marker", "installed-marker"],
)
def test_initial_marker_is_installed_with_operation_directory(tmp_path, monkeypatch, interruption):
    world, _ = claude_world(tmp_path)
    root = memory_operations.root()
    replace = memory_journal._replace
    rename = Path.rename
    sync = memory_journal.sync_directory

    def interrupt_replace(path, pending, dest, raw):
        if dest == "initial.pending" and interruption in ("empty-stage", "pending-marker"):
            if interruption == "pending-marker":
                (path / pending).touch(mode=0o600)
            raise InterruptedError("marker not installed")
        return replace(path, pending, dest, raw)

    def interrupt_rename(path, target):
        if path.name.startswith(".initial-") and interruption == "staged-marker":
            raise InterruptedError("cannot install operation")
        return rename(path, target)

    def interrupt_sync(directory):
        if interruption == "stage-sync" and directory.name.startswith(".initial-"):
            assert (directory / "initial.pending").read_bytes() == b""
            assert not (root / directory.name.removeprefix(".initial-")).exists()
            assert directory.stat().st_mode & 0o777 == 0o700
            raise InterruptedError("stage marker not synced")
        if directory == root and interruption == "installed-marker":
            raise InterruptedError("operation not synced")
        return sync(directory)

    with monkeypatch.context() as fault:
        fault.setattr(memory_journal, "_replace", interrupt_replace)
        fault.setattr(Path, "rename", interrupt_rename)
        fault.setattr(memory_journal, "sync_directory", interrupt_sync)
        outcome = world.decide("unfinished", remove=False)
        assert (outcome.state, outcome.operation_id) == ("failed", None)
    (partial,) = root.iterdir()
    if interruption == "installed-marker":
        assert memory_journal.OPERATION_ID.fullmatch(partial.name)
        assert (partial / "initial.pending").read_bytes() == b""
    else:
        assert partial.name.startswith(".initial-")
    assert memory_operations.history() == []
    saved = world.decide("new approval", remove=False)
    assert saved.state == "harvested"
    for _ in range(2):
        memory_operations.cleanup_checkpoints()
        assert [path.name for path in root.iterdir()] == [saved.operation_id]


@pytest.mark.parametrize("ancestor", ["state", "memory", "operations"])
def test_abandoned_cleanup_checks_ancestors(tmp_path, ancestor):
    world, _ = claude_world(tmp_path)
    saved = world.decide("approved", remove=False)
    root = memory_operations.root()
    abandoned = root / (".abandoned-" + "b" * 32)
    abandoned.mkdir(mode=0o700)
    marker = abandoned / "initial.pending"
    marker.touch(mode=0o600)
    redirected = {"state": root.parent.parent, "memory": root.parent, "operations": root}[ancestor]
    outside = tmp_path / "moved"
    redirected.rename(outside)
    redirected.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        memory_operations.cleanup_checkpoints()
    assert marker.read_bytes() == b""
    redirected.unlink()
    outside.rename(redirected)
    memory_operations.cleanup_checkpoints()
    assert [p.name for p in root.iterdir()] == [saved.operation_id]


def test_stage_cleanup_refuses_unrecognized_contents_and_final_directory(tmp_path):
    world, _ = claude_world(tmp_path)
    world.decide("approved", remove=False)
    root = memory_operations.root()
    stage = root / (".initial-" + "c" * 32)
    stage.mkdir(mode=0o700)
    unexpected = stage / "journal.pending"
    unexpected.write_bytes(b"preserve these bytes")
    with pytest.raises(ValueError, match="Unrecognized"):
        memory_operations.cleanup_checkpoints()
    assert unexpected.read_bytes() == b"preserve these bytes"
    unexpected.unlink()
    stage.rmdir()
    unknown = root / ("d" * 32)
    unknown.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="restore its header"):
        memory_operations.cleanup_checkpoints()
    assert unknown.is_dir()


def test_selected_owner_lookup_releases_unrelated_headers_while_walking(tmp_path, monkeypatch):
    world, _ = claude_world(tmp_path)
    saved = world.decide("approved", remove=False)
    base = memory_operations.load(saved.operation_id, world.path)
    root = memory_operations.root()
    names = [f"{n:032x}" for n in range(8)]
    for name in names:
        path = root / name
        path.mkdir(mode=0o700)
        (path / "journal.json").touch()
    seen = []

    def headers(path, **kwargs):
        if path.name == base.id:
            return base
        assert sum(ref() is not None for ref in seen) <= 1
        op = replace(base, id=path.name, harvest_id=path.name)
        seen.append(weakref.ref(op))
        return op

    monkeypatch.setattr(memory_operations, "_read_operation", headers)
    assert memory_operations.harvest_operations({base.harvest_id}) == {base.harvest_id: base}
    assert len(seen) == len(names)
