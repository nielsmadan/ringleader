import hashlib
import json
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from ringleader import (
    harvests,
    memory_lifecycle,
    memory_operations,
    registry,
)
from ringleader.memory_lifecycle import MemoryLifecycle, prune_memory_recovery
from ringleader.memory_sources import transactions
from ringleader.memory_sources.claude_code import ClaudeMemorySource, ClaudeRoots
from ringleader.presence import (
    PresenceClient,
    PresenceResult,
    PresenceSession,
    RunResult,
)

from memory_helpers import (
    LINK,
    NOW,
    OTHER_LINK,
    World,
    claude_world,
    codex_world,
    set_file_identity,
)


def test_save_edit_complete_and_two_review_cycles(tmp_path):
    w, store = claude_world(tmp_path)
    one = w.decide("first\n", remove=False)
    assert one.state == "harvested"
    (item,) = harvests.snapshot_harvests().items
    assert (item.content, (store / "note.md").read_bytes()) == (
        "first\n",
        b"native private text\r\n",
    )
    assert w.decide("second\n", expected="stale").state == "refused"
    assert w.decide("second\n", expected=item.token, remove=False).state == "harvested"
    (edited,) = harvests.snapshot_harvests().items
    assert (edited.harvest_id, edited.content) == (item.harvest_id, "second\n")
    assert w.decide(expected=item.token).state == "refused"
    assert w.recover(one.operation_id).state == "harvested"
    assert w.decide(expected=edited.token).state == "removed"
    assert {p.name for p in store.iterdir()} == {"MEMORY.md", "other.md"}
    assert (store / "MEMORY.md").read_text() == "# Memory\n" + OTHER_LINK
    (store / "MEMORY.md").write_text("# Memory\n" + OTHER_LINK + "- [New](new.md)\n")
    assert w.recover(one.operation_id, restore=True).state == "restored"
    assert (store / "note.md").read_bytes() == b"native private text\r\n"
    assert (
        store / "MEMORY.md"
    ).read_text() == "# Memory\n" + OTHER_LINK + "- [New](new.md)\n" + LINK
    new_ref = w.scan(NOW + timedelta(seconds=1))
    assert new_ref.fingerprint == w.ref.fingerprint and new_ref.incarnation != w.ref.incarnation
    assert w.decide("stale").state == "refused"
    two = w.decide("new cycle", ref=new_ref)
    assert two.state == "removed" and two.operation_id != one.operation_id


@pytest.mark.parametrize(
    "presence",
    [
        PresenceResult(problems=("offline",)),
        PresenceResult(available=False),
        PresenceResult((PresenceSession("claude", "self", True, "/elsewhere"),)),
    ],
)
def test_unknown_or_active_presence_keeps_durable_approval(tmp_path, presence):
    w, store = claude_world(tmp_path)
    w.presence = replace(w.quiet, sessions=presence)
    result = w.decide("exact approved text", retention_days=0)
    assert (result.state, Path(result.harvest_path).is_file()) == ("removal-pending", True)
    assert (store / "note.md").is_file()
    assert (
        prune_memory_recovery(registry_path=w.path, now=NOW + timedelta(days=100), delete=True)
        == ()
    )
    (store / "note.md").write_text("different revision")
    w.presence = w.quiet
    assert w.recover(result.operation_id).state == "refused"
    assert (store / "note.md").read_text() == "different revision"


