from __future__ import annotations

import base64
import json
import os
import selectors
import signal
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from http import HTTPStatus
from typing import IO
from urllib.parse import urlsplit

from .provider_environment import CLAUDE_PROFILE_OVERRIDES
from .remote_types import HttpReply, InvalidPayload, object_dict

MAX_BODY = 4 * 1024 * 1024
MAX_INPUT = 32 * 1024
MAX_FRAME = 6 * 1024 * 1024
HTTP_SECONDS = 15.0
FRAME_HEADER = 4
MAX_HEADERS = 4
MAX_HEADER_VALUE = 16000
MAX_REPLY_HEADER = 512
MAX_STDERR = 16384
MAX_HTTP_STATUS = 599
_ASCII_LOW = 32
_ASCII_HIGH = 126
_WORKER_ENVIRONMENT = frozenset(
    {
        "HOME",
        "TMPDIR",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        *CLAUDE_PROFILE_OVERRIDES,
    }
)
_ENDPOINTS = frozenset(
    {
        ("api.anthropic.com", "/api/oauth/usage"),
        ("openrouter.ai", "/api/v1/key"),
        ("openrouter.ai", "/api/v1/activity"),
        ("openrouter.ai", "/api/v1/credits"),
    }
)


class RemoteHttpError(Exception):
    def __init__(self, code: str = "http_error") -> None:
        self.code = code
        super().__init__(
            {
                "http_timeout": "Usage request exceeded its deadline.",
                "http_size": "Usage response exceeded the size limit.",
                "http_redirect": "Usage endpoint refused an unexpected redirect.",
                "http_endpoint": "Usage endpoint is not allowed.",
                "http_protocol": "Usage worker returned an invalid response.",
            }.get(code, "Usage request failed.")
        )


