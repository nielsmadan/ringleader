import ctypes
import io
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from ringleader.usage import claude_credentials as credentials
from ringleader.usage import claude_keychain as keychain
from ringleader.usage import claude_subscription as claude
from ringleader.usage import remote_http
from ringleader.usage.remote_types import InvalidPayload, QuotaWindow

NOW = datetime(2026, 11, 19, 12, tzinfo=UTC)
TOKEN = "fixture-claude-access-token"
EXPIRY = int((NOW + timedelta(hours=1)).timestamp() * 1000)
_READ_CREDENTIALS = keychain.read_credentials


def _oauth(**values):
    oauth = {
        "accessToken": TOKEN,
        "expiresAt": EXPIRY,
        "subscriptionType": "max",
        "scopes": ["user:profile", "user:inference"],
        **values,
    }
    return json.dumps({"claudeAiOauth": oauth}).encode()


def test_credentials_exact_profile_and_mode_identity(tmp_path):
    home = tmp_path / "home"
    profile = tmp_path / "configured"
    secure = tmp_path / "secure"
    for path in (home / ".claude", profile, secure):
        path.mkdir(parents=True)
        (path / ".credentials.json").write_bytes(_oauth())
    environment = {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(profile),
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": str(secure),
        credentials.TOKEN_VAR: TOKEN,
    }
    agent, plan = credentials.resolve_credential(
        "agent", environment, platform="linux", home=home, now=NOW
    )
    assert plan == "max"
    assert agent.access_token == TOKEN
    assert agent.expires_at == NOW + timedelta(hours=1)
    assert agent.scopes == {"user:profile", "user:inference"}
    assert credentials.credential_path(environment, home=home, cwd=tmp_path).parent == secure
    environment.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    other, _ = credentials.resolve_credential(
        "agent", environment, platform="linux", home=home, now=NOW
    )
    assert credentials.credential_path(environment, home=home, cwd=tmp_path).parent == profile
    override, env_plan = credentials.resolve_credential(
        "env", environment, platform="linux", home=home, now=NOW
    )
    assert env_plan is None
    assert override.expires_at is None and override.scopes is None
    assert len({agent.scope_key, other.scope_key, override.scope_key}) == 3
    assert TOKEN not in repr(agent)


def test_relative_credentials_profile_uses_explicit_cwd(tmp_path):
    path = credentials.credential_path(
        {"CLAUDE_CONFIG_DIR": "profiles/../profile"}, home=tmp_path, cwd=tmp_path / "cwd"
    )
    assert path == tmp_path / "cwd" / "profile" / ".credentials.json"


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"expiresAt": None}, "credentials_expired"),
        ({"expiresAt": int(NOW.timestamp() * 1000)}, "credentials_expired"),
        ({"expiresAt": True}, "credentials_invalid"),
        ({"expiresAt": float("inf")}, "credentials_invalid"),
        ({"scopes": ["user:inference"]}, "scope_denied"),
        ({"scopes": None}, "scope_denied"),
        ({"scopes": "user:profile"}, "credentials_invalid"),
        ({"accessToken": TOKEN + "\nspoof"}, "credentials_invalid"),
        ({"subscriptionType": TOKEN}, "credentials_invalid"),
    ],
)
def test_invalid_oauth_is_actionable_without_secret(overrides, code):
    with pytest.raises(credentials.CredentialError) as error:
        credentials.parse_credentials(_oauth(**overrides), profile="fixture", now=NOW)
    assert error.value.diagnostic.code == code
    assert TOKEN not in str(error.value)


def test_agent_mode_has_no_cross_profile_or_env_fallback(tmp_path):
    home = tmp_path / "owner"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_bytes(_oauth())
    environment = {
        "HOME": str(home),
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": str(tmp_path / "missing"),
        credentials.TOKEN_VAR: TOKEN,
    }
    with pytest.raises(credentials.CredentialError) as error:
        credentials.resolve_credential("agent", environment, platform="darwin", home=home, now=NOW)
    assert error.value.status == "not_configured"
    with pytest.raises(credentials.CredentialError) as error:
        credentials.resolve_credential(
            "env", {"HOME": str(home)}, platform="darwin", home=home, now=NOW
        )
    assert error.value.diagnostic.code == "not_configured"


def test_explicit_empty_secure_storage_is_default_file_but_never_native(tmp_path):
    environment = {
        "HOME": str(tmp_path),
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": "",
        "CLAUDE_CONFIG_DIR": "different-profile",
    }
    assert credentials.credential_path(environment, home=tmp_path, cwd=tmp_path) == (
        tmp_path / ".claude" / ".credentials.json"
    )
    assert keychain.native_profile_allowed(environment, "darwin") is False


