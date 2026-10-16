from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass

from ..progress import CancelCheck

POLL_SECONDS = 0.05
TERM_GRACE_SECONDS = 2.0
READER_JOIN_SECONDS = 2.0
TIMEOUT_SECONDS = 600.0
MAX_OUTPUT_CHARS = 2_000_000


@dataclass(frozen=True)
class CommandRun:
    argv: tuple[str, ...]
    returncode: int | None
    output: str
    seconds: float


def _stop_group(process: subprocess.Popen[str]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(TERM_GRACE_SECONDS)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def run_rl(
    argv: Sequence[str],
    *,
    cancelled: CancelCheck,
    columns: int,
    python: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> CommandRun:
    started = time.monotonic()
    child_env = {
        **(os.environ if env is None else env),
        "NO_COLOR": "1",
        "TERM": "dumb",
        "COLUMNS": str(columns),
    }
    process = subprocess.Popen(
        [python or sys.executable, "-m", "ringleader", *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
        env=child_env,
    )
    chunks: list[str] = []
    truncated = threading.Event()
    stream = process.stdout

    def read() -> None:
        kept = 0
        max_chars = MAX_OUTPUT_CHARS
        for chunk in stream or ():
            if kept >= max_chars:
                truncated.set()
                continue
            chunks.append(chunk[: max_chars - kept])
            kept += len(chunks[-1])
            if len(chunks[-1]) < len(chunk):
                truncated.set()

    reader = threading.Thread(target=read, name="rl-run-reader", daemon=True)
    reader.start()
    was_cancelled = False
    timed_out = False
    while process.poll() is None:
        timed_out = time.monotonic() - started >= timeout
        if cancelled() or timed_out:
            was_cancelled = True
            _stop_group(process)
            break
        time.sleep(POLL_SECONDS)
    process.wait()
    reader.join(READER_JOIN_SECONDS)
    if stream is not None and not reader.is_alive():
        stream.close()
    notes = [
        *(["[output truncated]"] if truncated.is_set() else []),
        *([f"[timed out after {timeout:.0f}s]"] if timed_out else []),
    ]
    return CommandRun(
        argv=tuple(argv),
        returncode=None if was_cancelled else process.returncode,
        output="\n".join(["".join(chunks), *notes]) if notes else "".join(chunks),
        seconds=time.monotonic() - started,
    )