def validate_endpoint(url: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(url)
        allowed = (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and not parsed.query
            and not parsed.fragment
            and (parsed.hostname, parsed.path) in _ENDPOINTS
            and parsed.netloc == parsed.hostname
        )
    except ValueError as exc:
        raise RemoteHttpError("http_endpoint") from exc
    if not allowed or parsed.hostname is None:
        raise RemoteHttpError("http_endpoint")
    return parsed.hostname, parsed.path


def validate_headers(headers: Mapping[str, str]) -> dict[str, str]:
    if len(headers) > MAX_HEADERS:
        raise RemoteHttpError("http_protocol")
    clean = dict(headers)
    if any(
        key.lower() not in ("authorization", "anthropic-beta", "accept", "user-agent")
        or len(value) > MAX_HEADER_VALUE
        or not printable_ascii(value)
        for key, value in clean.items()
    ):
        raise RemoteHttpError("http_protocol")
    return clean


def printable_ascii(value: str) -> bool:
    return all(_ASCII_LOW <= ord(char) <= _ASCII_HIGH for char in value)


@contextmanager
def _termination_cleanup() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextmanager
def piped_child(
    argv: Sequence[str],
    stop: Callable[[subprocess.Popen[bytes]], None],
    *,
    environment: Mapping[str, str],
) -> Iterator[subprocess.Popen[bytes]]:
    pending: int | None = None

    def defer(signum: int, frame: object) -> None:
        nonlocal pending
        if pending is None:
            pending = signum

    with _termination_cleanup(), ExitStack() as cleanup:
        with ExitStack() as handlers:
            # Inherit the process group and defer direct signals until child cleanup is registered.
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous = signal.getsignal(signum)
                    if previous != signal.SIG_IGN:
                        handlers.callback(signal.signal, signum, previous)
                        signal.signal(signum, defer)
            child = subprocess.Popen(
                list(argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            cleanup.callback(stop, child)
        if pending is not None:
            signal.raise_signal(pending)
        yield child


def _stop_worker(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        child.kill()
    child.wait()
    for pipe in (child.stdin, child.stdout, child.stderr):
        if pipe is not None:
            pipe.close()


def run_worker(
    argv: Sequence[str],
    request: bytes,
    *,
    deadline: float,
    max_output: int = MAX_FRAME + 4,
    environment: Mapping[str, str] | None = None,
) -> bytes:
    if len(request) > MAX_INPUT + 4:
        raise RemoteHttpError("http_protocol")
    if deadline <= time.monotonic():
        raise RemoteHttpError("http_timeout")
    if environment is None:
        environment = {
            name: value for name, value in os.environ.items() if name in _WORKER_ENVIRONMENT
        }
    try:
        with piped_child(argv, _stop_worker, environment=environment) as child:
            return _exchange(child, request, deadline, max_output)
    except OSError as exc:
        raise RemoteHttpError from exc


def _write_input(pipe: IO[bytes], request: bytes, written: int) -> int:
    try:
        return written + os.write(pipe.fileno(), request[written:])
    except BrokenPipeError:
        raise RemoteHttpError("http_protocol") from None


def _exchange(
    child: subprocess.Popen[bytes], request: bytes, deadline: float, max_output: int
) -> bytes:
    assert child.stdin is not None and child.stdout is not None and child.stderr is not None
    output = bytearray()
    written, stderr_bytes = 0, 0
    with selectors.DefaultSelector() as selector:
        for pipe, events in (
            (child.stdin, selectors.EVENT_WRITE),
            (child.stdout, selectors.EVENT_READ),
            (child.stderr, selectors.EVENT_READ),
        ):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, events)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RemoteHttpError("http_timeout")
            ready = selector.select(remaining)
            if not ready:
                raise RemoteHttpError("http_timeout")
            for key, _ in ready:
                selected = key.fileobj
                if selected is child.stdin:
                    written = _write_input(child.stdin, request, written)
                    if written == len(request):
                        selector.unregister(child.stdin)
                        child.stdin.close()
                    continue
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(selected)
                    continue
                if selected is child.stdout:
                    output.extend(chunk)
                    if len(output) > max_output:
                        raise RemoteHttpError("http_size")
                else:
                    stderr_bytes += len(chunk)
                    if stderr_bytes > MAX_STDERR:
                        raise RemoteHttpError("http_protocol")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RemoteHttpError("http_timeout")
        try:
            code = child.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise RemoteHttpError("http_timeout") from exc
        if code != 0:
            raise RemoteHttpError
    return bytes(output)


def get(url: str, headers: Mapping[str, str], *, deadline: float) -> HttpReply:
    validate_endpoint(url)
    payload = json.dumps(
        {"url": url, "headers": validate_headers(headers)}, allow_nan=False
    ).encode()
    request = struct.pack("!I", len(payload)) + payload
    response = run_worker(
        [sys.executable, "-I", "-m", "ringleader.usage.remote_http_worker"],
        request,
        deadline=min(deadline, time.monotonic() + HTTP_SECONDS),
    )
    try:
        if (
            len(response) < FRAME_HEADER
            or len(response) - FRAME_HEADER != struct.unpack("!I", response[:FRAME_HEADER])[0]
        ):
            raise InvalidPayload
        raw = object_dict(json.loads(response[4:]))
        if "error" in raw:
            code = raw["error"]
            raise RemoteHttpError(code if code in ("http_redirect", "http_size") else "http_error")
        status, body, response_headers = raw["status"], raw["body"], object_dict(raw["headers"])
        if (
            type(status) is not int
            or not HTTPStatus.CONTINUE <= status <= MAX_HTTP_STATUS
            or not isinstance(body, str)
        ):
            raise InvalidPayload
        if HTTPStatus.MULTIPLE_CHOICES <= status < HTTPStatus.BAD_REQUEST:
            raise RemoteHttpError("http_redirect")
        decoded = base64.b64decode(body, validate=True)
        if len(decoded) > MAX_BODY or any(
            key not in ("retry-after", "content-type")
            or not isinstance(value, str)
            or len(value) > MAX_REPLY_HEADER
            or not printable_ascii(value)
            for key, value in response_headers.items()
        ):
            raise InvalidPayload
        return HttpReply(
            status, {key: str(value) for key, value in response_headers.items()}, decoded
        )
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        raise RemoteHttpError("http_protocol") from exc
