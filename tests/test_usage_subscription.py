import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from ringleader import commands
from ringleader.cli import main
from ringleader.config import Config, save_config
from ringleader.tui.runner import run_rl
from ringleader.usage import claude_credentials, openrouter, remote_http, subscription
from ringleader.usage import codex_subscription as codex
from ringleader.usage.remote_cache import RemoteCache
from ringleader.usage.remote_types import HttpReply, InvalidPayload, QuotaWindow, ReportMode

NOW = datetime(2026, 11, 19, 12, tzinfo=UTC)
TOKEN = "fixture-claude-access-token-456"
ACCOUNT = "private-fixture-codex-account-123"
BODY = b'{"five_hour":{"utilization":0,"resets_at":null}}'
CODEX_BODY = {
    "accountId": ACCOUNT,
    "rateLimits": {"primary": {"usedPercent": 33}},
    "rateLimitsByLimitId": {
        "codex": {
            "planType": "plus",
            "limitName": "Codex main",
            "primary": {
                "usedPercent": 0,
                "windowDurationMins": 480,
                "resetsAt": 1800000000,
            },
            "secondary": {"usedPercent": 66, "resetsAt": None},
        },
        "review": {
            "planType": "plus",
            "primary": {"usedPercent": 115, "windowDurationMins": 30},
            "credits": {"hasCredits": True, "balance": "1234.50"},
        },
    },
}

_FAKE_CODEX = """
import json
import os
import signal
import sys
import time
from pathlib import Path

mode, root = sys.argv[1:3]
root = Path(root)
args = sys.argv[3:]
if mode == "environment":
    assert os.environ["CODEX_HOME"] == str(root / "fixture-codex-home")
    names = json.loads((root / "credential-vars.json").read_text())
    assert all(name not in os.environ for name in names)
if args == ["--version"]:
    (root / "version").write_text("called")
    if mode == "slow_version":
        time.sleep(60)
    print({"old": "codex-cli 0.169.99", "unknown_version": "unexpected-output",
           "prerelease": "codex-cli 0.170.0-alpha.1"}.get(mode, "codex-cli 0.170.0"))
    raise SystemExit(0)
assert args == ["app-server", "--listen", "stdio://", "-c", "analytics.enabled=false"], args
signal.signal(signal.SIGTERM, signal.SIG_IGN)
(root / "app-server.pid").write_text(str(os.getpid()))

def read():
    return json.loads(sys.stdin.buffer.readline())

def fragmented(message):
    data = json.dumps(message).encode() + b"\\n"
    os.write(1, data[:9])
    time.sleep(0.005)
    os.write(1, data[9:])

initialize = read()
assert initialize["method"] == "initialize"
assert initialize["params"]["clientInfo"]["name"] == "ringleader-subscription"
assert initialize["params"]["clientInfo"]["version"]
assert initialize["params"]["capabilities"] == {
    "experimentalApi": False, "requestAttestation": False, "explicitGatewayOauth": True
}
if mode == "stall":
    time.sleep(60)
if mode == "stdout_flood":
    os.write(1, b"x" * (1024 * 1024 + 64))
if mode == "stderr_flood":
    os.write(2, b"provider-fixture-secret" * 2048)
if mode == "bad_json":
    os.write(1, b'{"not-valid-json!"}\\n')
if mode == "unsolicited":
    fragmented({"id": "u1", "method": "account/chatgptAuthTokens/refresh"})
if mode == "messages_flood":
    for i in range(300):
        fragmented({"method": "irrelevant", "params": {"i": i}})
if mode == "auth":
    (root / "init_ack").write_text("called")
fragmented({"method": "account/rateLimits/updated", "params": {}})
fragmented({"id": 999, "result": {}})
fragmented({"id": initialize["id"], "result": {}})
initialized, quota = read(), read()
assert initialized == {"method": "initialized"}, initialized
assert quota["method"] == "account/rateLimits/read"
assert quota["params"] == {"excludeResetCreditDetails": True}
(root / "requests").write_text(json.dumps([initialize, initialized, quota]))
if mode in ("unknown_method", "auth", "other_error"):
    code = -32601 if mode == "unknown_method" else -32600
    message = (
        "chatgpt authentication required to read rate limits" if mode == "auth"
        else "provider-fixture-secret"
    )
    fragmented({"id": quota["id"], "error": {"code": code, "message": message}})
else:
    response = json.loads((root / "reply.json").read_text())
    fragmented({"id": quota["id"], "result": response})
time.sleep(60)
"""