def test_exact_profile_linux_credentials_require_bounded_regular_file(tmp_path):
    path = tmp_path / ".credentials.json"
    os.mkfifo(path)
    with pytest.raises(credentials.CredentialError) as error:
        credentials._read_file(path)
    assert error.value.diagnostic.code == "credentials_invalid"
    path.unlink()
    path.write_bytes(b"x" * (keychain.MAX_CREDENTIAL_BYTES + 1))
    with pytest.raises(credentials.CredentialError, match="invalid"):
        credentials._read_file(path)


def test_macos_native_adapter_requires_default_profile_and_real_home(monkeypatch, tmp_path):
    read_count = []
    home = str(tmp_path)
    monkeypatch.setattr(keychain.pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir=home))
    monkeypatch.setattr(keychain, "read_credentials", lambda: read_count.append(True) or _oauth())
    credential, plan = credentials.resolve_credential(
        "agent", {"HOME": home}, platform="darwin", home=tmp_path, now=NOW
    )
    assert (credential.access_token, plan, read_count) == (TOKEN, "max", [True])
    profile = tmp_path / ".claude"
    profile.mkdir()
    (profile / ".credentials.json").write_bytes(_oauth())
    file_credential, _ = credentials.resolve_credential(
        "agent",
        {"HOME": home, "CLAUDE_SECURESTORAGE_CONFIG_DIR": str(profile)},
        platform="darwin",
        home=tmp_path,
        now=NOW,
    )
    assert file_credential.scope_key != credential.scope_key
    assert keychain.native_profile_allowed({"HOME": home + "-other"}, "darwin") is False
    assert keychain.native_profile_allowed({"HOME": home}, "linux") is False
    for override in keychain.PROFILE_OVERRIDES:
        assert keychain.native_profile_allowed({"HOME": home, override: ""}, "darwin") is False


@pytest.mark.parametrize(
    "code,status",
    [
        ("keychain_missing", "not_configured"),
        ("keychain_denied", "error"),
        ("keychain_ambiguous", "error"),
        ("keychain_timeout", "error"),
    ],
)
def test_keychain_failure_is_safe_and_never_falls_back(monkeypatch, tmp_path, code, status):
    def refuse():
        raise keychain.KeychainError(code)

    monkeypatch.setattr(keychain, "native_profile_allowed", lambda *_: True)
    monkeypatch.setattr(keychain, "read_credentials", refuse)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_bytes(_oauth())
    with pytest.raises(credentials.CredentialError) as error:
        credentials.resolve_credential(
            "agent", {credentials.TOKEN_VAR: TOKEN}, platform="darwin", home=tmp_path, now=NOW
        )
    assert (error.value.diagnostic.code, error.value.status) == (code, status)


def _native_fake(monkeypatch, *, count=1, status=0, interaction_status=0, data_size=None):
    data = b'{"fixture":true}'
    data_buffer = ctypes.create_string_buffer(data)
    released = []
    observed = {}
    pointers = {}

    def constant(library, name):
        return pointers.setdefault(name, len(pointers) + 100)

    def dictionary(_allocator, keys, values, size, _keys, _values):
        observed["query"] = dict(zip(keys[:size], values[:size], strict=True))
        return 4

    def copy(_query, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = 5
        return status

    def interaction(allowed):
        observed["interaction"] = allowed
        return interaction_status

    def string(_allocator, value, _encoding):
        observed["service"] = value
        return 2

    def number(_allocator, _number_type, value):
        observed["limit"] = ctypes.cast(value, ctypes.POINTER(ctypes.c_int))[0]
        return 3

    def release(value):
        released.append(value)

    security = SimpleNamespace(
        SecItemCopyMatching=copy,
        SecKeychainSetUserInteractionAllowed=interaction,
    )
    core = SimpleNamespace(
        CFStringCreateWithCString=string,
        CFNumberCreate=number,
        CFDictionaryCreate=dictionary,
        CFGetTypeID=lambda value: {5: 10, 6: 11}[value],
        CFArrayGetTypeID=lambda: 10,
        CFArrayGetCount=lambda _: count,
        CFArrayGetValueAtIndex=lambda *_: 6,
        CFDataGetTypeID=lambda: 11,
        CFDataGetLength=lambda _: len(data) if data_size is None else data_size,
        CFDataGetBytePtr=lambda _: ctypes.addressof(data_buffer),
        CFRelease=release,
    )
    monkeypatch.setattr(keychain, "_constant", constant)
    return security, core, pointers, observed, released, data


def test_native_fake_restricts_query_and_disables_all_interaction(monkeypatch):
    security, core, pointers, observed, released, data = _native_fake(monkeypatch)
    assert keychain._copy_matching(security, core) == data
    assert observed == {
        "interaction": 0,
        "service": b"Claude Code-credentials",
        "limit": 2,
        "query": {
            pointers["kSecClass"]: pointers["kSecClassGenericPassword"],
            pointers["kSecAttrService"]: 2,
            pointers["kSecUseAuthenticationUI"]: pointers["kSecUseAuthenticationUIFail"],
            pointers["kSecMatchLimit"]: 3,
            pointers["kSecReturnData"]: pointers["kCFBooleanTrue"],
        },
    }
    assert released == [5, 4, 3, 2]


@pytest.mark.parametrize(
    "values,code",
    [
        ({"status": -25300}, "keychain_missing"),
        ({"status": -25308}, "keychain_denied"),
        ({"count": 2}, "keychain_ambiguous"),
        ({"data_size": keychain.MAX_CREDENTIAL_BYTES + 1}, "keychain_invalid"),
        ({"interaction_status": -1}, "keychain_denied"),
    ],
)
def test_native_fake_rejects_unsafe_or_unavailable_items(monkeypatch, values, code):
    security, core, *_ = _native_fake(monkeypatch, **values)
    with pytest.raises(keychain.KeychainError, match=code):
        keychain._copy_matching(security, core)


def test_native_worker_wrong_request_exits_before_loading_frameworks(monkeypatch):
    monkeypatch.setattr(keychain.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"")))
    assert keychain.main() == 2