@pytest.mark.parametrize("strength", ["birth", "inode"])
@pytest.mark.parametrize(
    ("hook_state", "current", "expected"),
    [
        ("missing", True, "hooks are missing"),
        ("drifted", False, "hooks are drifted"),
        ("wrong-protocol", False, "hooks are unknown"),
        ("installed", True, "current claude-code session is active"),
    ],
)
def test_coverage_and_environment_current_session_guard_empty_presence(
    tmp_path, monkeypatch, hook_state, current, expected, strength
):
    set_file_identity(monkeypatch, strength)
    w, store = claude_world(tmp_path)
    calls = []

    def run(args):
        calls.append(args[1:])
        if args[1] == "sessions":
            return RunResult(0, '{"protocol":1,"sessions":[],"problems":[]}')
        return RunResult(
            0,
            json.dumps(
                {
                    "protocol": 999 if hook_state == "wrong-protocol" else 1,
                    "state": hook_state,
                    "agent": "claude",
                    "path": str(w.source.roots.config / "settings.json"),
                }
            ),
        )

    if current:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "triaging-claude-session")
        monkeypatch.setenv("CODEX_THREAD_ID", "nested-codex-session")
    monkeypatch.setattr(memory_lifecycle, "PresenceClient", lambda: PresenceClient(runner=run))
    w.service = MemoryLifecycle(sources=lambda _: [w.source])
    outcome = w.decide("keep my explicit approval")
    assert outcome.state == "removal-pending" and expected in outcome.message
    assert calls == [["sessions", "--json"], ["hooks", "status", "--agent", "claude", "--json"]]
    assert (
        registry.load_registry(w.path)["memoryDecisions"][outcome.operation_id]["sourceState"]
        == "removal-pending"
    )
    assert (store / "note.md").read_bytes() == b"native private text\r\n"
    assert harvests.snapshot_harvests().items[0].content == "keep my explicit approval"
    w.service.presence = lambda _: w.quiet
    retried = w.recover(outcome.operation_id)
    if strength == "inode":
        assert retried.state == "refused" and "fresh review" in retried.message
        retried = w.decide()
    assert (retried.state, retried.operation_id) == ("removed", outcome.operation_id)


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize(
    "hook_report",
    ["default", "hardlinked", "absent", "relative", "missing-file", "directory", "same", "alias"],
)
def test_custom_config_root_requires_matching_hook_status(
    tmp_path, monkeypatch, agent, hook_report
):

    home = tmp_path / "default-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    if agent == "claude":
        w, store = claude_world(tmp_path)
        active = w.source.roots.config / "settings.json"
        default = home / ".claude/settings.json"
        native_file = store / "note.md"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(active.parent))
    else:
        w, _, native_file = codex_world(tmp_path)
        active = w.source.codex_home / "hooks.json"
        default = home / ".codex/hooks.json"
        monkeypatch.setenv("CODEX_HOME", str(active.parent))
        assert active.parent != native_file.parent
    reported = None
    if hook_report == "default":
        active.unlink()
        default.parent.mkdir()
        default.write_text("{}")
        reported = str(default)
    elif hook_report == "hardlinked":
        default.parent.mkdir()
        default.hardlink_to(active)
        reported = str(default)
    elif hook_report == "alias":
        default.parent.symlink_to(active.parent, target_is_directory=True)
        reported = str(default)
    elif hook_report == "relative":
        reported = active.name
    elif hook_report in {"missing-file", "directory"}:
        active.unlink()
        if hook_report == "directory":
            active.mkdir()
        reported = str(active)
    elif hook_report == "same":
        reported = str(active)
    original = native_file.read_bytes()

    def run(args):
        if args[1] == "sessions":
            return RunResult(0, '{"protocol":1,"sessions":[],"problems":[]}')
        assert args[1:] == ["hooks", "status", "--agent", agent, "--json"]
        payload = {"protocol": 1, "agent": agent, "state": "installed"}
        if reported is not None:
            payload["path"] = reported
        return RunResult(0, json.dumps(payload))

    monkeypatch.setattr(memory_lifecycle, "PresenceClient", lambda: PresenceClient(runner=run))
    w.service = MemoryLifecycle(sources=lambda _: [w.source])
    outcome = w.decide(decision="discard")
    if hook_report in {"same", "alias"}:
        assert outcome.state == "removed", outcome
        (remaining,) = w.source.scan().records
        assert remaining.name in {"other", "Native Slug"} and remaining.ref != w.ref
        return
    assert outcome.state == "removal-pending", outcome
    assert str(active) in outcome.message
    assert (reported if reported and Path(reported).is_absolute() else "no absolute path") in (
        outcome.message
    )
    assert native_file.read_bytes() == original
    decision = registry.load_registry(w.path)["memoryDecisions"][outcome.operation_id]
    assert (decision["decision"], decision["sourceState"]) == ("discard", "removal-pending")


