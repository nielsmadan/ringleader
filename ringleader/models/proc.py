from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..progress import CancelCheck
from .base import ModelError

_POLL_SECONDS = 0.25
_TERM_GRACE_SECONDS = 2.0

_live_groups: set[int] = set()
_live_lock = threading.Lock()


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def launchable(command_prefix: Sequence[str]) -> bool:
    return not command_prefix or shutil.which(command_prefix[0]) is not None


def neutral_cwd() -> Path:
    return Path(tempfile.gettempdir())


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _stop(proc: subprocess.Popen[str]) -> None:
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        proc.communicate(timeout=_TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_group(proc.pid, signal.SIGKILL)
        try:
            proc.communicate(timeout=_TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    with contextlib.suppress(OSError):
                        pipe.close()
            proc.wait()


def run_model_process(
    argv: Sequence[str],
    *,
    stdin_text: str | None,
    timeout: float | None,
    cancelled: CancelCheck,
    cwd: Path | None = None,
) -> ProcessResult:
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            cwd=cwd or neutral_cwd(),
        )
    except OSError as e:
        raise ModelError(f"{argv[0]} invocation failed: {e}") from e
    with _live_lock:
        _live_groups.add(proc.pid)
    try:
        deadline = None if timeout is None else time.monotonic() + timeout
        pending_input = stdin_text
        while True:
            try:
                out, err = proc.communicate(input=pending_input, timeout=_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                pending_input = None
            else:
                return ProcessResult(proc.returncode, out, err)
            if cancelled():
                _stop(proc)
                raise ModelError("cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                _stop(proc)
                raise ModelError(f"timed out after {timeout:.0f}s")
    except BaseException:
        if proc.poll() is None:
            _stop(proc)
        raise
    finally:
        with _live_lock:
            _live_groups.discard(proc.pid)


def _still_live(groups: Sequence[int]) -> list[int]:
    with _live_lock:
        return [pgid for pgid in groups if pgid in _live_groups]


def kill_all_children() -> int:
    with _live_lock:
        groups = sorted(_live_groups)
    for pgid in groups:
        _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + _TERM_GRACE_SECONDS
    while time.monotonic() < deadline and _still_live(groups):
        time.sleep(_POLL_SECONDS / 5)
    for pgid in _still_live(groups):
        _signal_group(pgid, signal.SIGKILL)
    return len(groups)