def test_native_keychain_adapter_round_trips_the_worker_frame(monkeypatch):
    start = keychain.time.monotonic()
    expected = _oauth()
    monkeypatch.setattr(keychain, "native_profile_allowed", lambda *_: True)
    monkeypatch.setattr(keychain, "_load_frameworks", lambda: ("security", "core"))

    def copy_matching(security, core):
        assert (security, core) == ("security", "core")
        return expected

    def run(argv, request, *, deadline, max_output):
        assert argv == [keychain.sys.executable, "-I", "-m", "ringleader.usage.claude_keychain"]
        assert start < deadline <= keychain.time.monotonic() + keychain.KEYCHAIN_SECONDS
        output = io.BytesIO()
        with monkeypatch.context() as worker:
            worker.setattr(keychain.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(request)))
            worker.setattr(keychain.sys, "stdout", SimpleNamespace(buffer=output))
            assert keychain.main() == 0
        return output.getvalue()

    monkeypatch.setattr(keychain, "_copy_matching", copy_matching)
    monkeypatch.setattr(remote_http, "run_worker", run)
    assert _READ_CREDENTIALS() == expected


@pytest.mark.parametrize("credential_bytes", [65536, 65537])
def test_native_keychain_parent_bounds_worker_frames(monkeypatch, credential_bytes):
    run_worker = remote_http.run_worker
    fixture = (
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        f"sys.stdout.buffer.write(bytes([0]) + b'x' * {credential_bytes})\n"
    )

    def run(argv, request, **limits):
        return run_worker([argv[0], "-I", "-c", fixture], request, **limits)

    monkeypatch.setattr(remote_http, "run_worker", run)
    if credential_bytes == 65536:
        assert _READ_CREDENTIALS() == b"x" * credential_bytes
    else:
        with pytest.raises(keychain.KeychainError, match=r"^keychain_denied$"):
            _READ_CREDENTIALS()


@pytest.mark.parametrize(
    "output,code",
    [
        (b"unexpected-credential-frame", "keychain_invalid"),
        (b"\x00", "keychain_invalid"),
        (b"keychain_ambiguous", "keychain_ambiguous"),
        (remote_http.RemoteHttpError("http_timeout"), "keychain_timeout"),
    ],
)
def test_native_keychain_parent_maps_worker_failures_safely(monkeypatch, output, code):
    def run(*args, **kwargs):
        if isinstance(output, Exception):
            raise output
        return output

    monkeypatch.setattr(remote_http, "run_worker", run)
    with pytest.raises(keychain.KeychainError, match=code):
        _READ_CREDENTIALS()


def _scoped(model_id, name, percent):
    return {
        "group": "weekly",
        "kind": "weekly_scoped",
        "scope": {"model": {"id": model_id, "display_name": name}},
        "percent": percent,
        "resets_at": None,
    }