def test_zero_day_expiry_preserves_queue_and_operation_metadata(tmp_path):
    w, _ = claude_world(tmp_path)
    result = w.decide("only the queue owns this", retention_days=0)
    assert result.state == "removed"
    journal = memory_operations.root() / result.operation_id / "journal.json"
    assert (journal.stat().st_mode & 0o777, journal.parent.stat().st_mode & 0o777) == (0o600, 0o700)
    assert json.loads(journal.read_text())["operation"]["payload"] == {}
    assert w.recover(result.operation_id, restore=True).state == "refused"
    assert harvests.snapshot_harvests().items[0].content == "only the queue owns this"
    assert w.decide("only the queue owns this").operation_id == result.operation_id


def test_direct_integration_rechecks_evidence_and_expires_its_markdown(tmp_path):
    w, store = claude_world(tmp_path)
    doc = tmp_path / "design.md"
    doc.write_text("knowledge")
    assert w.decide("approved", remove=False, direct=[tmp_path / "missing.md"]).state == "refused"
    saved = w.decide("approved", remove=False, direct=[doc])
    assert saved.state == "harvested"
    doc.write_text("changed")
    assert w.decide(retention_days=1).state == "refused" and (store / "note.md").is_file()
    doc.write_text("knowledge")
    assert w.decide(retention_days=1).state == "removed"
    later = NOW + timedelta(days=1)
    assert prune_memory_recovery(registry_path=w.path, now=later) == (saved.operation_id,)
    assert (
        memory_operations.load(saved.operation_id, w.path).payload["directMarkdown"] == "approved"
    )
    assert prune_memory_recovery(registry_path=w.path, now=later, delete=True) == (
        saved.operation_id,
    )
    op = memory_operations.load(saved.operation_id, w.path)
    assert (op.payload, op.direct_paths) == (
        {},
        {str(doc): hashlib.sha256(b"knowledge").hexdigest()},
    )


def test_direct_integration_refuses_case_alias_of_selected_note(tmp_path):
    w, store = claude_world(tmp_path)
    actual = store / "note.md"
    alias = tmp_path / "CLAUDE" / actual.relative_to(tmp_path / "claude")
    if not alias.exists():
        pytest.skip("case-insensitive filesystem required")
    assert alias.samefile(actual)
    outcome = w.decide("approved", remove=False, direct=[alias])
    assert outcome.state == "refused"
    if alias.resolve() == alias:
        assert "outside native storage" in outcome.message
    assert memory_operations.operations(w.path) == []
    assert actual.read_bytes() == b"native private text\r\n"


def test_direct_integration_refuses_hardlink_of_selected_note(tmp_path):
    w, store = claude_world(tmp_path)
    actual = store / "note.md"
    doc = tmp_path / "design.md"
    doc.hardlink_to(actual)
    outcome = w.decide("approved", remove=False, direct=[doc])
    assert outcome.state == "refused" and "outside native storage" in outcome.message
    assert memory_operations.operations(w.path) == []
    assert doc.read_bytes() == b"native private text\r\n"


def test_direct_integration_rechecks_other_native_roots_after_config_change(tmp_path):
    w, store = claude_world(tmp_path)
    custom = tmp_path / "custom"
    doc = custom / "agent-memory" / "scribe" / "design.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("integrated")
    assert w.decide("approved", remove=False, direct=[doc]).state == "harvested"
    new_source = ClaudeMemorySource(ClaudeRoots(custom, custom / "projects", tmp_path))
    w.service.sources = lambda _: [w.source, new_source]
    result = w.decide(retention_days=0)
    assert result.state == "refused" and "outside native storage" in result.message
    assert (store / "note.md").read_bytes() == b"native private text\r\n"


@pytest.mark.parametrize("area", [".claude/rules", ".CLAUDE/RULES", "claude/rules"])
def test_direct_integration_rejects_instruction_areas_before_journaling(tmp_path, area):
    w, _ = claude_world(tmp_path)
    parent = (tmp_path / area) if area == "claude/rules" else (tmp_path / "project" / area)
    if area == ".CLAUDE/RULES":
        (parent.parent.parent / ".claude/rules").mkdir(parents=True)
        if not parent.is_dir():
            pytest.skip("case-insensitive filesystem required")
    else:
        parent.mkdir(parents=True)
    doc = parent / "learnings.md"
    doc.write_text("always loaded rule")
    outcome = w.decide("approved", remove=False, direct=[doc])
    assert outcome.state == "refused" and "Instruction files" in outcome.message
    assert memory_operations.operations(w.path) == []


