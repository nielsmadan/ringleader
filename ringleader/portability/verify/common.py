from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import native
from ..capabilities import NativeRoots
from ..model import PreservationError

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
REPR_LIMIT = 600


@dataclass(frozen=True)
class HarnessResult:
    version: str
    summary: str
    schema_identity: str | None = None


class VerificationFailed(PreservationError):
    pass


def require(condition: object, label: str) -> None:
    if not condition:
        raise VerificationFailed(f"self-verification failed: {label}")


def require_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        detail = repr({"expected": expected, "actual": actual})
        if len(detail) > REPR_LIMIT:
            detail = detail[:REPR_LIMIT] + "..."
        raise VerificationFailed(f"self-verification failed: {label}: {detail}")


def require_outside_checkout(workdir: Path) -> Path:
    resolved = workdir.resolve()
    if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
        raise VerificationFailed(
            f"self-verification workdir must be outside any git checkout: {resolved}"
        )
    return resolved


def isolated_roots(base: Path) -> NativeRoots:
    roots = NativeRoots(
        base / "home", base / "store", base / "cwd", base / "config", base / "cache"
    )
    for path in (
        roots.home,
        roots.store,
        roots.cwd,
        roots.config,
        roots.cache / "tmp",
        roots.home / "data",
        roots.home / "state",
    ):
        path.mkdir(parents=True, exist_ok=True)
    return roots


def environment(
    roots: NativeRoots, agent: str, extra: dict[str, str] | None = None
) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "en_US.UTF-8",
        **dict(native.env(roots, agent)),
        **(extra or {}),
    }


def run_native(
    argv: list[str], roots: NativeRoots, agent: str, extra: dict[str, str] | None = None
) -> str:
    try:
        result = subprocess.run(
            argv,
            cwd=roots.cwd,
            env=environment(roots, agent, extra),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerificationFailed(f"self-verification failed: {argv[0]}: {exc}") from exc
    if result.returncode:
        raise VerificationFailed(
            f"self-verification failed: {Path(argv[0]).name} {' '.join(argv[1:2])} exited "
            f"{result.returncode}: {(result.stdout + result.stderr).strip()[-REPR_LIMIT:]}"
        )
    return result.stdout.strip()


@dataclass(frozen=True)
class Capture:
    url: str
    requests: queue.Queue[tuple[str, dict[str, str], bytes]]


@contextmanager
def loopback(error: dict[str, Any]) -> Iterator[Capture]:
    from http.server import (  # noqa: PLC0415 -- heavy; verify-only
        BaseHTTPRequestHandler,
        ThreadingHTTPServer,
    )

    requests: queue.Queue[tuple[str, dict[str, str], bytes]] = queue.Queue()
    response = json.dumps(error).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            requests.put((self.path, dict(self.headers), body))
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Capture(f"http://127.0.0.1:{server.server_port}", requests)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