def _executable(path, script):
    path.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {script}\n")
    path.chmod(0o700)


@pytest.fixture
def fake_codex(monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))

    def install(mode="ok", response=CODEX_BODY):
        source = tmp_path / "fake_codex.py"
        source.write_text(_FAKE_CODEX)
        (tmp_path / "reply.json").write_text(json.dumps(response))
        _executable(
            bin_dir / "codex",
            f'{shlex.quote(str(source))} {shlex.quote(mode)} {shlex.quote(str(tmp_path))} "$@"',
        )
        return tmp_path

    return install


def _cli(*args, timeout=5):
    return subprocess.run(
        [sys.executable, "-P", "-m", "ringleader", "usage", "subscription", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _no_process(pidfile):
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_codex_parser_prefers_metered_buckets_with_opaque_identity():
    scope, result = codex.parse_usage(CODEX_BODY, profile="profile")
    assert result.plan == "plus"
    assert result.account_label.startswith("account:")
    assert scope is not None and len(scope) == 64
    assert result.windows == (
        QuotaWindow("codex:primary", "Codex main primary", 0, 480, "2027-01-15T08:00:00+00:00"),
        QuotaWindow("codex:secondary", "Codex main secondary", 66, None, None),
        QuotaWindow("review:primary", "review primary", 115, 30, None),
    )
    assert ACCOUNT not in repr(result)


def test_codex_parser_falls_back_to_legacy_unknown_account_and_empty_quota():
    scope, result = codex.parse_usage(
        {"rateLimits": {"limitId": "legacy", "secondary": {"usedPercent": 1}}},
        profile="fixture",
    )
    assert scope is None and result.account_label is None and result.plan is None
    assert result.windows == (QuotaWindow("legacy:secondary", "legacy secondary", 1, None, None),)
    assert codex.parse_usage({"rateLimits": {}}, profile="fixture")[1].windows == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"rateLimits": {"primary": {"usedPercent": True}}},
        {"rateLimits": {"primary": {"usedPercent": 0.1}}},
        {"rateLimits": {"primary": {"usedPercent": -1}}},
        {"rateLimits": {"primary": {"usedPercent": 1, "resetsAt": True}}},
        {"rateLimits": {"primary": {"usedPercent": 1, "resetsAt": 10**100}}},
        {"rateLimits": {"primary": {"usedPercent": 1, "windowDurationMins": False}}},
        {"rateLimitsByLimitId": []},
        {"rateLimitsByLimitId": {"a": None}},
        {
            "rateLimitsByLimitId": {
                "codex": {"primary": {"usedPercent": 1}},
                "codex\x01": {"primary": {"usedPercent": 2}},
            }
        },
        {"rateLimits": {"limitId": "legacy\x01", "primary": {"usedPercent": 1}}},
        {"rateLimits": {"limitName": ACCOUNT}, "accountId": ACCOUNT},
        {"rateLimits": {}, "accountId": "control\x00with-raw-account"},
    ],
)
def test_codex_parser_rejects_malformed_recognized_fields(payload):
    with pytest.raises(InvalidPayload):
        codex.parse_usage(payload, profile="fixture")


def test_codex_children_keep_profile_with_unrelated_credentials_filtered(monkeypatch, fake_codex):
    root = fake_codex("environment")
    monkeypatch.setenv("CODEX_HOME", str(root / "fixture-codex-home"))
    names = sorted({claude_credentials.TOKEN_VAR, *openrouter.KEY_VARIABLES.values()})
    (root / "credential-vars.json").write_text(json.dumps(names))
    for name in names:
        monkeypatch.setenv(name, "fixture-only-token")
    save_config(Config(usage_codex_auth="agent"))
    result = _cli("--source", "codex", "--json")
    assert result.returncode == 0
    data = json.loads(result.stdout)["results"][0]["data"]
    assert data["windows"][0]["used_percent"] == 0