def test_direct_integration_syncs_destination_file_and_directory_chain(tmp_path, monkeypatch):
    w, _ = claude_world(tmp_path)
    parent = tmp_path / "new" / "nested"
    parent.mkdir(parents=True)
    draft = parent / "draft"
    draft.write_text("integration")
    doc = parent / "design.md"
    draft.replace(doc)
    observed = []
    sync = os.fsync
    expected = [path.stat().st_ino for path in (doc, parent, parent.parent, tmp_path)]

    def fsync(fd):
        inode = os.fstat(fd).st_ino
        if inode in expected:
            observed.append(inode)
        sync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    assert w.decide("approved", remove=False, direct=[doc]).state == "harvested"
    assert observed[: len(expected)] == expected


def test_direct_integration_refuses_destination_replaced_during_sync(tmp_path, monkeypatch):
    w, _ = claude_world(tmp_path)
    doc = tmp_path / "design.md"
    doc.write_text("the original integration")
    other = tmp_path / "replacement"
    other.write_text("the replacement")
    original = doc.stat()
    sync = os.fsync

    def fsync(fd):
        sync(fd)
        if os.path.samestat(os.fstat(fd), original):
            other.replace(doc)

    monkeypatch.setattr(os, "fsync", fsync)
    result = w.decide("approved", remove=False, direct=[doc])
    assert result.state == "refused" and "changed while recording evidence" in result.message
    assert memory_operations.operations(w.path) == []
    assert doc.read_text() == "the replacement"


def test_inline_and_ambiguous_link_and_restore_collision(tmp_path):
    w, store = claude_world(tmp_path, inline=True)
    result = w.decide(decision="discard")
    assert result.state == "removed"
    assert (store / "MEMORY.md").read_text() == LINK + OTHER_LINK
    assert w.recover(result.operation_id, restore=True).state == "restored"
    (store / "MEMORY.md").write_text("discussion: [note](note.md) and [other](other.md)\n")
    w.ref = World(tmp_path, w.source, lambda r: r["path"] == str(store / "note.md")).ref
    assert w.decide(decision="discard").state == "refused"
    assert (store / "note.md").is_file()


def test_unchosen_empty_output_and_stale_ref(tmp_path):
    w, store = claude_world(tmp_path)
    assert w.decide().state == w.decide(" \t\n").state == "refused"
    assert w.decide("wrong ref", ref=replace(w.ref, incarnation="different")).state == "refused"
    assert (store / "note.md").read_bytes() == b"native private text\r\n"
    assert memory_operations.operations(w.path) == []


def test_concurrent_duplicate_decisions_and_restore_collision(tmp_path):
    w, store = claude_world(tmp_path)
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(lambda _: w.decide(decision="discard"), range(2)))
    assert len({op.operation_id for op in outcomes}) == 1
    assert [op.state for op in outcomes] == ["removed", "removed"]
    (store / "note.md").write_text("new knowledge")
    op = outcomes[0].operation_id
    assert w.recover(op, restore=True).state == "refused"
    assert (store / "note.md").read_text() == "new knowledge"


def test_refused_restore_preflight_still_expires_completed_removal(tmp_path):
    w, store = claude_world(tmp_path)
    result = w.decide(decision="discard", retention_days=1)
    (store / "note.md").write_text("new knowledge")
    assert w.recover(result.operation_id, restore=True).state == "refused"
    still_removed = memory_operations.load(result.operation_id, w.path)
    assert (still_removed.source_state, still_removed.done, still_removed.restoring) == (
        "removed",
        True,
        False,
    )
    assert prune_memory_recovery(
        registry_path=w.path, now=NOW + timedelta(days=90), delete=True
    ) == (result.operation_id,)
    assert memory_operations.load(result.operation_id, w.path).payload == {}
    assert (store / "note.md").read_text() == "new knowledge"


def test_restore_preserves_group_readable_topic_mode(tmp_path):
    w, store = claude_world(tmp_path)
    topic = store / "note.md"
    topic.chmod(0o640)
    result = w.decide(decision="discard")
    assert result.state == "removed"
    assert w.recover(result.operation_id, restore=True).state == "restored"
    assert (topic.read_bytes(), topic.stat().st_mode & 0o777) == (
        b"native private text\r\n",
        0o640,
    )


