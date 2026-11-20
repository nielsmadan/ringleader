from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import subprocess
import time
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from importlib.metadata import version

from . import remote_http
from .provider_environment import REPORT_CREDENTIALS
from .remote_types import (
    MAX_WINDOWS,
    Diagnostic,
    InvalidPayload,
    QuotaWindow,
    SubscriptionData,
    count,
    fingerprint,
    number,
    object_dict,
    opaque_identity,
    optional_string,
    safe_string,
)

CODEX_SECONDS = 20.0
MIN_VERSION = (0, 170, 0)
MAX_LINE = 1024 * 1024
MAX_OUTPUT = 2 * MAX_LINE
MAX_STDERR = 16 * 1024
MAX_MESSAGES = 256
CHUNK_BYTES = 16 * 1024
QUIT_GRACE = 0.2
METHOD_NOT_FOUND = -32601
_AUTH_ERRORS = frozenset(
    {
        "codex account authentication required to read rate limits",
        "chatgpt authentication required to read rate limits",
    }
)


class CodexUsageError(Exception):
    def __init__(self, code: str) -> None:
        self.diagnostic = Diagnostic(
            code,
            {
                "not_configured": "Install Codex CLI 0.170.0 or newer to read Codex subscription usage.",
                "unsupported_version": "Codex subscription usage requires Codex CLI 0.170.0 or newer.",
                "auth_denied": "Sign in to Codex with a ChatGPT account to read its subscription usage.",
                "unsupported_method": "This Codex app-server cannot read subscription rate limits.",
                "codex_timeout": "Codex subscription lookup exceeded its deadline.",
                "codex_output_limit": "Codex app-server exceeded its output limit.",
                "codex_protocol": "Codex app-server returned an invalid usage response.",
            }.get(code, "Codex app-server could not read subscription usage."),
        )
        super().__init__(self.diagnostic.message)


def _version_probe(executable: str, deadline: float, environment: Mapping[str, str]) -> None:
    try:
        output = remote_http.run_worker(
            [executable, "--version"],
            b"",
            deadline=deadline,
            max_output=1024,
            environment=environment,
        )
    except remote_http.RemoteHttpError as exc:
        code = "codex_timeout" if exc.code == "http_timeout" else "unsupported_version"
        raise CodexUsageError(code) from None
    match = re.fullmatch(
        rb"codex(?:-cli)? ([0-9]{1,5})\.([0-9]{1,5})\.([0-9]{1,5})(?:\+[A-Za-z0-9.-]+)?",
        output.strip(),
    )
    if match is None or tuple(int(part) for part in match.groups()) < MIN_VERSION:
        raise CodexUsageError("unsupported_version")


def _rpc_error(value: object) -> CodexUsageError:
    error = object_dict(value)
    code = error.get("code")
    message = error.get("message")
    if type(code) is not int or not isinstance(message, str):
        return CodexUsageError("codex_protocol")
    if code == METHOD_NOT_FOUND:
        return CodexUsageError("unsupported_method")
    return CodexUsageError("auth_denied" if message in _AUTH_ERRORS else "codex_error")


class _Stdio:
    def __init__(
        self, child: subprocess.Popen[bytes], selector: selectors.BaseSelector, deadline: float
    ) -> None:
        assert child.stdin is not None and child.stdout is not None and child.stderr is not None
        self._input = child.stdin
        self._output_fd = child.stdout.fileno()
        self._selector = selector
        self._deadline = deadline
        self._buffer = bytearray()
        self._stdout_bytes = self._stderr_bytes = self._messages = 0
        for pipe in (child.stdin, child.stdout, child.stderr):
            os.set_blocking(pipe.fileno(), False)
        for pipe in (child.stdout, child.stderr):
            selector.register(pipe, selectors.EVENT_READ)

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise CodexUsageError("codex_timeout")
        return remaining

    def send(self, message: dict[str, object]) -> None:
        self._remaining()
        payload = json.dumps(message, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if os.write(self._input.fileno(), payload) != len(payload):
            raise CodexUsageError("codex_protocol")

    def _receive(self, fd: int) -> None:
        chunk = os.read(fd, CHUNK_BYTES)
        if not chunk:
            self._selector.unregister(fd)
            if fd == self._output_fd:
                raise CodexUsageError("codex_protocol")
        elif fd == self._output_fd:
            self._stdout_bytes += len(chunk)
            if self._stdout_bytes > MAX_OUTPUT:
                raise CodexUsageError("codex_output_limit")
            self._buffer.extend(chunk)
        else:
            self._stderr_bytes += len(chunk)
            if self._stderr_bytes > MAX_STDERR:
                raise CodexUsageError("codex_output_limit")

    def _line(self) -> bytes:
        while True:
            remaining = self._remaining()
            end = self._buffer.find(b"\n")
            if end > MAX_LINE or (end < 0 and len(self._buffer) > MAX_LINE):
                raise CodexUsageError("codex_output_limit")
            if end >= 0:
                line = bytes(self._buffer[:end])
                del self._buffer[: end + 1]
                return line
            for selected, _ in self._selector.select(remaining):
                self._receive(selected.fd)

    def reply(self, expected_id: int) -> dict[str, object]:
        while self._messages < MAX_MESSAGES:
            self._messages += 1
            message = object_dict(json.loads(self._line()))
            if "method" in message:
                if "id" in message or not isinstance(message["method"], str):
                    raise CodexUsageError("codex_protocol")
                continue
            request_id = message.get("id")
            if type(request_id) is not int:
                raise CodexUsageError("codex_protocol")
            if request_id != expected_id:
                continue
            if ("result" in message) == ("error" in message):
                raise CodexUsageError("codex_protocol")
            if "error" in message:
                raise _rpc_error(message["error"])
            return object_dict(message["result"])
        raise CodexUsageError("codex_output_limit")


def _stop(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        with suppress(ProcessLookupError):
            child.terminate()
        try:
            child.wait(timeout=QUIT_GRACE)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                child.kill()
            child.wait()
    for pipe in (child.stdin, child.stdout, child.stderr):
        if pipe is not None:
            pipe.close()


def _query(executable: str, deadline: float, environment: Mapping[str, str]) -> dict[str, object]:
    if deadline <= time.monotonic():
        raise CodexUsageError("codex_timeout")
    argv = [executable, "app-server", "--listen", "stdio://", "-c", "analytics.enabled=false"]
    with (
        remote_http.piped_child(argv, _stop, environment=environment) as child,
        selectors.DefaultSelector() as selector,
    ):
        stdio = _Stdio(child, selector, deadline)
        stdio.send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "ringleader-subscription",
                        "version": version("ringleader"),
                    },
                    "capabilities": {
                        "experimentalApi": False,
                        "requestAttestation": False,
                        "explicitGatewayOauth": True,
                    },
                },
            }
        )
        stdio.reply(1)
        stdio.send({"method": "initialized"})
        stdio.send(
            {
                "id": 2,
                "method": "account/rateLimits/read",
                "params": {
                    "excludeResetCreditDetails": True,
                },
            }
        )
        return stdio.reply(2)