def test_codex_real_fake_stdio_and_cli_cache_account(fake_codex):
    root = fake_codex()
    save_config(Config(usage_codex_auth="agent"))
    online = _cli("--source", "codex", "--json")
    assert online.returncode == 0, online.stderr
    result = json.loads(online.stdout)["results"][0]
    assert result["provider"] == "codex"
    assert result["status"] == "ok" and result["cached"] is False
    assert result["identity"].startswith("account:")
    assert result["data"]["windows"][1]["resets_at"] is None
    assert ACCOUNT not in online.stdout
    assert ACCOUNT not in RemoteCache().path.read_text()
    assert [r["method"] for r in json.loads((root / "requests").read_text())] == [
        "initialize",
        "initialized",
        "account/rateLimits/read",
    ]
    _no_process(root / "app-server.pid")

    (root / "version").unlink()
    cached = _cli("--source", "codex", "--cached")
    assert cached.returncode == 0
    assert f"last fetched {result['identity']}" in cached.stdout
    assert not (root / "version").exists()
    second = _cli("--source", "codex", "--json")
    assert json.loads(second.stdout)["results"][0]["cached"] is False
    assert (root / "version").exists()

    fake_codex("other_error")
    failed = _cli("--source", "codex", "--json")
    assert failed.returncode == 1
    failure = json.loads(failed.stdout)["results"][0]
    assert (failure["status"], failure["identity"], failure["data"]) == ("error", None, None)
    assert "provider-fixture-secret" not in failed.stdout + failed.stderr


@pytest.mark.parametrize("mode", ["old", "unknown_version", "prerelease"])
def test_codex_unsupported_version_never_starts_app_server(fake_codex, mode):
    root = fake_codex(mode)
    with pytest.raises(codex.CodexUsageError) as error:
        codex.read_rate_limits()
    assert error.value.diagnostic.code == "unsupported_version"
    assert (root / "version").exists()
    assert not (root / "app-server.pid").exists()


def test_codex_missing_executable_is_unconfigured(fake_codex):
    with pytest.raises(codex.CodexUsageError) as error:
        codex.read_rate_limits()
    assert error.value.diagnostic.code == "not_configured"


@pytest.mark.parametrize(
    "mode,code",
    [
        ("stdout_flood", "codex_output_limit"),
        ("stderr_flood", "codex_output_limit"),
        ("messages_flood", "codex_output_limit"),
        ("bad_json", "codex_protocol"),
        ("unsolicited", "codex_protocol"),
        ("unknown_method", "unsupported_method"),
        ("auth", "auth_denied"),
        ("other_error", "codex_error"),
    ],
)
def test_codex_real_fake_stdio_failures_reap_and_hide_upstream_text(fake_codex, mode, code):
    root = fake_codex(mode)
    with pytest.raises(codex.CodexUsageError) as error:
        codex.read_rate_limits()
    assert error.value.diagnostic.code == code
    assert "provider-fixture-secret" not in str(error.value)
    _no_process(root / "app-server.pid")


@pytest.mark.parametrize("mode", ["slow_version", "stall"])
def test_codex_entire_process_chain_obeys_deadline(monkeypatch, fake_codex, mode):
    root = fake_codex(mode)
    monkeypatch.setattr(codex, "CODEX_SECONDS", 0.45)
    start = time.monotonic()
    with pytest.raises(codex.CodexUsageError) as error:
        codex.read_rate_limits()
    assert error.value.diagnostic.code == "codex_timeout"
    assert (root / "version").exists()
    assert time.monotonic() - start < 2
    if mode == "stall":
        _no_process(root / "app-server.pid")


def test_codex_query_uses_budget_remaining_after_version_probe(monkeypatch):
    starts = []
    current = 100.0
    monkeypatch.setattr(codex, "time", SimpleNamespace(monotonic=lambda: current))
    monkeypatch.setattr(codex.shutil, "which", lambda _: "fixture-codex")

    def probe(executable, deadline, environment):
        nonlocal current
        starts.append((executable, deadline - current))
        current += codex.CODEX_SECONDS / 2

    def query(executable, deadline, environment):
        starts.append((executable, deadline - current))
        return CODEX_BODY

    monkeypatch.setattr(codex, "_version_probe", probe)
    monkeypatch.setattr(codex, "_query", query)
    assert codex.read_rate_limits() == CODEX_BODY
    assert starts == [
        ("fixture-codex", codex.CODEX_SECONDS),
        ("fixture-codex", codex.CODEX_SECONDS / 2),
    ]


