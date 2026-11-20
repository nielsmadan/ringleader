import concurrent.futures
import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from ringleader.config import load_config, usage_preference
from ringleader.usage.remote_cache import BackoffKey, CacheError, CacheKey, RemoteCache, retry_after
from ringleader.usage.remote_types import (
    ActivityData,
    ActivityRow,
    CreditsData,
    Diagnostic,
    InvalidPayload,
    KeySpendData,
    ProviderResult,
    QuotaWindow,
    SubscriptionData,
    count,
    fingerprint,
    number,
    opaque_identity,
    result_from_dict,
    result_to_dict,
    results_json,
)

NOW = datetime(2026, 11, 19, 12, tzinfo=UTC)
FINGERPRINT = fingerprint("env", "fake-token")
IDENTITY = opaque_identity(FINGERPRINT)


def _cache_wall_time():
    return NOW + timedelta(minutes=1)


def _result(*, now=NOW, provider="claude", data=None, scope="subscription"):
    return ProviderResult(
        provider,
        scope,
        IDENTITY,
        "ok",
        now.isoformat(),
        False,
        False,
        data
        if data is not None
        else SubscriptionData(
            None,
            None,
            (
                QuotaWindow("five_hour", "5h", 0, 300, None),
                QuotaWindow("weekly", "Weekly", None, None, None),
            ),
        ),
        None,
    )


def test_remote_serialization_preserves_zero_unknown_negative_and_independent_counts():
    subscription = result_to_dict(_result())
    assert subscription["data"] == {
        "plan": None,
        "account_label": None,
        "windows": [
            {
                "key": "five_hour",
                "label": "5h",
                "used_percent": 0.0,
                "duration_minutes": 300,
                "resets_at": None,
            },
            {
                "key": "weekly",
                "label": "Weekly",
                "used_percent": None,
                "duration_minutes": None,
                "resets_at": None,
            },
        ],
    }
    key = _result(
        provider="openrouter",
        scope="key",
        data=KeySpendData(0, 0, 0, 0, 0, 0, 0, 0, None, None, None, False),
    )
    activity = _result(
        provider="openrouter",
        scope="activity",
        data=ActivityData(
            "2026-11-18",
            "2026-11-18",
            "UTC",
            "management credential",
            (
                ActivityRow(
                    "2026-11-18", "model", "model-v1", "provider", "endpoint", 1, 2, 3, 4, 1, 0, 0
                ),
            ),
        ),
    )
    credits = _result(provider="openrouter", scope="credits", data=CreditsData(1, 2, -1))
    for result in (key, activity, credits):
        assert result_from_dict(result_to_dict(result)) == result
    document = json.loads(results_json((key, activity, credits), kind="token"))
    assert document["version"] == 1 and document["kind"] == "token"
    assert document["results"][2]["data"]["balance_usd"] == -1
    assert document["results"][1]["data"]["rows"][0]["cached_tokens"] == 3
    with pytest.raises(InvalidPayload):
        results_json((key,), kind="subscription")
    with pytest.raises(InvalidPayload):
        results_json((_result(),), kind="token")


@pytest.mark.parametrize("value", [True, False, "0", None, math.nan, math.inf, -math.inf, 10**1000])
def test_remote_numbers_reject_non_numeric_boolean_nonfinite_and_overflow(value):
    with pytest.raises(InvalidPayload):
        number(value)


@pytest.mark.parametrize("value", [True, 1.0, "1", -1])
def test_remote_counts_require_nonnegative_integers(value):
    with pytest.raises(InvalidPayload):
        count(value)


