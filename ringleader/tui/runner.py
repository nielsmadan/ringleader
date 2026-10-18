from __future__ import annotations

import codecs
import io
import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass

from ..progress import CancelCheck

POLL_SECONDS = 0.05
TERM_GRACE_SECONDS = 2.0
READER_JOIN_SECONDS = 2.0
READ_BYTES = 65536
TIMEOUT_SECONDS = 600.0
MAX_OUTPUT_CHARS = 2_000_000


@dataclass(frozen=True)
class CommandRun:
    argv: tuple[str, ...]
    returncode: int | None
    output: str
    seconds: float


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(TERM_GRACE_SECONDS)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def _drain(fd: int, stop: threading.Event, sink: Callable[[str], None]) -> None:
    decoder = io.IncrementalNewlineDecoder(
        codecs.getincrementaldecoder("utf-8")(errors="replace"), translate=True
    )
    while not stop.is_set():
        ready, _, _ = select.select([fd], [], [], POLL_SECONDS)
        if not ready:
            continue
        data = os.read(fd, READ_BYTES)
        if not data:
            break
        sink(decoder.decode(data))
    sink(decoder.decode(b"", final=True))


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
    read_fd, write_fd = os.pipe()
    try:
        process = subprocess.Popen(
            [python or sys.executable, "-P", "-m", "ringleader", *argv],
            stdout=write_fd,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=child_env,
        )
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    chunks: list[str] = []
    kept = 0
    truncated = threading.Event()
    stop = threading.Event()

    def keep(chunk: str) -> None:
        nonlocal kept
        room = max(0, MAX_OUTPUT_CHARS - kept)
        if len(chunk) > room:
            truncated.set()
        if room and chunk:
            chunks.append(chunk[:room])
            kept += len(chunks[-1])

    reader = threading.Thread(
        target=_drain, args=(read_fd, stop, keep), name="rl-run-reader", daemon=True
    )
    reader.start()
    was_cancelled = False
    timed_out = False
    try:
        while process.poll() is None:
            timed_out = time.monotonic() - started >= timeout
            if cancelled() or timed_out:
                was_cancelled = True
                _stop_group(process)
                break
            time.sleep(POLL_SECONDS)
        process.wait()
        reader.join(READER_JOIN_SECONDS)
    finally:
        stop.set()
        reader.join()
        os.close(read_fd)
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