@pytest.mark.parametrize("action", ["remove", "restore"])
@pytest.mark.parametrize("boundary", ["topic", "index"])
def test_resume_each_claude_component_after_sigterm(tmp_path, action, boundary):
    w, store = claude_world(tmp_path)
    op = w.decide(decision="discard").operation_id if action == "restore" else None
    pid = os.fork()
    if pid == 0:

        def stop(self, name):
            if name == boundary:
                os.kill(os.getpid(), signal.SIGTERM)
            original(self, name)

        original = memory_operations.Operation.component
        memory_operations.Operation.component = stop
        if op:
            w.recover(op, restore=True)
        else:
            w.decide("approved before any source effects")
        os._exit(50)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == -signal.SIGTERM
    (journal,) = memory_operations.operations(w.path)
    if action == "remove":
        assert harvests.snapshot_harvests().items[0].content == "approved before any source effects"
    restored = w.recover(journal.id)
    assert restored.state == ("removed" if action == "remove" else "restored"), restored
    assert sorted(path.name for path in store.iterdir()) == (
        ["MEMORY.md", "other.md"] if action == "remove" else ["MEMORY.md", "note.md", "other.md"]
    )


def test_replay_weak_identity_requires_fresh_review_until_staged(tmp_path, monkeypatch):
    from ringleader.memory_sources import base, claude_code, claude_mutation

    def weak(stat):
        return "inode:" + str(stat.st_ino)

    for module in (base, claude_code, claude_mutation, transactions):
        monkeypatch.setattr(module, "file_identity", weak)
    w, store = claude_world(tmp_path)
    w.presence = replace(w.quiet, sessions=PresenceResult(available=False))
    op = w.decide(decision="discard").operation_id
    w.presence = w.quiet
    outcome = w.recover(op)
    assert outcome.state == "refused" and "fresh review" in outcome.message
    assert w.decide(decision="discard").state == "removed"
    assert (store / "other.md").read_text() == "other native content"


@pytest.mark.parametrize("body_proof", [True, False])
def test_persisted_publication_survives_queue_consumption_and_restore(tmp_path, body_proof):
    from ringleader.memory_types import origin_fields

    w, store = claude_world(tmp_path)
    saved = w.decide("approved", remove=False)
    (item,) = harvests.snapshot_harvests().items
    evidence = {
        **origin_fields(item.origin),
        "harvestId": item.harvest_id,
        "receiptId": "receipt",
        "checkout": str(tmp_path),
        "changedPaths": ["design.md"],
        "relativePath": item.path.name,
        "outcome": "changed",
    }
    if body_proof:
        evidence["approvedBodyDigest"] = item.body_digest
    registry.mutate_registry(
        w.path, lambda r: r.update(memoryPublications={item.harvest_id: evidence})
    )
    item.path.unlink()
    result = w.decide()
    if body_proof:
        assert result.state == "removed"
        assert w.recover(saved.operation_id, restore=True).state == "restored"
    else:
        assert result.state == "refused" and "approved Markdown" in result.message
    assert (store / "note.md").read_bytes() == b"native private text\r\n"
    assert harvests.snapshot_harvests().items == ()


def test_index_alias_remains_inline_through_capture_and_restore(tmp_path):
    w, store = claude_world(tmp_path, inline=True)
    index = store / "MEMORY.md"
    mixed = store / "mEmOrY.Md"
    index.rename(mixed)
    if not index.exists():
        mixed.rename(index)
        mixed.hardlink_to(index)
    row = next(r for r in w.source.scan().records if r.is_inline)
    assert row.ref.source_ref == str(index)
    if mixed.stat().st_nlink > 1:
        mixed.unlink()
    w.ref = w.scan()
    removed = w.decide(decision="discard")
    assert removed.state == "removed"
    assert index.read_text() == LINK + OTHER_LINK
    assert w.recover(removed.operation_id, restore=True).state == "restored"
    assert index.read_text() == "# Memory\ninline private text\n" + LINK + OTHER_LINK