def test_real_runner_cancels_codex_worker(fake_codex):
    root = fake_codex("stall")
    save_config(Config(usage_codex_auth="agent"))
    pidfile = root / "app-server.pid"
    run = run_rl(
        ["usage", "subscription", "--source", "codex", "--refresh", "--json"],
        cancelled=pidfile.exists,
        columns=80,
        timeout=5,
    )
    assert pidfile.exists()
    assert run.returncode is None
    assert "provider-fixture-secret" not in run.output
    _no_process(pidfile)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_direct_cli_signals_reap_codex_worker(fake_codex, signum):
    root = fake_codex("stall")
    save_config(Config(usage_codex_auth="agent"))
    pidfile = root / "app-server.pid"
    with subprocess.Popen(
        [sys.executable, "-P", "-m", "ringleader", "usage", "subscription", "--source", "codex"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        try:
            deadline = time.monotonic() + 4
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert pidfile.exists()
            proc.send_signal(signum)
            _, stderr = proc.communicate(timeout=3)
            assert proc.returncode != 0
            assert b"provider-fixture-secret" not in stderr
            _no_process(pidfile)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)


@pytest.mark.parametrize(
    "signum,expected_error", [(signal.SIGINT, KeyboardInterrupt), (signal.SIGTERM, SystemExit)]
)
@pytest.mark.parametrize("client", ["worker", "codex"])
def test_direct_signals_reap_children_during_startup(monkeypatch, signum, expected_error, client):
    original = subprocess.Popen
    children = []
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]

    def interrupted_spawn(_argv, **options):
        child = original(argv, **options)
        children.append(child)
        os.kill(os.getpid(), signum)
        return child

    monkeypatch.setattr(subprocess, "Popen", interrupted_spawn)
    try:
        with pytest.raises(expected_error) as caught:
            deadline = time.monotonic() + 2
            if client == "worker":
                remote_http.run_worker(argv, b"", deadline=deadline)
            else:
                codex._query(sys.executable, deadline, {})
        if signum == signal.SIGTERM:
            assert caught.value.code == 128 + signum
        assert len(children) == 1
        child = children[0]
        assert child.returncode in (-signal.SIGTERM, -signal.SIGKILL)
        assert all(pipe.closed for pipe in (child.stdin, child.stdout, child.stderr))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
            for pipe in (child.stdin, child.stdout, child.stderr):
                pipe.close()


def _results(*sources, cached=False, refresh=False, now=NOW):
    mode = ReportMode.CACHED if cached else ReportMode.REFRESH if refresh else ReportMode.DEFAULT
    return subscription.subscription_results(sources, mode=mode, now=now)


def _http_fixture(monkeypatch, *replies):
    calls = []
    responses = iter(replies)

    def fake(url, headers, *, deadline):
        assert deadline > time.monotonic()
        calls.append((url, dict(headers)))
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(remote_http, "get", fake)
    return calls


