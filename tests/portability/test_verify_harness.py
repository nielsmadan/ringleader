from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from ringleader.portability.verify.codex import Client, client
from ringleader.portability.verify.common import VerificationFailed, require_outside_checkout

FAKE_CODEX = """#!/usr/bin/env python3
import json
import os
import sys


def main():
    with open("codex-fake.pid", "w") as fh:
        fh.write(str(os.getpid()))
    try:
        behavior = open("behavior.txt").read().strip()
    except OSError:
        behavior = "ok"
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        msg_id = message.get("id")
        if method == "initialize":
            if behavior == "reject":
                reply = {"id": msg_id, "error": {"message": "rejected by fake codex"}}
            else:
                reply = {"id": msg_id, "result": {}}
            print(json.dumps(reply), flush=True)
        elif method == "initialized":
            continue
        elif behavior == "hang":
            sys.stderr.write("fake codex stalled on " + str(method) + "\\n")
            sys.stderr.flush()
        else:
            print(json.dumps({"id": msg_id, "result": {}}), flush=True)


if __name__ == "__main__":
    main()
"""


def install_fake_codex(base: Path, behavior: str) -> str:
    fake = base / "fake-codex"
    fake.write_text(FAKE_CODEX)
    fake.chmod(0o755)
    cwd = base / "work" / "cwd"
    cwd.mkdir(parents=True)
    (cwd / "behavior.txt").write_text(behavior)
    return str(fake)


def wait_for_pid(pid_file: Path) -> int:
    deadline = time.monotonic() + 5
    while not pid_file.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{pid_file} was never created")
        time.sleep(0.02)
    return int(pid_file.read_text())


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_codex_handshake_failure_terminates_the_app_server(tmp_path):
    executable = install_fake_codex(tmp_path, "reject")
    base = tmp_path / "work"

    with pytest.raises(VerificationFailed, match="rejected by fake codex"):
        Client(executable, base)

    pid = wait_for_pid(base / "cwd" / "codex-fake.pid")
    # _terminate() waits on the process before returning, so by the time __init__
    # finishes raising, the child should already be gone.
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_codex_rpc_timeout_message_includes_exit_code_and_stderr_tail(tmp_path, monkeypatch):
    from ringleader.portability.verify import codex as verify_codex

    monkeypatch.setattr(verify_codex, "RPC_TIMEOUT", 2.0)
    executable = install_fake_codex(tmp_path, "hang")
    base = tmp_path / "work"

    with (
        client(executable, base) as session,
        pytest.raises(
            VerificationFailed,
            match=r"codex thread/list timed out \(exit=None, stderr tail: '[^']*stalled on thread/list[^']*'\)",
        ),
    ):
        session.call("thread/list", {"limit": 10})


def test_require_outside_checkout_refuses_a_path_inside_a_git_tree(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    inside = tmp_path / "repo" / "nested" / "verify"
    with pytest.raises(
        VerificationFailed, match="self-verification workdir must be outside any git checkout"
    ):
        require_outside_checkout(inside)
    outside = tmp_path / "scratch" / "verify"
    assert require_outside_checkout(outside) == outside.resolve()