def read_rate_limits() -> dict[str, object]:
    deadline = time.monotonic() + CODEX_SECONDS
    executable = shutil.which("codex")
    if executable is None:
        raise CodexUsageError("not_configured")
    environment = {
        name: value for name, value in os.environ.items() if name not in REPORT_CREDENTIALS
    }
    try:
        _version_probe(executable, deadline, environment)
        return _query(executable, deadline, environment)
    except (ValueError, TypeError, RecursionError):
        raise CodexUsageError("codex_protocol") from None
    except OSError:
        raise CodexUsageError("codex_error") from None


def _reset_at(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not int:
        raise InvalidPayload
    return datetime.fromtimestamp(value, tz=UTC).isoformat()


def _window(bucket: str, label: str, slot: str, value: object) -> QuotaWindow:
    raw = object_dict(value)
    duration = raw.get("windowDurationMins")
    return QuotaWindow(
        f"{bucket}:{slot}",
        f"{label} {slot}",
        number(count(raw.get("usedPercent"))),
        None if duration is None else count(duration),
        _reset_at(raw.get("resetsAt")),
    )


def _buckets(raw: dict[str, object]) -> dict[str, object]:
    by_id = raw.get("rateLimitsByLimitId")
    mapped = {} if by_id is None else object_dict(by_id)
    if mapped:
        return mapped
    legacy = object_dict(raw.get("rateLimits"))
    limit_id = legacy.get("limitId")
    if limit_id is None or limit_id == "":
        limit_id = "codex"
    if not isinstance(limit_id, str):
        raise InvalidPayload
    return {limit_id: legacy}


def parse_usage(payload: object, *, profile: str) -> tuple[str | None, SubscriptionData]:
    try:
        raw = object_dict(payload)
        account_id = optional_string(raw.get("accountId"))
        if raw.get("accountId") != account_id:
            raise InvalidPayload
        scope = fingerprint("codex", profile, account_id) if account_id else None
        identity = opaque_identity(scope, account=True) if scope else None
        secrets = (account_id,) if account_id else ()
        windows: list[QuotaWindow] = []
        plans = set()
        buckets = _buckets(raw)
        if len(buckets) > MAX_WINDOWS:
            raise InvalidPayload
        for limit_id, value in buckets.items():
            bucket = safe_string(limit_id, secrets=secrets, limit=128)
            if not bucket or bucket != limit_id:
                raise InvalidPayload
            entry = object_dict(value)
            label = optional_string(entry.get("limitName"), secrets=secrets) or bucket
            label = safe_string(label, secrets=secrets, limit=480)
            plan = optional_string(entry.get("planType"), secrets=secrets)
            if plan:
                plans.add(plan)
            windows.extend(
                _window(bucket, label, slot, window)
                for slot in ("primary", "secondary")
                if (window := entry.get(slot)) is not None
            )
        if len(windows) > MAX_WINDOWS:
            raise InvalidPayload
        return scope, SubscriptionData(
            next(iter(plans)) if len(plans) == 1 else None, identity, tuple(windows)
        )
    except (ValueError, TypeError, OverflowError, OSError, RecursionError):
        raise InvalidPayload from None
