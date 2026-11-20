import io
import json
import os
import signal
import struct
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from ringleader.usage import remote_http, remote_http_worker
from ringleader.usage.remote_http import (
    MAX_BODY,
    RemoteHttpError,
    get,
    run_worker,
    validate_endpoint,
)

URL = "https://api.anthropic.com/api/oauth/usage"


class FakeResponse:
    def __init__(self, body=b"{}", status=200, headers=None):
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, count):
        return self.body.read(count)

    def getheader(self, name):
        return self.headers.get(name)


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.closed = False
        self.requested = None

    def request(self, method, path, *, headers):
        self.requested = method, path, headers

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def test_http_worker_only_requests_validated_fixed_get_endpoint():
    connection = FakeConnection(FakeResponse(b'{"zero":0}', headers={"retry-after": "60"}))
    reply = remote_http_worker.fetch(
        URL, {"Authorization": "Bearer fake-secret"}, connect=lambda _: connection
    )
    assert (reply.status, reply.body, dict(reply.headers)) == (
        200,
        b'{"zero":0}',
        {"retry-after": "60"},
    )
    assert connection.requested == (
        "GET",
        "/api/oauth/usage",
        {"Authorization": "Bearer fake-secret"},
    )
    assert connection.closed is True


@pytest.mark.parametrize(
    "url",
    [
        "http://api.anthropic.com/api/oauth/usage",
        "https://example.test/api/oauth/usage",
        "https://api.anthropic.com/api/oauth/usage?token=fake-secret",
        "https://api.anthropic.com:443/api/oauth/usage",
        "https://user@api.anthropic.com/api/oauth/usage",
        "https://openrouter.ai/api/v1/chat/completions",
        "https://api.anthropic.com/API/oauth/usage",
    ],
)
def test_parent_and_worker_reject_unapproved_endpoint_without_transport(url):
    with pytest.raises(RemoteHttpError, match="not allowed"):
        validate_endpoint(url)
    with pytest.raises(RemoteHttpError, match="not allowed"):
        remote_http_worker.fetch(url, {}, connect=lambda _: pytest.fail("connection attempted"))
    with pytest.raises(RemoteHttpError, match="not allowed"):
        get(url, {}, deadline=time.monotonic() + 1)


def test_http_redirect_is_refused_and_body_size_is_bounded():
    for response, code in (
        (FakeResponse(status=302), "http_redirect"),
        (FakeResponse(body=b"x" * (MAX_BODY + 1)), "http_size"),
    ):
        connection = FakeConnection(response)
        with pytest.raises(RemoteHttpError) as error:
            remote_http_worker.fetch(URL, {}, connect=lambda _, connection=connection: connection)
        assert error.value.code == code
        assert connection.closed is True


@pytest.mark.parametrize("status", [429, 503])
def test_http_worker_keeps_retry_after_without_reading_error_body(status):
    class ThrottledResponse(FakeResponse):
        def read(self, count):
            pytest.fail("worker read the unused error response body")

    connection = FakeConnection(ThrottledResponse(status=status, headers={"retry-after": "90"}))
    reply = remote_http_worker.fetch(URL, {}, connect=lambda _: connection)
    assert (reply.status, reply.body, reply.headers) == (status, b"", {"retry-after": "90"})
    assert connection.closed is True


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer fake\nsecret"},
        {"Authorization": "a" * 16001},
        {"Cookie": "fake-secret"},
    ],
)
def test_http_rejects_unsafe_headers_before_connection(headers):
    with pytest.raises(RemoteHttpError):
        remote_http_worker.fetch(
            URL, headers, connect=lambda _: pytest.fail("connection attempted")
        )


def test_parent_passes_secret_in_private_frame_not_argv(monkeypatch):
    observed = {}

    def fake_worker(argv, request, *, deadline):
        observed.update(argv=argv, request=request, deadline=deadline)
        encoded = json.dumps({"status": 200, "headers": {}, "body": "e30="}).encode()
        return struct.pack("!I", len(encoded)) + encoded

    monkeypatch.setattr(remote_http, "run_worker", fake_worker)
    now = time.monotonic()
    reply = get(URL, {"Authorization": "Bearer fake-secret"}, deadline=now + 50)
    assert reply.body == b"{}"
    assert observed["argv"] == [sys.executable, "-I", "-m", "ringleader.usage.remote_http_worker"]
    assert json.loads(observed["request"][4:])["headers"] == {"Authorization": "Bearer fake-secret"}
    assert now < observed["deadline"] <= time.monotonic() + 15


def test_internal_worker_receives_only_required_environment(monkeypatch):
    monkeypatch.setenv("HOME", "/fixture-home")
    monkeypatch.setenv("SSL_CERT_FILE", "/fixture-certificates")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/fixture-profile")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fixture-secret")
    monkeypatch.setenv("UNRELATED_CREDENTIAL", "fixture-secret")
    script = (
        "import os;"
        "assert 'CLAUDE_CODE_OAUTH_TOKEN' not in os.environ;"
        "assert 'UNRELATED_CREDENTIAL' not in os.environ;"
        "print(os.environ['HOME'],os.environ['SSL_CERT_FILE'],"
        "os.environ['CLAUDE_SECURESTORAGE_CONFIG_DIR'])"
    )
    assert (
        run_worker([sys.executable, "-I", "-c", script], b"", deadline=time.monotonic() + 2)
        == b"/fixture-home /fixture-certificates /fixture-profile\n"
    )