def test_serialization_normalizes_controls_and_rejects_supplied_secrets():
    data = SubscriptionData(
        "Plan\x1b\nname", None, (QuotaWindow("weekly", "Week\tly", 110, None, None),)
    )
    encoded = result_to_dict(_result(data=data))
    assert encoded["data"]["plan"] == "Planname"
    assert encoded["data"]["windows"][0]["used_percent"] == 110
    with pytest.raises(InvalidPayload):
        result_to_dict(_result(data=data), secrets=("Plan",))
    with pytest.raises(InvalidPayload):
        result_to_dict(
            _result(data=SubscriptionData("fake-\x1bsecret", None, ())), secrets=("fake-secret",)
        )
    with pytest.raises(InvalidPayload):
        result_to_dict(replace(_result(), identity="raw-account@example.test"))
    with pytest.raises(InvalidPayload):
        results_json((_result(data=SubscriptionData("a" * 513, None, ())),), kind="subscription")


def test_config_validation_is_lazy_for_local_and_strict_for_remote(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"usage_cache_seconds": True, "usage_claude_auth": ["agent"]}))
    cfg = load_config(path)
    assert cfg.usage_cache_seconds is True
    with pytest.raises(ValueError, match=r"usage\.claude_auth"):
        usage_preference("usage.claude_auth", cfg.usage_claude_auth)
    for value in (True, 1.0, "60", -1, 3601, None):
        with pytest.raises(ValueError, match=r"usage\.cache_seconds"):
            usage_preference("usage.cache_seconds", value)


def test_cache_roundtrip_ttl_partition_permissions_and_monotonic_success(tmp_path):
    cache = RemoteCache(tmp_path / "usage" / "remote-v1.json", clock=_cache_wall_time)
    key = CacheKey("claude", "subscription", FINGERPRINT)
    backoff = BackoffKey("claude", "usage", FINGERPRINT)
    assert cache.save(key, _result(), now=NOW) is None
    assert cache.lookup_with_backoff(key, backoff, now=NOW, cache_seconds=60) == (
        replace(_result(), cached=True),
        None,
    )
    assert cache.path.stat().st_mode & 0o777 == 0o600
    stale, _ = cache.lookup_with_backoff(
        key, backoff, now=NOW + timedelta(seconds=61), cache_seconds=60
    )
    assert stale.stale is True
    other_fingerprint = fingerprint("another")
    assert cache.lookup_with_backoff(
        replace(key, fingerprint=other_fingerprint),
        replace(backoff, fingerprint=other_fingerprint),
        now=NOW,
        cache_seconds=60,
    ) == (None, None)
    later = NOW + timedelta(seconds=10)
    assert cache.save(key, _result(now=later), now=later) is None
    assert cache.save(key, _result(), now=later) is None
    assert cache.save(key, _result(), now=NOW) is None
    found, _ = cache.lookup_with_backoff(key, backoff, now=later, cache_seconds=60)
    assert found.fetched_at == later.isoformat()


def test_cache_failed_refresh_keeps_same_identity_success_and_marks_stale(tmp_path):
    cache = RemoteCache(tmp_path / "remote-v1.json", clock=_cache_wall_time)
    key = CacheKey("claude", "subscription", FINGERPRINT)
    backoff = BackoffKey("claude", "usage", FINGERPRINT)
    cache.save(key, _result(), now=NOW)
    legacy = json.loads(cache.path.read_text())
    entry = legacy["entries"][key.encoded()]
    entry["last_attempt"] = entry["success"]
    cache.path.write_text(json.dumps(legacy))
    assert cache.lookup_with_backoff(key, backoff, now=NOW, cache_seconds=60) == (
        replace(_result(), cached=True),
        None,
    )
    failure = replace(
        _result(), status="error", data=None, diagnostic=Diagnostic("denied", "Access denied.")
    )
    cache.save(key, failure, now=NOW + timedelta(seconds=1))
    later = NOW + timedelta(seconds=2)
    found, _ = cache.lookup_with_backoff(key, backoff, now=later, cache_seconds=60)
    assert (found.status, found.stale, found.fetched_at, found.data) == (
        "error",
        True,
        NOW.isoformat(),
        _result().data,
    )
    assert cache.lookup_with_backoff(
        replace(key, fingerprint="unknown"), backoff, now=later, cache_seconds=60
    ) == (None, None)
    assert cache.latest_many((("claude", "subscription", None),), now=later, cache_seconds=60) == (
        found,
    )