def test_claude_online_ttl_force_refresh_and_cached_no_credentials(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    calls = _http_fixture(monkeypatch, HttpReply(200, {}, BODY), HttpReply(200, {}, BODY))
    fresh, missing = _results()
    assert (fresh.status, missing.status) == ("ok", "not_configured")
    assert fresh.identity.startswith("credential:")
    reads = []
    read = RemoteCache._read

    def read_once(cache):
        reads.append(cache.path)
        return read(cache)

    monkeypatch.setattr(RemoteCache, "_read", read_once)
    (reused,) = _results("claude", now=NOW + timedelta(seconds=59))
    assert reused.cached is True and reused.fetched_at == fresh.fetched_at
    assert reads == [RemoteCache().path]
    (refreshed,) = _results("claude", refresh=True, now=NOW + timedelta(seconds=60))
    assert refreshed.cached is False
    assert len(calls) == 2
    assert calls[0] == (
        "https://api.anthropic.com/api/oauth/usage",
        {
            "Authorization": f"Bearer {TOKEN}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "new-token-that-is-not-the-fetched-one")
    monkeypatch.setattr(
        subscription.claude_credentials,
        "resolve_credential",
        lambda *_, **__: pytest.fail("cache-only mode resolved Claude credentials"),
    )
    reads.clear()
    cached, missing = _results(cached=True, now=NOW + timedelta(seconds=150))
    assert cached.cached is True and cached.stale is True
    assert (cached.identity, cached.fetched_at, cached.data) == (
        fresh.identity,
        refreshed.fetched_at,
        fresh.data,
    )
    assert missing.status == "uncached"
    assert reads == [RemoteCache().path]
    assert TOKEN not in RemoteCache().path.read_text()


@pytest.mark.parametrize(
    "response,code",
    [
        (HttpReply(401, {}, TOKEN.encode()), "auth_denied"),
        (HttpReply(403, {}, TOKEN.encode()), "scope_denied"),
        (HttpReply(503, {}, TOKEN.encode()), "service_unavailable"),
        (HttpReply(200, {}, TOKEN.encode()), "invalid_payload"),
        (remote_http.RemoteHttpError("http_timeout"), "http_timeout"),
    ],
)
def test_claude_failures_use_safe_diagnostics(monkeypatch, response, code):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    _http_fixture(monkeypatch, response)
    (result,) = _results("claude")
    assert (result.status, result.data, result.diagnostic.code) == ("error", None, code)
    assert TOKEN not in repr(result)
    assert TOKEN not in RemoteCache().path.read_text()


@pytest.mark.parametrize("status", [429, 503])
def test_claude_backoff_stale_fallback_and_credential_partition(monkeypatch, status):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    calls = _http_fixture(
        monkeypatch,
        HttpReply(200, {}, BODY),
        HttpReply(status, {"Retry-After": "60"}, b"private response"),
        HttpReply(403, {}, b"denied"),
    )
    (original,) = _results("claude")
    (failure,) = _results("claude", refresh=True, now=NOW + timedelta(seconds=1))
    assert failure.status == "error" and failure.stale and failure.data == original.data
    plus_2s = NOW + timedelta(seconds=2)
    (cached,) = _results("claude", cached=True, now=plus_2s)
    assert cached == failure
    (backed_off,) = _results("claude", refresh=True, now=plus_2s)
    assert backed_off.diagnostic.code == "backoff"
    assert backed_off.diagnostic.retry_at == (NOW + timedelta(seconds=61)).isoformat()
    assert backed_off.fetched_at == original.fetched_at
    assert len(calls) == 2
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fixture-other-credential-987")
    (other,) = _results("claude", now=plus_2s)
    assert other.status == "error" and other.data is None
    assert other.identity != original.identity
    assert len(calls) == 3


def test_default_retries_a_recent_error_instead_of_returning_hidden_failure(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    calls = _http_fixture(
        monkeypatch, HttpReply(200, {}, BODY), HttpReply(500, {}, b""), HttpReply(200, {}, BODY)
    )
    _results("claude")
    (failure,) = _results("claude", refresh=True, now=NOW + timedelta(seconds=1))
    assert failure.status == "error" and failure.stale
    (recovered,) = _results("claude", now=NOW + timedelta(seconds=2))
    assert recovered.status == "ok" and recovered.cached is False and len(calls) == 3


def test_cache_corruption_warning_preserves_success_and_bad_cache_only_is_failure(
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    path = RemoteCache().path
    path.parent.mkdir(parents=True)
    path.write_text("broken")
    assert main(["usage", "subscription", "--cached", "--json"]) == 1
    cached = json.loads(capsys.readouterr().out)["results"]
    assert all(value["diagnostic"]["code"] == "cache_invalid" for value in cached)
    _http_fixture(monkeypatch, HttpReply(200, {}, BODY))
    (result,) = _results("claude")
    assert (result.status, result.diagnostic.code) == ("ok", "cache_repaired")
    assert _results("claude", cached=True)[0].status == "ok"


def test_cli_cold_cached_offline_help_discovery_and_invalid_flags(monkeypatch, fake_codex):
    root = fake_codex()
    save_config(Config(usage_codex_auth="agent", usage_claude_auth="agent"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "invalid restricted token")
    cached = _cli("--cached", "--json")
    assert cached.returncode == 0 and cached.stderr == ""
    assert [item["status"] for item in json.loads(cached.stdout)["results"]] == [
        "uncached",
        "uncached",
    ]
    assert not (root / "version").exists()
    assert "usage subscription" in commands.command_paths()
    assert "--cached" in _cli("--help").stdout
    assert _cli("--cached", "--refresh").returncode == 2
    assert _cli("--source", "bad").returncode == 2


def test_cli_both_sources_partial_success_and_explicit_missing(monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    _http_fixture(monkeypatch, HttpReply(200, {}, BODY))
    assert main(["usage", "subscription", "--json"]) == 0
    root = json.loads(capsys.readouterr().out)
    assert (root["version"], root["kind"]) == (1, "subscription")
    assert [value["status"] for value in root["results"]] == ["ok", "not_configured"]
    assert main(["usage", "subscription", "--source", "claude", "--source", "codex"]) == 1
    text = capsys.readouterr().out
    assert "0% used" in text and "usage.codex_auth agent" in text

    save_config(Config(usage_codex_auth="agent"))

    def auth_denied():
        raise codex.CodexUsageError("auth_denied")

    monkeypatch.setattr(codex, "read_rate_limits", auth_denied)
    assert main(["usage", "subscription", "--json"]) == 1
    mixed = json.loads(capsys.readouterr().out)
    assert [value["status"] for value in mixed["results"]] == ["ok", "error"]


def test_invalid_selected_auth_preference_does_not_block_sibling(monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    _http_fixture(monkeypatch, HttpReply(200, {}, BODY))
    save_config(Config(usage_codex_auth="invalid"))
    assert main(["usage", "subscription", "--source", "claude", "--json"]) == 0
    (claude_result,) = json.loads(capsys.readouterr().out)["results"]
    assert claude_result["data"]["windows"][0]["used_percent"] == 0
    assert main(["usage", "subscription", "--json"]) == 1
    assert [
        (result["status"], result["diagnostic"]["code"] if result["diagnostic"] else None)
        for result in json.loads(capsys.readouterr().out)["results"]
    ] == [("ok", None), ("error", "invalid_config")]


def test_fresh_subscription_survives_cache_lock_contention(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    _http_fixture(monkeypatch, HttpReply(200, {}, BODY))
    path = RemoteCache().path
    path.parent.mkdir(parents=True)
    with path.with_suffix(".lock").open("wb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        (result,) = _results("claude", refresh=True)
    assert (result.status, result.cached, result.diagnostic.code) == ("ok", False, "cache_busy")
    assert result.data.windows == (QuotaWindow("five_hour", "5-hour", 0, 300, None),)


def test_subscription_service_rejects_invalid_mode():
    with pytest.raises(ValueError, match="not a valid ReportMode"):
        subscription.subscription_results((), mode="cached and refresh", now=NOW)


def test_text_cached_expired_window_omits_old_utilization(monkeypatch, capsys):
    from ringleader.usage.remote_render import render_results

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    _http_fixture(
        monkeypatch,
        HttpReply(200, {}, b'{"five_hour":{"utilization":84,"resets_at":"2026-11-19T12:01:00Z"}}'),
    )
    _results("claude")
    later = NOW + timedelta(minutes=2)
    cached = _results("claude", cached=True, now=later)
    render_results(cached, kind="subscription", json_output=False, now=later)
    text = capsys.readouterr().out
    assert "last fetched credential:" in text
    assert "expired at 2026-11-19T12:01:00+00:00" in text
    assert "84%" not in text


def test_real_runner_cancels_cli_http_worker_chain(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    pidfile = tmp_path / "http-worker.pid"
    fixture_worker = (
        "import os,time; from pathlib import Path;"
        "from ringleader.usage import remote_http_worker as w;"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()));"
        "w.fetch=lambda *_a,**_k:time.sleep(60);"
        "raise SystemExit(w.main())"
    )
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import sys\n"
        "from ringleader.usage import remote_http as h\n"
        "from ringleader.cli import main\n"
        "original=h.run_worker\n"
        "def test_worker(argv,request,**options):\n"
        "    assert argv[1:]==['-I','-m','ringleader.usage.remote_http_worker']\n"
        f"    return original([sys.executable,'-I','-c',{fixture_worker!r}],request,**options)\n"
        "h.run_worker=test_worker\n"
        "raise SystemExit(main(sys.argv[4:]))\n"
    )
    launcher = tmp_path / "run-fixture"
    _executable(launcher, f'{shlex.quote(str(driver))} "$@"')
    result = run_rl(
        ["usage", "subscription", "--source", "claude", "--refresh"],
        cancelled=pidfile.exists,
        columns=80,
        python=str(launcher),
        timeout=5,
    )
    assert pidfile.exists()
    assert result.returncode is None
    assert TOKEN not in result.output
    _no_process(pidfile)