def test_claude_windows_keep_zero_unknown_and_nonduplicate_scopes():
    payload = {
        "five_hour": {"utilization": 0, "resets_at": "2026-11-19T16:00:00Z"},
        "seven_day": {"utilization": None, "resets_at": None},
        "seven_day_opus": {"utilization": 2, "resets_at": "2026-11-21T12:00:00Z"},
        "seven_day_sonnet": {"utilization": 112},
        "limits": [
            {"group": "monthly", "percent": "ignored"},
            _scoped("claude-opus", "Opus", 3),
            _scoped("fable", "Fable", None),
            _scoped("fable", "Fable", 0),
            _scoped("fable", "Fable", 99),
            _scoped("configured-all-models", "All models", 4),
        ],
    }
    parsed = claude.parse_usage(json.dumps(payload).encode(), plan="max")
    assert parsed.plan == "max"
    assert parsed.windows == (
        QuotaWindow("five_hour", "5-hour", 0.0, 300, "2026-11-19T16:00:00+00:00"),
        QuotaWindow("seven_day", "Weekly", None, 10080, None),
        QuotaWindow("seven_day_sonnet", "Sonnet weekly", 112.0, 10080, None),
        QuotaWindow(
            "weekly_scoped:claude-opus", "Opus weekly", 3.0, 10080, "2026-11-21T12:00:00+00:00"
        ),
        QuotaWindow("weekly_scoped:fable", "Fable weekly", 0.0, 10080, None),
    )


def test_claude_flat_zero_fills_matching_scoped_unknown():
    parsed = claude.parse_usage(
        json.dumps(
            {
                "seven_day_sonnet": {"utilization": 0},
                "limits": [_scoped("claude-sonnet", "Sonnet", None)],
            }
        ).encode()
    )
    assert parsed.windows == (
        QuotaWindow("weekly_scoped:claude-sonnet", "Sonnet weekly", 0, 10080, None),
    )


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "first_id,first_percent,expected",
    [
        ("claude-opus", 30, {"weekly_scoped:claude-opus": 30}),
        ("claude-opus-4-6", None, {"weekly_scoped:claude-opus-4-6": None, "seven_day_opus": 80}),
    ],
)
def test_claude_flat_quota_prefers_exact_id_and_keeps_ambiguous_alias_separate(
    reverse, first_id, first_percent, expected
):
    other_id = "claude-opus-4-7"
    models = [_scoped(first_id, "Opus", first_percent), _scoped(other_id, "Opus", None)]
    if reverse:
        models.reverse()
    reset = "2026-11-21T12:00:00+00:00"
    parsed = claude.parse_usage(
        json.dumps(
            {"seven_day_opus": {"utilization": 80, "resets_at": reset}, "limits": models}
        ).encode()
    )
    assert {window.key: window.used_percent for window in parsed.windows} == {
        **expected,
        f"weekly_scoped:{other_id}": None,
    }
    assert {window.key: window.resets_at for window in parsed.windows} == {
        **dict.fromkeys(expected),
        f"weekly_scoped:{other_id}": None,
        ("weekly_scoped:claude-opus" if first_id == "claude-opus" else "seven_day_opus"): reset,
    }


def test_claude_flat_quota_fills_a_unique_display_alias():
    parsed = claude.parse_usage(
        json.dumps(
            {
                "seven_day_opus": {"utilization": 80},
                "limits": [_scoped("claude-opus-4-6", "Opus", None)],
            }
        ).encode()
    )
    assert parsed.windows == (
        QuotaWindow("weekly_scoped:claude-opus-4-6", "Opus weekly", 80, 10080, None),
    )


@pytest.mark.parametrize(
    "value",
    [
        {"five_hour": {"utilization": False}},
        {"five_hour": {"utilization": float("nan")}},
        {"five_hour": {"utilization": -1}},
        {"five_hour": {"resets_at": "2026-11-19T16:00:00"}},
        {"five_hour": {"resets_at": "2026-11-19x16:00:00Z"}},
        {"five_hour": {"resets_at": "2026-11-19\x00T16:00:00Z"}},
        {"five_hour": []},
        {"limits": {}},
        {"five_hour": {"utilization": 0}, "limits": [False]},
        {"limits": [{"group": "weekly", "kind": 1}]},
        {"limits": [{"group": None, "kind": "weekly_scoped"}]},
        {"limits": [_scoped("fable", "Fable", "10")]},
        {"limits": [_scoped(None, None, 12)]},
        {"limits": [_scoped(TOKEN, TOKEN, 1)]},
    ],
)
def test_claude_rejects_malformed_recognized_fields(value):
    with pytest.raises(InvalidPayload):
        claude.parse_usage(json.dumps(value).encode(), secrets=(TOKEN,))


def test_claude_empty_windows_are_valid_and_optional_fields_stay_unknown():
    assert claude.parse_usage(b'{"five_hour":null}').windows == ()
    assert claude.parse_usage(b'{"five_hour":{}}').windows == (
        QuotaWindow("five_hour", "5-hour", None, 300, None),
    )