def test_activity_cache_keeps_exact_dates_and_cache_only_selects_requested_days(tmp_path):
    cache = RemoteCache(tmp_path / "remote-v1.json", clock=_cache_wall_time)
    key = CacheKey("openrouter", "activity", FINGERPRINT, "2026-11-18", "2026-11-18", 1)
    result = _result(
        provider="openrouter",
        scope="activity",
        data=ActivityData(
            key.since,
            key.until,
            "UTC",
            "management credential",
            (),
        ),
    )
    cache.save(key, result, now=NOW)
    assert cache.lookup_with_backoff(
        replace(key, since="2026-11-17", until="2026-11-17"),
        BackoffKey("openrouter", "activity", FINGERPRINT),
        now=NOW,
        cache_seconds=60,
    ) == (None, None)
    missing, found = cache.latest_many(
        (("openrouter", "activity", 30), ("openrouter", "activity", 1)),
        now=NOW + timedelta(days=1),
        cache_seconds=60,
    )
    assert missing is None
    assert (found.data.since, found.data.until, found.stale) == ("2026-11-18", "2026-11-18", True)


@pytest.mark.parametrize(
    "payload,code",
    [
        (b"no JSON", "cache_invalid"),
        (b'{"version":2,"entries":{},"backoffs":{}}', "cache_version"),
        (b'{"version":true,"entries":{},"backoffs":{}}', "cache_version"),
    ],
)
def test_cache_rejects_corruption_and_unsupported_versions_safely(tmp_path, payload, code):
    cache = RemoteCache(tmp_path / "remote-v1.json", clock=_cache_wall_time)
    cache.path.write_bytes(payload)
    with pytest.raises(CacheError) as error:
        cache.latest_many((("claude", "subscription", None),), now=NOW, cache_seconds=60)
    assert error.value.diagnostic.code == code
    assert "no JSON" not in str(error.value)


def test_cache_rejects_future_timestamps(tmp_path):
    wall_now = NOW
    cache = RemoteCache(tmp_path / "remote-v1.json", clock=lambda: wall_now)
    key = CacheKey("claude", "subscription", FINGERPRINT)
    assert cache.save(key, _result(), now=NOW) is None
    wall_now = NOW - timedelta(seconds=1)
    with pytest.raises(CacheError, match="future"):
        cache.lookup_with_backoff(
            key, BackoffKey("claude", "usage", FINGERPRINT), now=NOW, cache_seconds=60
        )
    assert cache.save(key, _result(), now=NOW).code == "cache_future"


