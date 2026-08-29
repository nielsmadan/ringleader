import multiprocessing
import os
import time

import pytest
from ringleader import stateio


def test_atomic_write_bytes_replaces_complete_file(tmp_path):
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    stateio.atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"new"
    assert target.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_atomic_write_bytes_keeps_old_file_when_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    monkeypatch.setattr(os, "replace", lambda *_: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError, match="boom"):
        stateio.atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"old"
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def _try_lock(state_root, ready_path, entered_path):
    os.environ["XDG_DATA_HOME"] = state_root
    ready_path.write_text("ready")
    with stateio.publish_lock():
        entered_path.write_text("entered")


def _wait_for_path(path):
    for _ in range(20):
        if path.exists():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {path}")


def test_publish_lock_blocks_another_process(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    context = multiprocessing.get_context("fork")
    ready = tmp_path / "ready"
    entered = tmp_path / "entered"
    with stateio.publish_lock():
        child = context.Process(target=_try_lock, args=(str(tmp_path), ready, entered))
        child.start()
        _wait_for_path(ready)
        time.sleep(0.1)
        assert entered.exists() is False
    _wait_for_path(entered)
    child.join(2)
    assert child.exitcode == 0