@pytest.mark.parametrize(
    "output", [b"", b"secret", struct.pack("!I", 2) + b"[]", struct.pack("!I", 2) + b"{}"]
)
def test_invalid_worker_frame_has_safe_diagnostic(monkeypatch, output):
    monkeypatch.setattr(remote_http, "run_worker", lambda *_, **__: output)
    with pytest.raises(RemoteHttpError) as error:
        get(URL, {}, deadline=time.monotonic() + 1)
    assert str(error.value) == "Usage worker returned an invalid response."


def test_real_worker_subprocess_ignores_shadow_package_and_revalidates_endpoint(
    monkeypatch, tmp_path
):
    shadow = tmp_path / "ringleader" / "usage"
    shadow.mkdir(parents=True)
    (shadow.parent / "__init__.py").touch()
    (shadow / "__init__.py").touch()
    captured = tmp_path / "captured"
    (shadow / "remote_http_worker.py").write_text(
        "import pathlib, sys\n"
        "if b'Bearer fake-secret' in sys.stdin.buffer.read():\n"
        "    pathlib.Path('captured').touch()\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    encoded = json.dumps(
        {"url": "https://not-allowed.test/", "headers": {"Authorization": "Bearer fake-secret"}}
    ).encode()
    output = run_worker(
        [sys.executable, "-I", "-m", "ringleader.usage.remote_http_worker"],
        struct.pack("!I", len(encoded)) + encoded,
        deadline=time.monotonic() + 5,
    )
    assert json.loads(output[4:]) == {"error": "http_endpoint"}
    assert not captured.exists()


@pytest.mark.parametrize(
    "script,code",
    [
        ("import time;time.sleep(20)", "http_timeout"),
        ("import os;os.write(1,b'x'*70000)", "http_size"),
        ("import os;os.write(2,b'fake-secret'*3000)", "http_protocol"),
    ],
)
def test_actual_subprocess_timeout_and_flood_are_killed_and_reaped(monkeypatch, script, code):
    children = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(remote_http.subprocess, "Popen", spawn)
    start = time.monotonic()
    with pytest.raises(RemoteHttpError) as error:
        run_worker(
            [sys.executable, "-c", script], b"request", deadline=start + 0.3, max_output=1024
        )
    assert error.value.code == code
    assert time.monotonic() - start < 2
    assert children[0].returncode is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0].pid, os.WNOHANG)


def test_worker_parent_sigterm_cleans_up_nested_worker(tmp_path):
    pidfile = tmp_path / "worker.pid"
    child_script = (
        "import os,time,pathlib;pathlib.Path("
        + repr(str(pidfile))
        + ").write_text(str(os.getpid()));time.sleep(20)"
    )
    parent_script = (
        "import sys,time;from ringleader.usage.remote_http import run_worker;"
        "run_worker([sys.executable,'-c',"
        + repr(child_script)
        + "],b'request',deadline=time.monotonic()+10)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", parent_script],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        until = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert pidfile.exists()
        worker_pid = int(pidfile.read_text())
        os.killpg(child.pid, signal.SIGTERM)
        child.communicate(timeout=3)
        assert child.returncode != 0
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
        child.communicate(timeout=3)


def test_worker_framed_fixture_reads_response_without_network(monkeypatch):
    encoded = json.dumps({"url": URL, "headers": {"Authorization": "Bearer fake-secret"}}).encode()
    output = io.BytesIO()
    monkeypatch.setattr(
        remote_http_worker.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(struct.pack("!I", len(encoded)) + encoded)),
    )
    monkeypatch.setattr(remote_http_worker.sys, "stdout", SimpleNamespace(buffer=output))
    monkeypatch.setattr(
        remote_http_worker, "fetch", lambda *_: remote_http.HttpReply(200, {}, b"{}")
    )
    assert remote_http_worker.main() == 0
    assert json.loads(output.getvalue()[4:]) == {"status": 200, "headers": {}, "body": "e30="}


@pytest.mark.parametrize(
    "mode,code",
    [("stall", "http_timeout"), ("oversize", "http_size"), ("redirect", "http_redirect")],
)
def test_real_http_worker_against_loopback_fixture_obeys_bounds(tmp_path, mode, code):
    import socket
    import threading

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve():
        try:
            with listener.accept()[0] as connection:
                connection.settimeout(5)
                connection.recv(32768)
                if mode == "redirect":
                    connection.sendall(
                        b"HTTP/1.1 302 Found\r\nLocation: https://example.test/\r\nContent-Length: 0\r\n\r\n"
                    )
                elif mode == "stall":
                    connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nx")
                    stop.wait(5)
                else:
                    connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4194305\r\n\r\n")
                    connection.sendall(b"x" * (MAX_BODY + 1))
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            listener.close()

    server = threading.Thread(target=serve)
    server.start()
    driver = tmp_path / "worker_driver.py"
    driver.write_text(
        "import functools,http.client\n"
        "from ringleader.usage import remote_http_worker as worker\n"
        f"connect=lambda _:http.client.HTTPConnection('127.0.0.1',{port},timeout=5)\n"
        "worker.fetch=functools.partial(worker.fetch,connect=connect)\n"
        "raise SystemExit(worker.main())\n"
    )
    encoded = json.dumps({"url": URL, "headers": {"Authorization": "Bearer fake-secret"}}).encode()
    try:
        if mode == "stall":
            with pytest.raises(RemoteHttpError) as error:
                run_worker(
                    [sys.executable, str(driver)],
                    struct.pack("!I", len(encoded)) + encoded,
                    deadline=time.monotonic() + 0.5,
                )
            assert error.value.code == code
        else:
            reply = run_worker(
                [sys.executable, str(driver)],
                struct.pack("!I", len(encoded)) + encoded,
                deadline=time.monotonic() + 5,
            )
            assert json.loads(reply[4:]) == {"error": code}
    finally:
        stop.set()
        server.join(6)
        assert server.is_alive() is False