def test_independent_concurrent_cache_updates_are_merged(tmp_path):
    path = tmp_path / "remote-v1.json"

    def write(index):
        result = _result(data=SubscriptionData(str(index), None, ()))
        return RemoteCache(path, clock=_cache_wall_time).save(
            CacheKey("claude", "subscription", fingerprint(str(index))), result, now=NOW
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        warnings = list(pool.map(write, range(20)))
    assert warnings == [None] * 20
    assert len(json.loads(path.read_text())["entries"]) == 20


def test_out_of_order_overlapping_attempts_keep_independent_entries_and_latest_success(tmp_path):
    newer = NOW + timedelta(seconds=2)
    cache = RemoteCache(tmp_path / "remote-v1.json", clock=lambda: newer)
    first_key = CacheKey("claude", "subscription", FINGERPRINT)
    other_key = replace(first_key, fingerprint=fingerprint("other"))
    newest = _result(now=newer, data=SubscriptionData("latest", None, ()))
    older = _result(data=SubscriptionData("older", None, ()))

    assert cache.save(first_key, newest, now=newer) is None
    assert cache.save(other_key, older, now=NOW) is None
    assert cache.save(first_key, older, now=NOW) is None

    for key, result in ((first_key, newest), (other_key, older)):
        backoff = BackoffKey("claude", "usage", key.fingerprint)
        assert cache.lookup_with_backoff(key, backoff, now=newer, cache_seconds=60) == (
            replace(result, cached=True),
            None,
        )


def test_cache_lock_contention_is_bounded(tmp_path):
    import fcntl
    import time

    cache = RemoteCache(tmp_path / "remote-v1.json", clock=_cache_wall_time)
    with cache.path.with_suffix(".lock").open("wb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = time.monotonic()
        warning = cache.save(CacheKey("claude", "subscription", FINGERPRINT), _result(), now=NOW)
        elapsed = time.monotonic() - start
    assert warning.code == "cache_busy" and elapsed < 1.5


def test_retry_after_backoff_is_bounded_and_independent_of_activity_dates(tmp_path):
    cache = RemoteCache(tmp_path / "remote-v1.json", clock=_cache_wall_time)
    backoff = BackoffKey("openrouter", "activity", FINGERPRINT)
    until = retry_after(429, {"Retry-After": "999999"}, now=NOW)
    assert until == (NOW + timedelta(days=1)).isoformat()
    assert (
        retry_after(503, {"retry-after": "Thu, 19 Nov 2026 12:01:00 GMT"}, now=NOW)
        == (NOW + timedelta(minutes=1)).isoformat()
    )
    assert retry_after(403, {"retry-after": "60"}, now=NOW) is None
    assert retry_after(429, {"retry-after": "untrusted token"}, now=NOW) is None
    key = CacheKey("openrouter", "activity", FINGERPRINT, "2026-11-18", "2026-11-18", 1)
    error = ProviderResult(
        "openrouter",
        "activity",
        IDENTITY,
        "error",
        None,
        False,
        False,
        None,
        Diagnostic("rate_limit", "Try later.", until),
    )
    assert cache.save(key, error, now=NOW, backoff_key=backoff, retry_at=until) is None
    assert cache.lookup_with_backoff(
        replace(key, since="2026-11-17", days=2), backoff, now=NOW, cache_seconds=60
    ) == (None, until)
    assert cache.lookup_with_backoff(
        CacheKey("openrouter", "key", FINGERPRINT),
        BackoffKey("openrouter", "key", FINGERPRINT),
        now=NOW,
        cache_seconds=60,
    ) == (None, None)
    assert cache.lookup_with_backoff(
        key, backoff, now=NOW + timedelta(days=1), cache_seconds=60
    ) == (None, None)


def test_newer_claude_success_prevents_delayed_backoff(tmp_path):
    cache = RemoteCache(tmp_path / "remote-v1.json", clock=_cache_wall_time)
    key = CacheKey("claude", "subscription", FINGERPRINT)
    backoff = BackoffKey("claude", "usage", FINGERPRINT)
    until = (NOW + timedelta(minutes=5)).isoformat()
    failure = replace(
        _result(),
        status="error",
        fetched_at=None,
        data=None,
        diagnostic=Diagnostic("rate_limited", "Try later.", until),
    )
    newer = NOW + timedelta(seconds=2)
    success = _result(now=newer)

    assert cache.save(key, success, now=newer, backoff_key=backoff) is None
    assert cache.save(key, failure, now=NOW, backoff_key=backoff, retry_at=until) is None

    assert cache.lookup_with_backoff(key, backoff, now=newer, cache_seconds=60) == (
        replace(success, cached=True),
        None,
    )


@pytest.mark.parametrize("newer_saves_first", [False, True])
@pytest.mark.parametrize("different_query", [False, True])
def test_overlapping_plain_failure_preserves_retry_after(
    tmp_path, newer_saves_first, different_query
):
    cache = RemoteCache(tmp_path / "remote-v1.json", clock=_cache_wall_time)
    key = CacheKey("openrouter", "activity", FINGERPRINT, "2026-11-18", "2026-11-18", 1)
    other = replace(key, since="2026-11-17", days=2) if different_query else key
    backoff = BackoffKey("openrouter", "activity", FINGERPRINT)
    until = (NOW + timedelta(minutes=5)).isoformat()
    failure = ProviderResult(
        "openrouter",
        "activity",
        IDENTITY,
        "error",
        None,
        False,
        False,
        None,
        Diagnostic("rate_limited", "Try later.", until),
    )
    later = NOW + timedelta(seconds=2)
    attempts = [
        (key, failure, NOW, until),
        (
            other,
            replace(failure, diagnostic=Diagnostic("http_error", "Request failed.")),
            later,
            None,
        ),
    ]
    for query, result, at, retry_at in reversed(attempts) if newer_saves_first else attempts:
        assert cache.save(query, result, now=at, backoff_key=backoff, retry_at=retry_at) is None

    assert cache.lookup_with_backoff(other, backoff, now=later, cache_seconds=60) == (
        None,
        until,
    )


def test_newer_activity_success_prevents_backoff_from_delayed_other_query(tmp_path):
    cache = RemoteCache(tmp_path / "remote-v1.json", clock=_cache_wall_time)
    older_key = CacheKey("openrouter", "activity", FINGERPRINT, "2026-11-17", "2026-11-18", 2)
    newer_key = CacheKey("openrouter", "activity", FINGERPRINT, "2026-11-18", "2026-11-18", 1)
    backoff = BackoffKey("openrouter", "activity", FINGERPRINT)
    until = (NOW + timedelta(minutes=5)).isoformat()
    failure = ProviderResult(
        "openrouter",
        "activity",
        IDENTITY,
        "error",
        None,
        False,
        False,
        None,
        Diagnostic("rate_limited", "Try later.", until),
    )
    newer = NOW + timedelta(seconds=2)
    success = _result(
        now=newer,
        provider="openrouter",
        scope="activity",
        data=ActivityData(newer_key.since, newer_key.until, "UTC", "management credential", ()),
    )

    assert cache.save(older_key, failure, now=NOW, backoff_key=backoff, retry_at=until) is None
    assert cache.lookup_with_backoff(older_key, backoff, now=NOW, cache_seconds=60) == (
        None,
        until,
    )
    assert cache.save(newer_key, success, now=newer, backoff_key=backoff) is None
    assert (
        cache.save(
            older_key, failure, now=NOW + timedelta(seconds=1), backoff_key=backoff, retry_at=until
        )
        is None
    )

    assert cache.lookup_with_backoff(newer_key, backoff, now=newer, cache_seconds=60) == (
        replace(success, cached=True),
        None,
    )


def test_cache_never_saves_known_supplied_secret_or_overwrites_future_version(tmp_path):
    cache = RemoteCache(tmp_path / "remote-v1.json", clock=_cache_wall_time)
    key = CacheKey("claude", "subscription", FINGERPRINT)
    leaked = _result(data=SubscriptionData("fake-secret", None, ()))
    warning = cache.save(key, leaked, now=NOW, secrets=("fake-secret",))
    assert warning.code == "cache_write"
    assert not cache.path.exists()
    cache.path.write_text('{"version":2,"entries":{},"backoffs":{}}')
    assert cache.save(key, _result(), now=NOW).code == "cache_version"
    assert json.loads(cache.path.read_text())["version"] == 2


def test_remote_json_version_one_golden(golden):
    golden("usage/remote_v1", results_json((_result(),), kind="subscription"))


def test_cache_refuses_nonregular_files_without_blocking(tmp_path):
    import os

    fifo = tmp_path / "cache-fifo"
    os.mkfifo(fifo)
    with pytest.raises(CacheError) as error:
        RemoteCache(fifo).latest_many(
            (("claude", "subscription", None),), now=NOW, cache_seconds=60
        )
    assert error.value.diagnostic.code == "cache_read"
    target = tmp_path / "target.json"
    target.write_text('{"version":1,"entries":{},"backoffs":{}}')
    symlink = tmp_path / "cache-link"
    symlink.symlink_to(target)
    with pytest.raises(CacheError) as error:
        RemoteCache(symlink).latest_many(
            (("claude", "subscription", None),), now=NOW, cache_seconds=60
        )
    assert error.value.diagnostic.code == "cache_read"
