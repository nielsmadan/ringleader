from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from ringleader import harvests, publish_state, registry
from ringleader.commands import harvest as harvest_command
from ringleader.commands.harvests import cmd_harvests
from ringleader.memory_types import MemoryOrigin
from ringleader.publish_git import PublishGitError
from ringleader.publish_state import (
    PublishStateError,
    abandon_receipt,
    begin_publish,
    consume_publish,
    list_receipts,
    load_receipt,
    prepare_publish,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    def write(repo_key: str, bodies: list[str]):
        paths = [
            harvests.write_harvest(
                session_id=f"session-{index}",
                name=f"harvest-{index}",
                folder_path="/work/repo",
                repo_url=repo_key,
                harvested_at=f"2026-08-28T00:00:0{index}+00:00",
                content=body,
            )
            for index, body in enumerate(bodies)
        ]
        by_path = {item.path: item for item in harvests.snapshot_harvests().items}
        return [by_path[path] for path in paths]

    return write


def _begin(item, tmp_path, *, checkout="checkout", tokens=None, bundle=None):
    return begin_publish(
        repo_key=item.repo_key,
        checkout=str(tmp_path / checkout),
        branch="refs/heads/main",
        base_commit="a" * 40,
        registry_path=str(tmp_path / "registry.json"),
        tokens=tokens if tokens is not None else [item.token],
        bundle_path=bundle if bundle is not None else tmp_path / f"{checkout}.md",
    )


def _clean_checkout(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
        ("commit", "--allow-empty", "-qm", "initial"),
    ):
        subprocess.run(["git", "-C", str(checkout), *args], check=True)
    base_commit = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    branch = subprocess.run(
        ["git", "-C", str(checkout), "symbolic-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return checkout, branch, base_commit


def _prepare_changed_receipt(selected, tmp_path):
    checkout, branch, base_commit = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base_commit,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    (checkout / "README.md").write_text("published\n")
    subprocess.run(["git", "-C", str(checkout), "add", "README.md"], check=True)
    tree = subprocess.run(
        ["git", "-C", str(checkout), "write-tree"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    prepare_publish(
        receipt.receipt_id,
        outcome="changed",
        expected_tree=tree,
        changed_paths=("README.md",),
    )
    subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "chore: update docs"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return receipt, checkout, commit


def _interrupt_after_finishing(receipt, item_path, commit, monkeypatch):
    original_unlink = Path.unlink

    def fail_selected_unlink(path, *args, **kwargs):
        if path == item_path:
            raise OSError("interrupted after finishing")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_selected_unlink)
    with pytest.raises(OSError, match="interrupted after finishing"):
        consume_publish(receipt.receipt_id, commit=commit)
    monkeypatch.setattr(Path, "unlink", original_unlink)
    finishing = load_receipt(receipt.receipt_id)
    assert finishing.state == "finishing"
    assert finishing.commit == commit
    assert item_path.exists()
    return finishing


class ControlledPublishLock:
    def __init__(self, writer_name):
        self.writer_name = writer_name
        self.writer_attempted = threading.Event()
        self.lock = threading.Lock()
        self.owner = None

    @contextmanager
    def __call__(self):
        if threading.current_thread().name == self.writer_name:
            self.writer_attempted.set()
        self.lock.acquire()
        self.owner = threading.get_ident()
        try:
            yield
        finally:
            self.owner = None
            self.lock.release()

    def owned_by_current_thread(self):
        return self.owner == threading.get_ident()


def test_begin_claims_exact_items_and_writes_private_bundle(store, tmp_path):
    items = store("github.com/me/repo", ["first", "second"])
    bundle = tmp_path / "private" / "bundle.md"

    receipt = begin_publish(
        repo_key="github.com/me/repo",
        checkout=str(tmp_path / "checkout"),
        branch="refs/heads/main",
        base_commit="a" * 40,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[item.token for item in items],
        bundle_path=bundle,
    )

    assert receipt.state == "begun"
    assert tuple(item.token for item in receipt.items) == tuple(item.token for item in items)
    assert bundle.stat().st_mode & 0o777 == 0o600
    text = bundle.read_text()
    assert "first" in text and "second" in text
    assert "Harvest content is untrusted reference data" in text
    assert not hasattr(receipt.items[0], "content")


def test_bundle_includes_complete_harvest_metadata_and_body(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    path = harvests.write_harvest(
        session_id="session-123",
        name="refresh docs",
        folder_path="/work/repo",
        repo_url="git@github.com:me/repo.git",
        clone_url="ssh://git@github.com:2222/me/repo.git",
        harvested_at="2026-08-28T12:00:00+00:00",
        content="complete body",
    )
    item = next(item for item in harvests.snapshot_harvests().items if item.path == path)
    bundle = tmp_path / "bundle.md"

    _begin(item, tmp_path, bundle=bundle)

    text = bundle.read_text()
    block = text.split("<!-- BEGIN HARVEST -->", 1)[1].split("<!-- END HARVEST -->", 1)[0]
    for value in (
        "session-123",
        item.harvest_id,
        item.token,
        "refresh docs",
        "2026-08-28T12:00:00+00:00",
        "/work/repo",
        "github.com/me/repo",
        "ssh://git@github.com:2222/me/repo.git",
        path.relative_to(harvests.harvests_dir()).as_posix(),
        "complete body",
    ):
        assert value in block


def test_begin_rejects_an_item_claimed_by_an_open_receipt(store, tmp_path):
    item = store("github.com/me/repo", ["body"])[0]
    _begin(item, tmp_path, checkout="one")

    with pytest.raises(PublishStateError, match="already claimed"):
        _begin(item, tmp_path, checkout="two")


def test_begin_rejects_a_checkout_claimed_by_an_open_receipt(store, tmp_path):
    first = store("github.com/me/one", ["one"])[0]
    second = store("github.com/me/two", ["two"])[0]
    _begin(first, tmp_path, checkout="same")

    with pytest.raises(PublishStateError, match="checkout"):
        _begin(second, tmp_path, checkout="same")


@pytest.mark.parametrize(
    ("tokens", "message"),
    [
        (["unknown:token"], "unknown or changed item"),
        (lambda item: [item.token, item.token], "duplicate item token"),
    ],
)
def test_begin_rejects_unknown_or_duplicate_tokens(store, tmp_path, tokens, message):
    item = store("github.com/me/repo", ["body"])[0]
    selected = tokens(item) if callable(tokens) else tokens

    with pytest.raises(PublishStateError, match=message):
        _begin(item, tmp_path, tokens=selected)


def test_begin_rejects_a_token_when_its_harvest_digest_changed(store, tmp_path):
    item = store("github.com/me/repo", ["body"])[0]
    item.path.write_text(item.path.read_text().replace("body", "changed"))

    with pytest.raises(PublishStateError, match="unknown or changed item"):
        _begin(item, tmp_path)


def test_begin_rejects_items_from_multiple_repositories(store, tmp_path):
    first = store("github.com/me/one", ["one"])[0]
    second = store("github.com/me/two", ["two"])[0]

    with pytest.raises(PublishStateError, match="one repository group"):
        _begin(first, tmp_path, tokens=[first.token, second.token])


@pytest.mark.parametrize(
    "bodies",
    [
        [str(index) for index in range(101)],
        ["x" * 131073],
    ],
)
def test_begin_rejects_groups_above_publish_bounds(store, tmp_path, bodies):
    items = store("github.com/me/repo", bodies)

    with pytest.raises(PublishStateError, match="publish bounds"):
        _begin(items[0], tmp_path, tokens=[item.token for item in items])


def test_begin_rejects_bundle_inside_checkout(store, tmp_path):
    item = store("github.com/me/repo", ["body"])[0]
    checkout = tmp_path / "checkout"

    with pytest.raises(PublishStateError, match="outside the checkout"):
        _begin(item, tmp_path, bundle=checkout / "inputs.md")


def test_begin_keeps_malformed_harvest_files_untouched(store, tmp_path):
    item = store("github.com/me/repo", ["body"])[0]
    malformed = harvests.harvests_dir() / "broken.md"
    malformed.write_bytes(b"\xff")

    _begin(item, tmp_path)

    assert malformed.read_bytes() == b"\xff"


def test_bundle_write_failure_retains_the_begun_receipt(store, tmp_path, monkeypatch):
    item = store("github.com/me/repo", ["body"])[0]
    bundle = tmp_path / "bundle.md"
    original = publish_state.atomic_write_text

    def write(path, text, *, mode=0o600):
        if path == bundle:
            raise OSError("bundle write failed")
        original(path, text, mode=mode)

    monkeypatch.setattr(publish_state, "atomic_write_text", write)

    with pytest.raises(OSError, match="bundle write failed"):
        _begin(item, tmp_path, bundle=bundle)

    receipts, problems = list_receipts()
    assert problems == ()
    assert len(receipts) == 1
    assert receipts[0].state == "begun"
    assert receipts[0].items[0].token == item.token


@pytest.fixture
def receipt(store, tmp_path):
    return _begin(store("github.com/me/repo", ["body"])[0], tmp_path)


def test_prepare_changed_records_tree_and_paths(receipt):
    prepared = prepare_publish(
        receipt.receipt_id,
        outcome="changed",
        expected_tree="b" * 40,
        changed_paths=("README.md", "docs/guide.md"),
    )

    assert prepared.state == "prepared"
    assert prepared.outcome == "changed"
    assert prepared.expected_tree == "b" * 40
    assert prepared.changed_paths == ("README.md", "docs/guide.md")


def test_prepare_no_change_has_no_tree_or_paths(receipt):
    prepared = prepare_publish(receipt.receipt_id, outcome="no-change")

    assert prepared.outcome == "no-change"
    assert prepared.expected_tree is None
    assert prepared.changed_paths == ()


def test_consume_removes_exact_items_and_keeps_a_newer_same_session_item(store, tmp_path):
    selected = store("github.com/me/repo", ["selected"])[0]
    checkout, branch, base_commit = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base_commit,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    newer_path = harvests.write_harvest(
        session_id=selected.session_id,
        name="newer",
        folder_path="/work/repo",
        repo_url="github.com/me/repo",
        harvested_at="2026-08-29T00:00:00+00:00",
        content="newer",
    )
    newer = next(item for item in harvests.snapshot_harvests().items if item.path == newer_path)
    reg = registry.empty_registry()
    reg["archived"] = [
        {
            "sessionId": selected.session_id,
            "harvestPath": ["docs/architecture.md", str(selected.path)],
            "thread": {
                "sessionId": selected.session_id,
                "harvestPath": ["docs/architecture.md", str(selected.path)],
            },
        }
    ]
    registry.save_registry(receipt.registry_path, reg)

    result = consume_publish(receipt.receipt_id, commit=None)

    assert result.consumed == 1
    assert [item.token for item in harvests.snapshot_harvests().items] == [newer.token]
    archived = registry.load_registry(receipt.registry_path)["archived"][0]
    assert archived["harvestPath"] == ["docs/architecture.md", str(newer.path)]
    assert archived["thread"]["harvestPath"] == ["docs/architecture.md", str(newer.path)]


def test_consume_resumes_after_an_interrupted_unlink(store, tmp_path, monkeypatch):
    selected = store("github.com/me/repo", ["first", "second"])
    checkout, branch, base_commit = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected[0].repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base_commit,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[item.token for item in selected],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    reg = registry.empty_registry()
    reg["archived"] = [
        {"sessionId": item.session_id, "harvestPath": [str(item.path)]} for item in selected
    ]
    registry.save_registry(receipt.registry_path, reg)
    original_unlink = Path.unlink
    calls = 0

    def fail_second(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("interrupted")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_second)
    with pytest.raises(OSError, match="interrupted"):
        consume_publish(receipt.receipt_id, commit=None)
    monkeypatch.setattr(Path, "unlink", original_unlink)

    assert load_receipt(receipt.receipt_id).state == "finishing"
    result = consume_publish(receipt.receipt_id, commit=None)

    assert result.consumed == len(selected)
    assert list_receipts()[0] == ()
    assert harvests.snapshot_harvests().items == ()


def test_finishing_consume_rejects_a_malformed_selected_path(store, tmp_path, monkeypatch):
    selected = store("github.com/me/repo", ["selected"])[0]
    checkout, branch, base_commit = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base_commit,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    registry.save_registry(receipt.registry_path, registry.empty_registry())
    _interrupt_after_finishing(receipt, selected.path, None, monkeypatch)
    selected.path.write_bytes(b"\xff")

    with pytest.raises(PublishStateError, match="missing or changed"):
        consume_publish(receipt.receipt_id, commit=None)

    assert load_receipt(receipt.receipt_id).state == "finishing"
    assert selected.path.read_bytes() == b"\xff"


def test_consume_rejects_a_same_content_symlink_substitution(store, tmp_path):
    selected = store("github.com/me/repo", ["selected"])[0]
    checkout, branch, base_commit = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base_commit,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    registry.save_registry(receipt.registry_path, registry.empty_registry())
    duplicate = harvests.harvests_dir() / "duplicate.md"
    duplicate.write_bytes(selected.path.read_bytes())
    selected.path.unlink()
    os.symlink(duplicate.name, selected.path)

    with pytest.raises(PublishStateError, match="missing or changed"):
        consume_publish(receipt.receipt_id, commit=None)

    assert load_receipt(receipt.receipt_id).state == "prepared"
    assert selected.path.is_symlink()
    assert duplicate.exists()


def test_consume_rejects_selected_fifo_promptly_without_leaking_subprocess(store, tmp_path):
    selected = store("github.com/me/repo", ["selected"])[0]
    checkout, branch, base_commit = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base_commit,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    registry.save_registry(receipt.registry_path, registry.empty_registry())
    selected.path.unlink()
    os.mkfifo(selected.path)
    script = """
import sys
from ringleader.publish_state import PublishStateError, consume_publish

try:
    consume_publish(sys.argv[1], commit=None)
except PublishStateError as error:
    if "missing or changed" not in str(error):
        raise
else:
    raise SystemExit("FIFO was accepted")
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, receipt.receipt_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        pytest.fail("consume blocked while opening the selected FIFO")

    assert process.returncode == 0, (stdout, stderr)
    assert process.poll() == 0
    assert load_receipt(receipt.receipt_id).state == "prepared"


def test_harvest_directory_fsync_failure_keeps_finishing_receipt_and_retry_resyncs(
    store, tmp_path, monkeypatch
):
    selected = store("github.com/me/repo", ["selected"])[0]
    checkout, branch, base_commit = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base_commit,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    reg = registry.empty_registry()
    reg["archived"] = [{"sessionId": selected.session_id, "harvestPath": [str(selected.path)]}]
    registry.save_registry(receipt.registry_path, reg)
    real_fsync = publish_state.os.fsync
    harvest_directory = harvests.harvests_dir().stat()
    directory_syncs = 0

    def fail_first_harvest_directory_fsync(fd):
        nonlocal directory_syncs
        target = publish_state.os.fstat(fd)
        if target.st_ino == harvest_directory.st_ino and target.st_dev == harvest_directory.st_dev:
            directory_syncs += 1
            if directory_syncs == 1:
                raise OSError("harvest directory sync failed")
        return real_fsync(fd)

    monkeypatch.setattr(publish_state.os, "fsync", fail_first_harvest_directory_fsync)

    with pytest.raises(OSError, match="harvest directory sync failed"):
        consume_publish(receipt.receipt_id, commit=None)

    assert not selected.path.exists()
    assert load_receipt(receipt.receipt_id).state == "finishing"
    assert registry.load_registry(receipt.registry_path)["archived"][0]["harvestPath"] == [
        str(selected.path)
    ]

    result = consume_publish(receipt.receipt_id, commit=None)

    assert result.consumed == 1
    assert directory_syncs == 2
    assert list_receipts()[0] == ()
    assert registry.load_registry(receipt.registry_path)["archived"][0]["harvestPath"] is None


def test_receipt_removal_fsync_failure_does_not_raise_after_consumption(
    store, tmp_path, monkeypatch
):
    selected = store("github.com/me/repo", ["selected"])[0]
    checkout, branch, base_commit = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base_commit,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    registry.save_registry(receipt.registry_path, registry.empty_registry())
    _interrupt_after_finishing(receipt, selected.path, None, monkeypatch)
    real_fsync = publish_state.os.fsync
    receipt_directory = publish_state.publish_receipts_dir().stat()

    def fail_receipt_directory_fsync(fd):
        target = publish_state.os.fstat(fd)
        if target.st_ino == receipt_directory.st_ino and target.st_dev == receipt_directory.st_dev:
            raise OSError("receipt directory sync failed")
        return real_fsync(fd)

    monkeypatch.setattr(publish_state.os, "fsync", fail_receipt_directory_fsync)

    result = consume_publish(receipt.receipt_id, commit=None)

    assert result.consumed == 1
    assert list_receipts()[0] == ()


def test_receipt_removal_close_failure_after_unlink_does_not_raise(store, tmp_path, monkeypatch):
    selected = store("github.com/me/repo", ["selected"])[0]
    checkout, branch, base_commit = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base_commit,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    registry.save_registry(receipt.registry_path, registry.empty_registry())
    _interrupt_after_finishing(receipt, selected.path, None, monkeypatch)
    receipt_directory = publish_state.publish_receipts_dir().stat()
    real_close = publish_state.os.close
    failed_close = False

    def close_then_fail_for_receipt_directory(fd):
        nonlocal failed_close
        target = publish_state.os.fstat(fd)
        real_close(fd)
        if target.st_ino == receipt_directory.st_ino and target.st_dev == receipt_directory.st_dev:
            failed_close = True
            raise OSError("receipt directory close failed")

    monkeypatch.setattr(publish_state.os, "close", close_then_fail_for_receipt_directory)

    result = consume_publish(receipt.receipt_id, commit=None)

    assert result.consumed == 1
    assert failed_close
    assert list_receipts()[0] == ()


def test_consume_and_concurrent_harvest_preserve_reconciled_and_new_paths(
    store, tmp_path, monkeypatch
):
    selected = store("github.com/me/repo", ["selected"])[0]
    checkout, branch, base_commit = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base_commit,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    writer_transcript = tmp_path / "writer.jsonl"
    writer_transcript.write_text('{"type":"user","message":{"content":"hi"}}\n')
    reg = registry.empty_registry()
    reg["threads"] = [
        {
            "sessionId": "writer",
            "file": str(writer_transcript),
            "project": "/repo",
            "gitProjectLabel": "repo",
            "title": "writer",
        }
    ]
    reg["archived"] = [{"sessionId": selected.session_id, "harvestPath": [str(selected.path)]}]
    registry.save_registry(receipt.registry_path, reg)

    class Model:
        def complete_json(self, prompt, schema):
            return {"summary": "writer summary"}

    monkeypatch.setattr(harvest_command, "active_model", lambda config: Model())
    monkeypatch.setattr(harvest_command.export, "render_conversation", lambda path: "hi")
    consume_loaded = threading.Event()
    writer_done = threading.Event()
    controlled_lock = ControlledPublishLock("harvest-writer")
    monkeypatch.setattr(publish_state, "publish_lock", controlled_lock)
    monkeypatch.setattr(harvest_command, "publish_lock", controlled_lock)
    real_load = registry.load_registry

    def controlled_load(path):
        loaded = real_load(path)
        if threading.current_thread().name == "consume" and not consume_loaded.is_set():
            consume_loaded.set()
            assert controlled_lock.writer_attempted.wait(3)
            if not controlled_lock.owned_by_current_thread():
                assert writer_done.wait(3)
        return loaded

    monkeypatch.setattr(registry, "load_registry", controlled_load)
    results = {}

    def run_consume():
        results["consume"] = consume_publish(receipt.receipt_id, commit=None)

    def run_writer():
        try:
            assert consume_loaded.wait(3)
            results["writer"] = harvest_command.cmd_harvest(
                ["writer", "--registry", receipt.registry_path]
            )
        finally:
            writer_done.set()

    consume_thread = threading.Thread(target=run_consume, name="consume")
    writer_thread = threading.Thread(target=run_writer, name="harvest-writer")
    consume_thread.start()
    writer_thread.start()
    consume_thread.join(5)
    writer_thread.join(5)

    assert not consume_thread.is_alive()
    assert not writer_thread.is_alive()
    assert results["consume"].consumed == 1
    assert results["writer"] == 0
    saved = real_load(receipt.registry_path)
    assert saved["archived"][0]["harvestPath"] is None
    assert len(saved["threads"][0]["harvestPath"]) == 1
    remaining = harvests.snapshot_harvests().items
    assert [item.session_id for item in remaining] == ["claude-code:writer"]
    assert saved["threads"][0]["harvestPath"] == [str(remaining[0].path)]


@pytest.mark.parametrize(
    ("outcome", "commit", "message"),
    [
        ("changed", None, "requires a commit"),
        ("no-change", "c" * 40, "does not accept a commit"),
    ],
)
def test_consume_enforces_the_outcome_commit_contract(store, tmp_path, outcome, commit, message):
    selected = store("github.com/me/repo", ["selected"])[0]
    checkout, branch, base_commit = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base_commit,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    if outcome == "changed":
        prepare_publish(
            receipt.receipt_id,
            outcome="changed",
            expected_tree="b" * 40,
            changed_paths=("README.md",),
        )
    else:
        prepare_publish(receipt.receipt_id, outcome="no-change")

    with pytest.raises(PublishStateError, match=message):
        consume_publish(receipt.receipt_id, commit=commit)

    assert load_receipt(receipt.receipt_id).state == "prepared"


def test_consume_rejects_changed_selected_content_and_keeps_receipt_and_registry(store, tmp_path):
    selected = store("github.com/me/repo", ["selected"])[0]
    checkout, branch, base = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    reg = registry.empty_registry()
    registry.save_registry(receipt.registry_path, reg)
    selected.path.write_text(selected.path.read_text().replace("selected", "changed"))
    with pytest.raises(PublishStateError, match="missing or changed"):
        consume_publish(receipt.receipt_id, commit=None)
    assert load_receipt(receipt.receipt_id).state == "prepared"
    assert registry.load_registry(receipt.registry_path) == reg


def test_consume_refuses_to_strand_a_live_harvested_session(store, tmp_path):
    selected = store("github.com/me/repo", ["selected"])[0]
    checkout, branch, base = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    reg = registry.empty_registry()
    reg["threads"] = [
        {
            "sessionId": selected.session_id,
            "decision": "harvested",
            "harvestPath": [str(selected.path)],
        }
    ]
    registry.save_registry(receipt.registry_path, reg)
    with pytest.raises(PublishStateError, match="archive live session"):
        consume_publish(receipt.receipt_id, commit=None)
    assert selected.path.exists()
    assert load_receipt(receipt.receipt_id).state == "prepared"


def test_consume_without_registry_record_is_retry_safe_after_registry_save_failure(
    store, tmp_path, monkeypatch
):
    selected = store("github.com/me/repo", ["selected"])[0]
    checkout, branch, base = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    registry.save_registry(receipt.registry_path, registry.empty_registry())
    real_save = publish_state.registry.commit_registry
    monkeypatch.setattr(
        publish_state.registry,
        "commit_registry",
        lambda *args: (_ for _ in ()).throw(OSError("save failed")),
    )
    with pytest.raises(OSError, match="save failed"):
        consume_publish(receipt.receipt_id, commit=None)
    assert load_receipt(receipt.receipt_id).state == "finishing"
    assert not selected.path.exists()
    monkeypatch.setattr(publish_state.registry, "commit_registry", real_save)
    assert consume_publish(receipt.receipt_id, commit=None).consumed == 1


@pytest.mark.parametrize("outcome", ("changed", "no-change"))
def test_finishing_retry_ignores_later_checkout_state(store, tmp_path, monkeypatch, outcome):
    selected = store("github.com/me/repo", ["selected"])[0]
    if outcome == "changed":
        receipt, checkout, commit = _prepare_changed_receipt(selected, tmp_path)
    else:
        checkout, branch, base = _clean_checkout(tmp_path)
        receipt = begin_publish(
            repo_key=selected.repo_key,
            checkout=str(checkout),
            branch=branch,
            base_commit=base,
            registry_path=str(tmp_path / "registry.json"),
            tokens=[selected.token],
            bundle_path=tmp_path / "bundle.md",
        )
        prepare_publish(receipt.receipt_id, outcome="no-change")
        commit = None
    registry.save_registry(receipt.registry_path, registry.empty_registry())
    real_save = publish_state.registry.commit_registry
    monkeypatch.setattr(
        publish_state.registry,
        "commit_registry",
        lambda *args: (_ for _ in ()).throw(OSError("save failed")),
    )
    with pytest.raises(OSError, match="save failed"):
        consume_publish(receipt.receipt_id, commit=commit)
    monkeypatch.setattr(publish_state.registry, "commit_registry", real_save)
    assert load_receipt(receipt.receipt_id).state == "finishing"
    assert not selected.path.exists()
    (checkout / "ordinary-work.txt").write_text("later work\n")

    def unexpected_git_verification(*args, **kwargs):
        raise PublishGitError("finishing retry rechecked Git")

    monkeypatch.setattr(publish_state, "verify_published_commit", unexpected_git_verification)
    monkeypatch.setattr(publish_state, "require_clean_base", unexpected_git_verification)

    assert consume_publish(receipt.receipt_id, commit=commit).consumed == 1


def test_finishing_changed_retry_rejects_a_different_commit(store, tmp_path, monkeypatch):
    selected = store("github.com/me/repo", ["selected"])[0]
    receipt, _checkout, commit = _prepare_changed_receipt(selected, tmp_path)
    registry.save_registry(receipt.registry_path, registry.empty_registry())
    real_save = publish_state.registry.commit_registry
    monkeypatch.setattr(
        publish_state.registry,
        "commit_registry",
        lambda *args: (_ for _ in ()).throw(OSError("save failed")),
    )
    with pytest.raises(OSError, match="save failed"):
        consume_publish(receipt.receipt_id, commit=commit)
    monkeypatch.setattr(publish_state.registry, "commit_registry", real_save)

    with pytest.raises(PublishStateError, match="different commit"):
        consume_publish(receipt.receipt_id, commit="d" * 40)


def test_receipt_unlink_failure_keeps_a_discoverable_finishing_receipt(
    store, tmp_path, monkeypatch
):
    selected = store("github.com/me/repo", ["selected"])[0]
    checkout, branch, base = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    registry.save_registry(receipt.registry_path, registry.empty_registry())
    real_remove = publish_state._remove_receipt_unlocked
    monkeypatch.setattr(
        publish_state,
        "_remove_receipt_unlocked",
        lambda _: (_ for _ in ()).throw(OSError("unlink failed")),
    )
    with pytest.raises(OSError, match="unlink failed"):
        consume_publish(receipt.receipt_id, commit=None)
    assert load_receipt(receipt.receipt_id).state == "finishing"
    monkeypatch.setattr(publish_state, "_remove_receipt_unlocked", real_remove)
    assert consume_publish(receipt.receipt_id, commit=None).consumed == 1


def test_prepare_rejects_illegal_outcomes(receipt):
    with pytest.raises(PublishStateError, match="changed"):
        prepare_publish(receipt.receipt_id, outcome="changed")
    with pytest.raises(PublishStateError, match="no-change"):
        prepare_publish(
            receipt.receipt_id,
            outcome="no-change",
            expected_tree="b" * 40,
        )


def test_prepare_only_transitions_a_begun_receipt(receipt):
    prepare_publish(receipt.receipt_id, outcome="no-change")

    with pytest.raises(PublishStateError, match="begun"):
        prepare_publish(receipt.receipt_id, outcome="no-change")


def test_publish_state_does_not_expose_an_unverified_finishing_transition():
    assert not hasattr(publish_state, "mark_finishing")


def test_no_change_finishing_retry_rejects_a_commit(store, tmp_path, monkeypatch):
    selected = store("github.com/me/repo", ["selected"])[0]
    checkout, branch, base_commit = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base_commit,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    registry.save_registry(receipt.registry_path, registry.empty_registry())
    _interrupt_after_finishing(receipt, selected.path, None, monkeypatch)

    with pytest.raises(PublishStateError, match="no-change"):
        consume_publish(receipt.receipt_id, commit="c" * 40)


@pytest.mark.parametrize("state", ["begun", "prepared"])
def test_abandon_only_removes_begun_receipts(receipt, state):
    if state == "prepared":
        prepare_publish(receipt.receipt_id, outcome="no-change")
        with pytest.raises(PublishStateError, match="begun"):
            abandon_receipt(receipt.receipt_id)
    else:
        abandon_receipt(receipt.receipt_id)
        with pytest.raises(PublishStateError, match="not found"):
            load_receipt(receipt.receipt_id)


def test_abandon_rejects_a_verified_finishing_receipt(store, tmp_path, monkeypatch):
    selected = store("github.com/me/repo", ["selected"])[0]
    checkout, branch, base_commit = _clean_checkout(tmp_path)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base_commit,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    registry.save_registry(receipt.registry_path, registry.empty_registry())
    _interrupt_after_finishing(receipt, selected.path, None, monkeypatch)

    with pytest.raises(PublishStateError, match="begun"):
        abandon_receipt(receipt.receipt_id)


def test_list_reports_malformed_receipts_without_trusting_them(receipt, tmp_path, monkeypatch):
    from ringleader.paths import publish_receipts_dir

    bad = publish_receipts_dir() / "not-a-receipt.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json")

    receipts, problems = list_receipts()

    assert [item.receipt_id for item in receipts] == [receipt.receipt_id]
    assert [(problem.path, problem.message) for problem in problems] == [(bad, "invalid JSON")]


def test_list_reports_a_receipt_symlink_without_distrusting_its_target(receipt):
    from ringleader.paths import publish_receipts_dir

    target = publish_receipts_dir() / f"{receipt.receipt_id}.json"
    link = publish_receipts_dir() / "linked.json"
    os.symlink(target.name, link)

    receipts, problems = list_receipts()

    assert [item.receipt_id for item in receipts] == [receipt.receipt_id]
    assert [problem.path for problem in problems] == [link]
    assert "read" in problems[0].message or "regular file" in problems[0].message


def test_recover_reports_a_receipt_fifo_promptly(receipt):
    from ringleader.paths import publish_receipts_dir

    fifo = publish_receipts_dir() / "blocked.json"
    os.mkfifo(fifo)
    script = """
from ringleader.commands.harvests import cmd_harvests

raise SystemExit(cmd_harvests(["recover", "--json"]))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        pytest.fail("receipt listing blocked while opening the FIFO")

    assert process.returncode == 0, (stdout, stderr)
    payload = json.loads(stdout)
    assert [item["receiptId"] for item in payload["receipts"]] == [receipt.receipt_id]
    assert [problem["path"] for problem in payload["problems"]] == [str(fifo)]


def test_load_rejects_unsafe_and_unknown_receipt_data(receipt):
    from ringleader.paths import publish_receipts_dir

    path = publish_receipts_dir() / f"{receipt.receipt_id}.json"
    raw = json.loads(path.read_text())
    raw["items"][0]["relativePath"] = "../../outside.md"
    raw["unexpected"] = True
    path.write_text(json.dumps(raw))

    with pytest.raises(PublishStateError, match=r"unknown keys|relativePath"):
        load_receipt(receipt.receipt_id)


@pytest.mark.parametrize(
    "changed_paths",
    [
        [""],
        ["/absolute.md"],
        ["."],
        [".."],
        ["docs/../escape.md"],
        ["nul\x00path.md"],
        ["line\nbreak.md"],
        ["README.md", "README.md"],
    ],
)
def test_load_rejects_unsafe_or_duplicate_changed_paths(receipt, changed_paths):
    prepared = prepare_publish(
        receipt.receipt_id,
        outcome="changed",
        expected_tree="b" * 40,
        changed_paths=("README.md",),
    )
    from ringleader.paths import publish_receipts_dir

    path = publish_receipts_dir() / f"{prepared.receipt_id}.json"
    raw = json.loads(path.read_text())
    raw["changedPaths"] = changed_paths
    path.write_text(json.dumps(raw))

    with pytest.raises(PublishStateError, match="changedPaths"):
        load_receipt(prepared.receipt_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("baseCommit", "not-an-oid"),
        ("expectedTree", "not-an-oid"),
    ],
)
def test_load_rejects_invalid_oids_and_state_combinations(receipt, field, value):
    prepared = prepare_publish(
        receipt.receipt_id,
        outcome="changed",
        expected_tree="b" * 40,
        changed_paths=("README.md",),
    )
    from ringleader.paths import publish_receipts_dir

    path = publish_receipts_dir() / f"{prepared.receipt_id}.json"
    raw = json.loads(path.read_text())
    raw[field] = value
    path.write_text(json.dumps(raw))

    with pytest.raises(PublishStateError, match=r"invalid|illegal|requires"):
        load_receipt(prepared.receipt_id)


def test_load_rejects_an_invalid_commit_in_a_verified_finishing_receipt(
    store, tmp_path, monkeypatch
):
    selected = store("github.com/me/repo", ["selected"])[0]
    receipt, _checkout, commit = _prepare_changed_receipt(selected, tmp_path)
    registry.save_registry(receipt.registry_path, registry.empty_registry())
    finishing = _interrupt_after_finishing(receipt, selected.path, commit, monkeypatch)
    from ringleader.paths import publish_receipts_dir

    path = publish_receipts_dir() / f"{finishing.receipt_id}.json"
    raw = json.loads(path.read_text())
    raw["commit"] = "not-an-oid"
    path.write_text(json.dumps(raw))

    with pytest.raises(PublishStateError, match=r"invalid|illegal|requires"):
        load_receipt(finishing.receipt_id)


def test_load_rejects_an_illegal_no_change_state_combination(receipt):
    prepared = prepare_publish(receipt.receipt_id, outcome="no-change")
    from ringleader.paths import publish_receipts_dir

    path = publish_receipts_dir() / f"{prepared.receipt_id}.json"
    raw = json.loads(path.read_text())
    raw["expectedTree"] = "b" * 40
    path.write_text(json.dumps(raw))

    with pytest.raises(PublishStateError, match="no-change"):
        load_receipt(prepared.receipt_id)


def test_list_distrusts_every_receipt_with_a_duplicate_id_or_item_claim(receipt):
    from ringleader.paths import publish_receipts_dir

    path = publish_receipts_dir() / f"{receipt.receipt_id}.json"
    duplicate_id = publish_receipts_dir() / "duplicate-id.json"
    duplicate_id.write_bytes(path.read_bytes())
    raw = json.loads(path.read_text())
    other_id = uuid.uuid4().hex
    raw["receiptId"] = other_id
    duplicate_token = publish_receipts_dir() / f"{other_id}.json"
    duplicate_token.write_text(json.dumps(raw))

    receipts, problems = list_receipts()

    assert receipts == ()
    assert {problem.path for problem in problems} == {path, duplicate_id, duplicate_token}


def test_load_requires_the_filename_to_match_the_receipt_identity(receipt):
    from ringleader.paths import publish_receipts_dir

    path = publish_receipts_dir() / f"{receipt.receipt_id}.json"
    moved = publish_receipts_dir() / "wrong-name.json"
    path.replace(moved)

    receipts, problems = list_receipts()

    assert receipts == ()
    assert [(problem.path, problem.message) for problem in problems] == [
        (moved, "receipt filename does not match receiptId")
    ]


def test_list_distrusts_original_when_a_wrongly_named_copy_claims_its_items(receipt):
    from ringleader.paths import publish_receipts_dir

    path = publish_receipts_dir() / f"{receipt.receipt_id}.json"
    copy = publish_receipts_dir() / "wrong-name.json"
    copy.write_bytes(path.read_bytes())

    receipts, problems = list_receipts()

    assert receipts == ()
    assert {problem.path for problem in problems} == {path, copy}


def test_list_distrusts_a_wrong_filename_with_a_fresh_id_sharing_only_the_token(receipt):
    from ringleader.paths import publish_receipts_dir

    original = publish_receipts_dir() / f"{receipt.receipt_id}.json"
    raw = json.loads(original.read_text())
    raw["receiptId"] = uuid.uuid4().hex
    collision = publish_receipts_dir() / "wrong-name.json"
    collision.write_text(json.dumps(raw))

    receipts, problems = list_receipts()

    assert receipts == ()
    assert {problem.path: problem.message for problem in problems} == {
        original: "duplicate item token",
        collision: "receipt filename does not match receiptId; duplicate item token",
    }


def test_list_deduplicates_repeated_collision_reasons_in_first_seen_order(store, tmp_path):
    items = store("github.com/me/repo", ["first", "second"])
    receipt = _begin(items[0], tmp_path, tokens=[item.token for item in items])
    from ringleader.paths import publish_receipts_dir

    original = publish_receipts_dir() / f"{receipt.receipt_id}.json"
    raw = json.loads(original.read_text())
    raw["receiptId"] = uuid.uuid4().hex
    collision = publish_receipts_dir() / "wrong-name.json"
    collision.write_text(json.dumps(raw))

    receipts, problems = list_receipts()

    assert receipts == ()
    assert {problem.path: problem.message for problem in problems} == {
        original: "duplicate item token",
        collision: "receipt filename does not match receiptId; duplicate item token",
    }


def test_load_rejects_distinct_items_with_the_same_harvest_relative_path(store, tmp_path):
    items = store("github.com/me/repo", ["first", "second"])
    receipt = _begin(items[0], tmp_path, tokens=[item.token for item in items])
    from ringleader.paths import publish_receipts_dir

    path = publish_receipts_dir() / f"{receipt.receipt_id}.json"
    raw = json.loads(path.read_text())
    raw["items"][1]["relativePath"] = raw["items"][0]["relativePath"]
    path.write_text(json.dumps(raw))

    with pytest.raises(PublishStateError, match="relativePath"):
        load_receipt(receipt.receipt_id)


def test_serialized_receipt_omits_harvest_bodies(store, tmp_path):
    item = store("github.com/me/repo", ["body that must stay private"])[0]
    receipt = _begin(item, tmp_path)
    from ringleader.paths import publish_receipts_dir

    text = (publish_receipts_dir() / f"{receipt.receipt_id}.json").read_text()

    assert "body that must stay private" not in text


def test_qualified_harvest_roundtrips_publish_to_native_session(tmp_path):
    checkout, branch, base = _clean_checkout(tmp_path)
    registry_path = tmp_path / "registry.json"
    reg = registry.empty_registry()
    reg["threads"] = [
        {
            "sessionId": "shared",
            "source": source,
            "project": str(checkout),
            "gitRemote": "https://github.com/me/repo",
            "harvestPath": ["docs/existing.md"],
        }
        for source in ("claude-code", "codex")
    ]
    registry.save_registry(registry_path, reg)
    markdown = tmp_path / "harvest.md"
    markdown.write_text("# Useful result\n")
    assert (
        harvest_command.cmd_harvest(
            ["codex:shared", "--registry", str(registry_path), "--from-file", str(markdown)]
        )
        == 0
    )
    item = harvests.snapshot_harvests().items[0]
    assert item.session_id == "codex:shared"
    current = registry.load_registry(registry_path)
    registry.archive_session(current, "codex:shared", str(tmp_path / "archive.jsonl"), "now")
    registry.commit_registry(registry_path, current)
    receipt = begin_publish(
        repo_key=item.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base,
        registry_path=str(registry_path),
        tokens=[item.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    assert consume_publish(receipt.receipt_id, commit=None).consumed == 1
    saved = registry.load_registry(registry_path)
    assert saved["archived"][0]["sessionId"] == "shared"
    assert saved["archived"][0]["source"] == "codex"
    assert saved["archived"][0]["harvestPath"] == ["docs/existing.md"]
    assert saved["threads"][0]["harvestPath"] == ["docs/existing.md"]


def test_publish_refuses_ambiguous_legacy_harvest_before_unlink(store, tmp_path):
    item = store("github.com/me/repo", ["legacy"])[0]
    checkout, branch, base = _clean_checkout(tmp_path)
    registry_path = tmp_path / "registry.json"
    reg = registry.empty_registry()
    reg["archived"] = [
        {"sessionId": item.session_id, "source": source, "harvestPath": [str(item.path)]}
        for source in ("claude-code", "codex")
    ]
    registry.save_registry(registry_path, reg)
    receipt = begin_publish(
        repo_key=item.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base,
        registry_path=str(registry_path),
        tokens=[item.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    original = registry_path.read_bytes()
    with pytest.raises(registry.AmbiguousSession, match="ambiguous session"):
        consume_publish(receipt.receipt_id, commit=None)
    assert registry_path.read_bytes() == original
    assert item.path.read_text().endswith("legacy\n")


@pytest.mark.parametrize(
    "selected_reference,remaining_reference",
    [
        ("same", "claude-code:same"),
        ("claude-code:same", "same"),
    ],
)
@pytest.mark.parametrize("bucket", ["threads", "archived"])
def test_consume_preserves_remaining_harvest_across_selector_spellings(
    tmp_path, selected_reference, remaining_reference, bucket
):
    checkout, branch, base = _clean_checkout(tmp_path)
    registry_path = tmp_path / "registry.json"
    paths = [
        harvests.write_harvest(
            session_id=reference,
            name="result",
            folder_path=str(checkout),
            repo_url="github.com/me/repo",
            harvested_at="2026-09-21T12:00:00+00:00",
            content="durable knowledge",
        )
        for reference in (selected_reference, remaining_reference)
    ]
    selected = next(item for item in harvests.snapshot_harvests().items if item.path == paths[0])
    reg = registry.empty_registry()
    reg[bucket] = [
        {
            "sessionId": "same",
            "source": "claude-code",
            "decision": "harvested",
            "harvestPath": [str(path) for path in paths],
        }
    ]
    registry.save_registry(registry_path, reg)
    receipt = begin_publish(
        repo_key=selected.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base,
        registry_path=str(registry_path),
        tokens=[selected.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    assert consume_publish(receipt.receipt_id, commit=None).consumed == 1
    assert registry.load_registry(registry_path)[bucket][0]["harvestPath"] == [str(paths[1])]
    assert [item.path for item in harvests.snapshot_harvests().items] == [paths[1]]


def _interrupt_registry_projection(*_args):
    raise OSError("projection interrupted")


def test_mixed_memory_publish_recovers_assignment_and_finishing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    checkout, branch, head = _clean_checkout(tmp_path)
    reg_path = tmp_path / "registry.json"
    origin = MemoryOrigin("memory001", "codex", "fingerprint", "incarnation", "project", "/source")
    first_write = {
        "origin": origin,
        "harvest_id": "f" * 32,
        "registry_path": reg_path,
        "folder_path": None,
        "repo_url": None,
        "name": "style",
        "harvested_at": "now",
        "content": "  Exact wording.\n\n",
    }
    path = harvests.write_harvest(**first_write)
    item = harvests.load_harvest_path(path)
    reg = registry.empty_registry()
    reg["memoryDecisions"] = {
        "operation": {
            "harvestId": item.harvest_id,
            "harvestToken": item.token,
            "sourceState": "present",
            "harvestPath": ["/doc/previous.md", str(path)],
        }
    }
    registry.save_registry(reg_path, reg)
    with pytest.raises(PublishStateError, match="repository group"):
        _begin(item, tmp_path)
    with pytest.raises(PublishStateError, match="existing Git checkout"):
        publish_state.assign_memory_harvest(item.token, project=tmp_path, registry_path=reg_path)
    with monkeypatch.context() as failure, pytest.raises(OSError, match="projection interrupted"):
        failure.setattr(registry, "commit_registry", _interrupt_registry_projection)
        publish_state.assign_memory_harvest(item.token, project=checkout, registry_path=reg_path)
    assert cmd_harvests(["recover", "--registry", str(reg_path)]) == 0
    capsys.readouterr()
    assigned = harvests.load_harvest_path(path)
    assert assigned.origin == origin
    assert assigned.content == item.content
    assert assigned.harvest_id == item.harvest_id
    assert assigned.repo_key == str(checkout)
    assert assigned.token != item.token
    with pytest.raises(PublishStateError, match="changed memory harvest"):
        publish_state.assign_memory_harvest(item.token, project=checkout, registry_path=reg_path)
    assert (
        registry.load_registry(reg_path)["memoryDecisions"]["operation"]["harvestToken"]
        == assigned.token
    )
    assert cmd_harvests(["snapshot", "--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)["items"]
    assert (row["destinationPath"], row["sourceProject"], row["originKind"]) == (
        str(checkout),
        "/source",
        "memory",
    )

    session = harvests.load_harvest_path(
        harvests.write_harvest(
            session_id="s",
            name="session",
            folder_path=str(checkout),
            repo_url=None,
            harvested_at="now",
            content="Another note",
        )
    )
    bundle = tmp_path / "bundle"
    receipt = begin_publish(
        repo_key=assigned.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=head,
        registry_path=str(reg_path),
        tokens=[assigned.token, session.token],
        bundle_path=bundle,
    )
    persisted = load_receipt(receipt.receipt_id)
    assert persisted.items[0].origin == origin
    assert persisted.items[1].session_id == "s"
    receipt_items = publish_state.receipt_data(persisted)["items"]
    assert receipt_items[0]["approvedBodyDigest"] == assigned.body_digest
    assert set(receipt_items[1]) == {"harvestId", "digest", "token", "sessionId", "relativePath"}
    assert "sourceProject: /source" in bundle.read_text()
    with pytest.raises(PublishStateError, match="claimed"):
        publish_state.assign_memory_harvest(
            assigned.token, project=checkout, registry_path=reg_path
        )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    with monkeypatch.context() as failure, pytest.raises(OSError, match="projection interrupted"):
        failure.setattr(registry, "commit_registry", _interrupt_registry_projection)
        consume_publish(receipt.receipt_id, commit=None)
    assert load_receipt(receipt.receipt_id).state == "finishing"
    assert harvests.snapshot_harvests().items == ()
    with pytest.raises(ValueError, match="requires publish receipt recovery"):
        harvests.write_harvest(**first_write)
    assert cmd_harvests(["consume", receipt.receipt_id]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["sessionIds"] == ["s"] and result["memoryIds"] == [origin.memory_id]
    after = registry.load_registry(reg_path)
    evidence = after["memoryPublications"][assigned.harvest_id]
    assert (
        evidence["outcome"],
        evidence["token"],
        evidence["memoryFingerprint"],
        evidence["memoryIncarnation"],
        evidence["approvedBodyDigest"],
    ) == ("no-change", assigned.token, origin.fingerprint, origin.incarnation, assigned.body_digest)
    decision = after["memoryDecisions"]["operation"]
    assert (decision["harvestPath"], decision["sourceState"], decision["harvestToken"]) == (
        ["/doc/previous.md"],
        "present",
        None,
    )


def test_old_memory_receipt_can_finish_without_independent_body_proof(tmp_path):
    checkout, branch, base = _clean_checkout(tmp_path)
    reg_path = tmp_path / "registry.json"
    path = harvests.write_harvest(
        origin=MemoryOrigin("memory001", "codex", "fingerprint", "incarnation", "global", None),
        folder_path=str(checkout),
        repo_url=None,
        name="style",
        harvested_at="now",
        content="Exact wording",
        registry_path=reg_path,
    )
    item = harvests.load_harvest_path(path)
    receipt = begin_publish(
        repo_key=item.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base,
        registry_path=str(reg_path),
        tokens=[item.token],
        bundle_path=tmp_path / "bundle",
    )
    record = publish_state._receipt_path(receipt.receipt_id)
    legacy = json.loads(record.read_text())
    legacy["items"][0].pop("approvedBodyDigest")
    record.write_text(json.dumps(legacy))
    legacy_receipt = load_receipt(receipt.receipt_id)
    assert publish_state.receipt_data(legacy_receipt) == legacy
    prepare_publish(receipt.receipt_id, outcome="no-change")
    assert consume_publish(receipt.receipt_id, commit=None).memory_ids == ("memory001",)
    assert registry.load_registry(reg_path)["memoryPublications"][item.harvest_id]["token"] == (
        item.token
    )
    assert list_receipts() == ((), ())


@pytest.mark.parametrize("assigned_before_recovery", (False, True))
def test_published_memory_evidence_survives_saved_queue_replay(
    tmp_path, monkeypatch, capsys, assigned_before_recovery
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg_path = tmp_path / "registry.json"
    checkout, branch, head = _clean_checkout(tmp_path)
    old_root = tmp_path / "other"
    old_root.mkdir()
    old_checkout, _, _ = _clean_checkout(old_root)
    origin = MemoryOrigin("note", "codex", "fp", "v1", "global", None)
    kwargs = {
        "origin": origin,
        "harvest_id": "a" * 32,
        "registry_path": reg_path,
        "folder_path": str(old_checkout) if assigned_before_recovery else None,
        "repo_url": None,
        "name": "note",
        "harvested_at": "2026-11-18",
        "content": "Approved wording.",
    }
    path = harvests.write_harvest(**kwargs)
    stale = path.read_bytes()
    item = harvests.load_harvest_path(path)
    reg = registry.empty_registry()
    reg["memoryDecisions"] = {
        "operation": {
            "harvestId": item.harvest_id,
            "harvestPath": ["/living/previous.md", str(path)],
            "sourceState": "present",
        },
    }
    registry.save_registry(reg_path, reg)
    assigned = publish_state.assign_memory_harvest(
        item.token, project=checkout, registry_path=reg_path
    )
    assigned_bytes = path.read_bytes()
    receipt = begin_publish(
        repo_key=assigned.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=head,
        registry_path=str(reg_path),
        tokens=[assigned.token],
        bundle_path=tmp_path / "bundle",
    )
    prepare_publish(receipt.receipt_id, outcome="no-change")
    assert consume_publish(receipt.receipt_id, commit=None).memory_ids == (origin.memory_id,)
    completed = registry.load_registry(reg_path)
    with pytest.raises(harvests.HarvestAlreadyPublished) as error:
        harvests.write_harvest(**kwargs)
    assert error.value.evidence == completed["memoryPublications"][assigned.harvest_id]
    with pytest.raises(ValueError, match="conflicting memory publication evidence"):
        harvests.write_harvest(
            **{**kwargs, "origin": MemoryOrigin("note", "codex", "fp", "v2", "global", None)}
        )
    assert harvests.snapshot_harvests().items == ()

    path.write_bytes(stale)
    assert cmd_harvests(["recover", "--registry", str(reg_path)]) == 0
    capsys.readouterr()
    assert registry.load_registry(reg_path) == completed
    with pytest.raises(PublishStateError, match="already published"):
        publish_state.assign_memory_harvest(item.token, project=checkout, registry_path=reg_path)
    path.write_bytes(assigned_bytes)
    with pytest.raises(PublishStateError, match="already published"):
        _begin(assigned, tmp_path)
    assert path.read_bytes() == assigned_bytes
    assert list_receipts() == ((), ())

    fresh = harvests.load_harvest_path(
        harvests.write_harvest(
            **{
                **kwargs,
                "origin": MemoryOrigin("fresh-note", "codex", "fp", "v1", "global", None),
                "harvest_id": "b" * 32,
                "folder_path": str(checkout),
            }
        )
    )
    assert cmd_harvests(["snapshot", "--json", "--registry", str(reg_path)]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert [row["token"] for row in snapshot["items"]] == [fresh.token]
    assert cmd_harvests(["--json", "--registry", str(reg_path)]) == 0
    assert [(row["path"], row["content"]) for row in json.loads(capsys.readouterr().out)] == [
        (str(fresh.path), fresh.content)
    ]
    assert snapshot["problems"] == [
        {"path": str(path), "message": f"memory harvest already published: {assigned.harvest_id}"}
    ]
    new_receipt = begin_publish(
        repo_key=assigned.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=head,
        registry_path=str(reg_path),
        tokens=[row["token"] for row in snapshot["items"]],
        bundle_path=tmp_path / "fresh-bundle",
    )
    assert [claim.harvest_id for claim in new_receipt.items] == [fresh.harvest_id]
    assert path.read_bytes() == assigned_bytes


def _queued_memory(tmp_path, checkout):
    return harvests.load_harvest_path(
        harvests.write_harvest(
            origin=MemoryOrigin("note", "codex", "fp", "v1", "global", None),
            harvest_id="a" * 32,
            registry_path=tmp_path / "registry.json",
            folder_path=str(checkout),
            repo_url=None,
            name="note",
            harvested_at="2026-11-18",
            content="Approved wording.",
        )
    )


def _begin_queued_memory(item, tmp_path, checkout, branch, head):
    return begin_publish(
        repo_key=item.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=head,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[item.token],
        bundle_path=tmp_path / "bundle.md",
    )


def test_duplicate_memory_ids_block_publish_and_recovery_projection(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    checkout, branch, head = _clean_checkout(tmp_path)
    item = _queued_memory(tmp_path, checkout)
    receipt = _begin_queued_memory(item, tmp_path, checkout, branch, head)
    prepare_publish(receipt.receipt_id, outcome="no-change")
    reg_path = tmp_path / "registry.json"
    reg = registry.empty_registry()
    reg["memoryDecisions"] = {"operation": {"harvestId": item.harvest_id}}
    registry.save_registry(reg_path, reg)
    duplicate = item.path.with_name("other.md")
    duplicate.write_text(
        item.path.read_text()
        .replace("memoryId: note", "memoryId: other")
        .replace("Approved wording.", "Different draft.")
    )
    other = harvests.load_harvest_path(duplicate)

    assert cmd_harvests(["snapshot"]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["items"] == []
    assert {problem["path"] for problem in snapshot["problems"]} == {
        str(item.path),
        str(other.path),
    }
    with pytest.raises(PublishStateError, match="duplicate harvest ID"):
        _begin(other, tmp_path)
    with pytest.raises(PublishStateError, match="duplicate harvest ID"):
        publish_state.assign_memory_harvest(item.token, project=checkout, registry_path=reg_path)
    with pytest.raises(PublishStateError, match="duplicate harvest ID"):
        consume_publish(receipt.receipt_id, commit=None)
    assert cmd_harvests(["recover", "--registry", str(reg_path)]) == 1
    recovery = capsys.readouterr()
    assert f"duplicate harvest ID in receipt: {item.harvest_id}" in recovery.err
    assert recovery.out == ""
    assert registry.load_registry(reg_path)["memoryDecisions"] == reg["memoryDecisions"]
    assert load_receipt(receipt.receipt_id).state == "prepared"
    duplicate.unlink()
    assert harvests.load_harvest_path(item.path) == item


def test_memory_receipt_claim_survives_changed_token(tmp_path, monkeypatch, capsys):
    from ringleader.paths import publish_receipts_dir

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    checkout, _, _ = _clean_checkout(tmp_path)
    item = _queued_memory(tmp_path, checkout)
    receipt = _begin(item, tmp_path)
    reg_path = tmp_path / "registry.json"
    reg = registry.load_registry(reg_path)
    reg["memoryDecisions"] = {
        "operation": {
            "harvestId": item.harvest_id,
            "harvestToken": item.token,
            "harvestPath": [str(item.path)],
            "destinationProject": str(checkout),
        }
    }
    registry.commit_registry(reg_path, reg)
    item.path.write_text(item.path.read_text().replace("Approved wording.", "Different draft."))
    edited = harvests.load_harvest_path(item.path)
    assert cmd_harvests(["snapshot"]) == 0
    (row,) = json.loads(capsys.readouterr().out)["items"]
    assert (row["token"], row["claimedBy"]) == (edited.token, receipt.receipt_id)
    with pytest.raises(PublishStateError, match="already claimed"):
        _begin(edited, tmp_path)

    conflicting = publish_state.receipt_data(receipt)
    other_id = uuid.uuid4().hex
    conflicting["receiptId"] = other_id
    conflicting["items"][0].update(digest=edited.digest, token=edited.token)
    other_receipt = publish_receipts_dir() / f"{other_id}.json"
    other_receipt.write_text(json.dumps(conflicting))
    receipts, problems = list_receipts()
    assert receipts == ()
    assert {problem.path.stem: problem.message for problem in problems} == {
        receipt.receipt_id: "duplicate memory harvest ID claim",
        other_id: "duplicate memory harvest ID claim",
    }
    assert cmd_harvests(["recover", "--json", "--registry", str(reg_path)]) == 0
    recovery = json.loads(capsys.readouterr().out)
    assert recovery == {
        "receipts": [],
        "problems": [
            {"path": str(problem.path), "message": problem.message} for problem in problems
        ],
    }
    assert registry.load_registry(reg_path) == reg


def test_conflicting_publication_evidence_blocks_consume_before_unlink(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    checkout, branch, head = _clean_checkout(tmp_path)
    item = _queued_memory(tmp_path, checkout)
    receipt = _begin_queued_memory(item, tmp_path, checkout, branch, head)
    prepare_publish(receipt.receipt_id, outcome="no-change")
    reg_path = tmp_path / "registry.json"
    reg = registry.load_registry(reg_path)
    reg["memoryPublications"] = {item.harvest_id: {"receiptId": "older receipt"}}
    registry.commit_registry(reg_path, reg)

    with pytest.raises(PublishStateError, match="conflicting memory publication evidence"):
        consume_publish(receipt.receipt_id, commit=None)

    assert harvests.load_harvest_path(item.path) == item
    assert load_receipt(receipt.receipt_id).state == "prepared"


def test_memory_receipt_rejects_mixed_origin(store, tmp_path):
    (item,) = store("example/repo", ["text"])
    receipt = _begin(item, tmp_path)
    data = publish_state.receipt_data(receipt)
    data["items"][0]["originKind"] = "memory"
    with pytest.raises(PublishStateError, match="origin"):
        publish_state._receipt_from_data(data)


@pytest.mark.parametrize("leading", ["", "  "])
def test_memory_assignment_stores_public_routing_and_filters_existing_queue_urls(
    tmp_path, capsys, leading
):
    from ringleader.commands.harvests import cmd_harvests

    checkout, branch, head = _clean_checkout(tmp_path)
    private = (
        leading
        + "https://fixture-name:fixture-pass@example.test/org/docs.git?access=fixture-query#fixture-fragment"
    )
    public = "https://example.test/org/docs.git"
    subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", private], check=True)
    reg_path = tmp_path / "registry.json"
    queued = _queued_memory(tmp_path, checkout)
    assigned = publish_state.assign_memory_harvest(
        queued.token, project=checkout, registry_path=reg_path
    )
    text = assigned.path.read_text()
    assert assigned.clone_url == public
    assert (assigned.repo_key, assigned.repo_url) == (
        "example.test/org/docs",
        "example.test/org/docs",
    )
    assert "fixture-" not in text
    assigned.path.write_text(text.replace(public, private))
    assert cmd_harvests(["snapshot", "--json", "--registry", str(reg_path)]) == 0
    (item,) = json.loads(capsys.readouterr().out)["items"]
    assert item["destinationCloneUrl"] == public
    assert cmd_harvests(["--json", "--registry", str(reg_path)]) == 0
    (body,) = json.loads(capsys.readouterr().out)
    assert body["destinationRepoUrl"] == "example.test/org/docs"
    assert "destinationCloneUrl" not in body
    bundle = tmp_path / "bundle"
    begin_publish(
        repo_key=assigned.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=head,
        registry_path=str(reg_path),
        tokens=[item["token"]],
        bundle_path=bundle,
    )
    assert "destinationRepoUrl: example.test/org/docs" in bundle.read_text()
    assert "fixture-" not in bundle.read_text()


def test_memory_bundle_identifies_checkout_alias_of_configured_rule(tmp_path, monkeypatch):
    checkout, branch, head = _clean_checkout(tmp_path)
    config = tmp_path / "custom-configuration"
    config.mkdir()
    docs = checkout / "docs"
    docs.mkdir()
    (config / "rules").symlink_to(docs, target_is_directory=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    item = _queued_memory(tmp_path, checkout)

    _begin_queued_memory(item, tmp_path, checkout, branch, head)

    rendered = (tmp_path / "bundle.md").read_text()
    label = "Protected instruction paths in this checkout (including descendants):"
    paths = json.loads(rendered.split(label + "\n")[1].splitlines()[0])
    assert paths == ["docs"]


@pytest.mark.parametrize("config_exists", [True, False])
def test_memory_bundle_reserves_future_rules_under_checkout_config_alias(tmp_path, config_exists):
    checkout, branch, head = _clean_checkout(tmp_path)
    config = checkout / "docs" / "agent"
    if config_exists:
        config.mkdir(parents=True)
    (checkout / ".claude").symlink_to(config, target_is_directory=True)
    item = _queued_memory(tmp_path, checkout)

    _begin_queued_memory(item, tmp_path, checkout, branch, head)

    label = "Protected instruction paths in this checkout (including descendants):"
    rendered = (tmp_path / "bundle.md").read_text()
    paths = json.loads(rendered.split(label + "\n")[1].splitlines()[0])
    assert paths == [
        "docs/agent/agents",
        "docs/agent/commands",
        "docs/agent/rules",
        "docs/agent/skills",
    ]


@pytest.mark.parametrize(
    ("legacy_key", "locator"),
    [
        (
            "  https///fixture-name:fixture-pass@example.test/org/docs.git?key=fixture-query#fragment",
            "  https://fixture-name:fixture-pass@example.test/org/docs.git?key=fixture-query#fragment",
        ),
        (
            "example.test/org/docs.git?key=fixture-query",
            "https://example.test/org/docs.git?key=fixture-query",
        ),
    ],
)
def test_historical_session_routing_is_public_on_read_and_new_harvest(
    tmp_path, capsys, legacy_key, locator
):
    from ringleader.harvesting import save_harvest_unlocked

    checkout, branch, head = _clean_checkout(tmp_path)
    path = tmp_path / "registry.json"
    reg = registry.load_registry(path)
    reg["threads"] = [
        {
            "sessionId": "old-session",
            "gitRemote": legacy_key,
            "gitCloneUrl": locator,
            "project": str(checkout),
        }
    ]
    registry.commit_registry(path, reg)
    public = "https://example.test/org/docs.git"
    key = "example.test/org/docs"
    _, queued = save_harvest_unlocked(
        "old-session", str(path), "Reviewed session knowledge", now_iso="2026-01-01"
    )
    new = queued.read_text()
    assert f"repoUrl: {key}\n" in new and f"cloneUrl: {public}\n" in new
    queued.write_text(
        new.replace(f"repoUrl: {key}\n", f"repoUrl: {legacy_key}\n").replace(
            f"cloneUrl: {public}\n", f"cloneUrl: {locator}\n"
        )
    )
    assert cmd_harvests(["snapshot", "--json", "--registry", str(path)]) == 0
    (item,) = json.loads(capsys.readouterr().out)["items"]
    assert (item["repoUrl"], item["repoKey"], item["cloneUrl"]) == (key, key, public)
    bundle = tmp_path / "bundle.md"
    begin_publish(
        repo_key=key,
        checkout=str(checkout),
        branch=branch,
        base_commit=head,
        registry_path=str(path),
        tokens=[item["token"]],
        bundle_path=bundle,
    )
    rendered = bundle.read_text()
    assert key in rendered
    assert "fixture-" not in rendered


def test_consume_reports_receipt_sync_io_error_and_retries(tmp_path, monkeypatch, capsys):
    import errno

    from ringleader import stateio

    checkout, branch, head = _clean_checkout(tmp_path)
    item = _queued_memory(tmp_path, checkout)
    receipt = _begin_queued_memory(item, tmp_path, checkout, branch, head)
    prepare_publish(receipt.receipt_id, outcome="no-change")
    sync = stateio.sync_directory

    def failed_sync(directory, **kwargs):
        if directory == publish_state._receipt_path(receipt.receipt_id).parent:
            raise OSError(errno.EIO, "receipt directory sync")
        sync(directory, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(stateio, "sync_directory", failed_sync)
        assert cmd_harvests(["consume", receipt.receipt_id]) == 1
    output = capsys.readouterr()
    assert (output.out, output.err) == (
        "",
        f"rl-harvests: [Errno {errno.EIO}] receipt directory sync\n",
    )
    assert load_receipt(receipt.receipt_id).state == "finishing"
    assert harvests.load_harvest_path(item.path).token == item.token
    assert cmd_harvests(["consume", receipt.receipt_id]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "receiptId": receipt.receipt_id,
        "consumed": 1,
        "sessionIds": [],
        "memoryIds": ["note"],
    }