def test_topic_removal_preserves_fenced_and_indented_index_examples(tmp_path):
    w, store = claude_world(tmp_path)
    index = store / "MEMORY.md"
    examples = "````markdown\n" + LINK + "```\n" + LINK + "````\n\n" + "    " + LINK + " \t" + LINK
    initial = "# Memory\n" + LINK + OTHER_LINK + examples
    index.write_text(initial)
    removed = w.decide(decision="discard")
    assert removed.state == "removed"
    assert index.read_text() == "# Memory\n" + OTHER_LINK + examples
    assert w.recover(removed.operation_id, restore=True).state == "restored"
    assert index.read_text() == initial


def test_direct_attestation_recognizes_rule_directory_identity(tmp_path):
    world, _ = claude_world(tmp_path)
    docs = tmp_path / "living-docs"
    docs.mkdir()
    guide = docs / "guide.md"
    guide.write_text("Project guide")
    (world.source.roots.config / "rules").symlink_to(docs, target_is_directory=True)
    outcome = world.decide("approved", remove=False, direct=[guide])
    assert outcome.state == "refused"
    assert guide.read_text() == "Project guide"
    assert memory_operations.operations(world.path) == []


@pytest.mark.parametrize(
    "example",
    [
        "- Example\n\n    ```markdown\n  [Topic](note.md)\n    ```\n",
        "> - Example\n>\n>     ~~~\n>   [Topic](note.md)\n>     ~~~\n",
    ],
)
def test_topic_removal_refuses_container_fences_before_native_edits(tmp_path, example):
    world, store = claude_world(tmp_path)
    index = store / "MEMORY.md"
    original = (LINK + OTHER_LINK + example).replace("\n", "\r\n").encode()
    index.write_bytes(original)
    result = world.decide(decision="discard")
    assert result.state == "refused", result
    assert index.read_bytes() == original
    assert (store / "note.md").read_bytes() == b"native private text\r\n"


def test_topic_removal_preserves_code_span_and_real_fence(tmp_path):
    world, store = claude_world(tmp_path)
    index = store / "MEMORY.md"
    example = "```inline code```\n\n```\n" + LINK + "```\u00a0\n" + LINK + "```\t \n"
    original = (example + LINK + OTHER_LINK).replace("\n", "\r\n").encode()
    index.write_bytes(original)
    removed = world.decide(decision="discard")
    assert removed.state == "removed", removed
    assert index.read_bytes() == (example + OTHER_LINK).replace("\n", "\r\n").encode()
    assert world.recover(removed.operation_id, restore=True).state == "restored"
    assert index.read_bytes() == original


def test_claude_confirmation_discovers_selected_store_once(tmp_path, monkeypatch):
    world, _ = claude_world(tmp_path)
    discover = ClaudeMemorySource._discover
    calls = []

    def observed(source):
        calls.append(source)
        return discover(source)

    monkeypatch.setattr(ClaudeMemorySource, "_discover", observed)
    assert world.decide("approved", remove=False).state == "harvested"
    assert calls == [world.source]


@pytest.mark.parametrize("in_subdirectory", [False, True])
@pytest.mark.parametrize("config_parent", ["home", "relocated"])
def test_standalone_document_approval_scopes_home_inventory(
    tmp_path, monkeypatch, in_subdirectory, config_parent
):
    from ringleader import instruction_targets

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    world, _ = claude_world(tmp_path)
    parent = tmp_path / config_parent
    parent.mkdir(exist_ok=True)
    (parent / ".claude").symlink_to(world.source.roots.config, target_is_directory=True)
    docs = parent / "docs" if in_subdirectory else parent
    docs.mkdir(exist_ok=True)
    guide = docs / "guide.md"
    guide.write_text("living document")
    unrelated = parent / "unrelated"
    unrelated.mkdir()
    for n in range(40):
        (unrelated / str(n)).touch()
    monkeypatch.setattr(instruction_targets, "_MAX_ENTRIES", 25)
    rules = world.source.roots.config / "rules"
    rules.mkdir()
    alias = rules / "project.md"
    alias.symlink_to(guide)
    refused = world.decide("approved", remove=False, direct=[guide])
    assert refused.state == "refused" and "outside native storage" in refused.message
    alias.unlink()
    result = world.decide("approved", remove=False, direct=[guide])
    assert result.state == "harvested", result


def test_multiline_code_span_cannot_remove_a_literal_topic_link(tmp_path):
    world, store = claude_world(tmp_path)
    index = store / "MEMORY.md"
    example = "```inline ` text\n[Topic](note.md)\ncloses ```\n\n"
    index.write_bytes((example + LINK + OTHER_LINK).encode())
    before = {p: p.read_bytes() for p in store.iterdir()}
    outcome = world.decide(decision="discard")
    if outcome.state == "removed":
        assert index.read_bytes() == (example + OTHER_LINK).encode()
    else:
        assert outcome.state == "refused"
        assert {p: p.read_bytes() for p in store.iterdir()} == before


@pytest.mark.parametrize("boundary", ["# Independent section", "Independent paragraph"])
def test_standalone_fence_after_list_and_heading_preserves_example(tmp_path, boundary):
    world, store = claude_world(tmp_path)
    index = store / "MEMORY.md"
    rest = f"- ordinary list\n\n{boundary}\n  ~~~markdown\n[Topic](note.md)\n  ~~~\n"
    index.write_bytes((LINK + OTHER_LINK + rest).encode())
    outcome = world.decide(decision="discard")
    assert outcome.state == "removed", outcome
    assert index.read_bytes() == (OTHER_LINK + rest).encode()


@pytest.mark.parametrize(
    ("before", "link", "after"),
    [
        ("- Example\n---\n  ~~~markdown\n[Topic](note.md)\n  ~~~\n", LINK, OTHER_LINK),
        ("- [Other](other.md) — use `a\\b`\n", LINK, ""),
        ("Paragraph `opens\ncloses here`\n", "[Topic](note.md)\n", ""),
        ("    literal with ` unmatched\n", LINK, OTHER_LINK),
        ("", LINK, OTHER_LINK),
    ],
)
def test_remove_genuine_link_keeps_original_markdown_bytes(tmp_path, before, link, after):
    world, store = claude_world(tmp_path)
    path = store / "MEMORY.md"
    original = ("\ufeff" + before + link + after).replace("\n", "\r\n").encode()
    path.write_bytes(original)
    outcome = world.decide(decision="discard")
    assert outcome.state == "removed", outcome
    assert path.read_bytes() == ("\ufeff" + before + after).replace("\n", "\r\n").encode()
    assert world.recover(outcome.operation_id, restore=True).state == "restored"
    assert path.read_bytes() == original


def test_remove_link_refuses_reinterpreting_retained_indentation(tmp_path):
    world, store = claude_world(tmp_path)
    path = store / "MEMORY.md"
    original = (LINK + "    Continuation of this list item\n").encode()
    path.write_bytes(original)
    outcome = world.decide(decision="discard")
    assert outcome.state == "refused"
    assert "reinterpret" in outcome.message
    assert path.read_bytes() == original
    assert (store / "note.md").read_bytes() == b"native private text\r\n"


@pytest.mark.parametrize(
    "index_text",
    [
        OTHER_LINK + LINK + "  Keep this guidance with the selected topic.\n",
        OTHER_LINK + LINK + "  - Document for this topic only.\n",
        "1. Restart the service.\n- [Topic](note.md)\n7. Restart the queue.\n",
    ],
)
def test_topic_removal_preserves_retained_list_ownership_and_numbering(tmp_path, index_text):
    world, store = claude_world(tmp_path)
    index = store / "MEMORY.md"
    index.write_bytes(index_text.replace("\n", "\r\n").encode())
    before = {path.name: path.read_bytes() for path in store.iterdir()}
    outcome = world.decide(decision="discard")
    assert outcome.state == "refused"
    assert "reinterpret" in outcome.message
    assert {path.name: path.read_bytes() for path in store.iterdir()} == before


def test_line_dense_index_refuses_topic_mutation_before_staging(tmp_path, monkeypatch):
    from ringleader.memory_sources import claude_index

    world, store = claude_world(tmp_path)
    path = store / "MEMORY.md"
    path.write_bytes(b"one\r\r\ntwo\rthree")
    before = {p.name: p.read_bytes() for p in store.iterdir()}
    monkeypatch.setattr(claude_index, "MAX_INDEX_LINES", 3)
    outcome = world.decide(decision="discard")
    assert outcome.state == "refused"
    assert "3 physical lines" in outcome.message
    assert {p.name: p.read_bytes() for p in store.iterdir()} == before
    assert not memory_operations.load(outcome.operation_id, world.path).payload.get("intent")
