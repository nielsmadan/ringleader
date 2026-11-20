import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from ringleader import commands
from ringleader.cli import main
from ringleader.config import Config, save_config
from ringleader.usage import openrouter, remote_http, token
from ringleader.usage.remote_cache import BackoffKey, CacheKey, RemoteCache
from ringleader.usage.remote_types import (
    CreditsData,
    Diagnostic,
    HttpReply,
    InvalidPayload,
    ProviderResult,
    ReportMode,
    fingerprint,
)

NOW = datetime(2026, 11, 20, 0, 0, 3, tzinfo=UTC)
KEY_VAR = "OPENROUTER_API_KEY"
MANAGEMENT_VAR = "OPENROUTER_MANAGEMENT_KEY"
KEY = "fixture-openrouter-ordinary-secret"
MANAGEMENT_KEY = "fixture-openrouter-management-secret"
ROTATED_KEY = "fixture-openrouter-rotated-secret"
DRIVER = Path(__file__).parent / "fixtures" / "openrouter_cli.py"
KEY_SPEND = {
    "usage": 25.5,
    "usage_daily": 0,
    "usage_weekly": 7.2,
    "usage_monthly": 12.25,
    "byok_usage": 1.5,
    "byok_usage_daily": 0.00000005,
    "byok_usage_weekly": 0.5,
    "byok_usage_monthly": 0.7,
    "limit": None,
    "limit_remaining": None,
    "limit_reset": None,
    "include_byok_in_limit": False,
}
ROW = {
    "date": "2026-11-19",
    "model": "openai/gpt-4.1",
    "model_permaslug": "openai/gpt-4.1-2025-04-14",
    "provider_name": "Fixture Provider",
    "endpoint_id": "550e8400-e29b-41d4-a716-446655440000",
    "prompt_tokens": 10,
    "completion_tokens": 125,
    "cached_tokens": 49,
    "reasoning_tokens": 135,
    "requests": 5,
    "usage": 0.015,
    "byok_usage_inference": 0.012,
}
CREDITS = {"total_credits": 25.5, "total_usage": 40.5}
ACTIVITY = [
    ROW | {"date": "2026-10-20", "requests": 71},
    ROW | {"date": "2026-10-21", "requests": 31},
    ROW | {"date": "2026-11-18", "requests": 13},
    ROW,
    ROW | {"date": "2026-11-20", "requests": 63},
]
VALID_REPLIES = {
    "key": (200, {}, {"data": KEY_SPEND}),
    "activity": (200, {}, {"data": ACTIVITY}),
    "credits": (200, {}, {"data": CREDITS}),
}


@pytest.fixture(autouse=True)
def no_real_openrouter(monkeypatch):
    def refuse(url, headers, *, deadline):
        pytest.fail("test reached an uninjected provider transport")

    monkeypatch.setattr(remote_http, "get", refuse)


@pytest.fixture
def openrouter_cli(tmp_path):
    scenario = tmp_path / "responses.json"
    log = scenario.with_suffix(".requests")

    def cli(*args, now=NOW, replies=VALID_REPLIES):
        scenario.write_text(json.dumps({"now": now.isoformat(), "replies": replies}))
        log.unlink(missing_ok=True)
        result = subprocess.run(
            [sys.executable, str(DRIVER), str(scenario), "usage", "token", *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result, log.read_text().splitlines() if log.exists() else []

    return cli


def _plain_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "ringleader", *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _payload(data):
    return json.dumps({"data": data}).encode()


def _run_reports(report="all", *, days=30, cached=False, refresh=False, now=NOW, clock=None):
    return token.token_results(
        report,
        days=days,
        mode=ReportMode.CACHED if cached else ReportMode.REFRESH if refresh else ReportMode.DEFAULT,
        now=now,
        clock=clock,
    )


def _inject_replies(monkeypatch, *scoped_replies):
    calls = []
    responses = iter(scoped_replies)

    def fetch(url, headers, *, deadline):
        scope, reply = next(responses)
        assert url == f"https://openrouter.ai/api/v1/{scope}"
        calls.append(scope)
        return reply

    monkeypatch.setattr(remote_http, "get", fetch)
    return calls


def test_token_transport_can_be_passed_per_invocation(monkeypatch):
    monkeypatch.setenv(MANAGEMENT_VAR, MANAGEMENT_KEY)
    calls = []

    def fetch(url, headers, *, deadline):
        calls.append(url)
        assert headers["Authorization"] == f"Bearer {MANAGEMENT_KEY}"
        return HttpReply(200, {}, _payload(CREDITS))

    (result,) = token.token_results(
        "credits", days=30, mode=ReportMode.REFRESH, now=NOW, transport=fetch
    )
    assert result.data == CreditsData(25.5, 40.5, -15)
    assert calls == ["https://openrouter.ai/api/v1/credits"]


def test_token_service_rejects_invalid_mode():
    with pytest.raises(ValueError, match="not a valid ReportMode"):
        token.token_results("credits", days=30, mode="cached and refresh", now=NOW)


def test_key_spend_preserves_periods_limits_and_external_byok():
    key = KEY_SPEND | {"limit": 100, "limit_remaining": 0, "limit_reset": "monthly"}
    data = openrouter.parse_report("key", _payload(key))
    assert (
        data.usage_usd,
        data.usage_daily_usd,
        data.usage_weekly_usd,
        data.usage_monthly_usd,
    ) == (25.5, 0.0, 7.2, 12.25)
    assert (
        data.byok_usage_usd,
        data.byok_usage_daily_usd,
        data.byok_usage_weekly_usd,
        data.byok_usage_monthly_usd,
    ) == (1.5, 0.00000005, 0.5, 0.7)
    assert (data.limit_usd, data.limit_remaining_usd, data.limit_reset) == (100, 0, "monthly")
    assert data.include_byok_in_limit is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"usage": True},
        {"usage_daily": None},
        {"usage_weekly": float("inf")},
        {"byok_usage_monthly": float("nan")},
        {"limit": False},
        {"limit_remaining": "0"},
        {"limit_reset": []},
        {"include_byok_in_limit": 1},
    ],
)
def test_key_spend_rejects_malformed_report_fields(overrides):
    with pytest.raises(InvalidPayload):
        openrouter.parse_report("key", _payload(KEY_SPEND | overrides))


@pytest.mark.parametrize("field", ["usage_monthly", "byok_usage_daily", "limit_reset"])
def test_key_spend_requires_report_fields(field):
    with pytest.raises(InvalidPayload):
        openrouter.parse_report("key", _payload({k: v for k, v in KEY_SPEND.items() if k != field}))


@pytest.mark.parametrize(
    ("now", "days", "expected"),
    [
        (NOW, 30, ("2026-10-21", "2026-11-19")),
        (NOW, 1, ("2026-11-19", "2026-11-19")),
        (NOW - timedelta(seconds=4), 30, ("2026-10-20", "2026-11-18")),
        (NOW.astimezone(timezone(timedelta(hours=-8))), 2, ("2026-11-18", "2026-11-19")),
        (datetime(2024, 3, 1, tzinfo=UTC), 1, ("2024-02-29", "2024-02-29")),
    ],
)
def test_activity_bounds_are_completed_utc_calendar_days(now, days, expected):
    assert openrouter.activity_bounds(now, days) == expected


def test_activity_keeps_original_independent_counters_and_only_requested_days():
    period = openrouter.activity_bounds(NOW, 30)
    data = openrouter.parse_report("activity", _payload(ACTIVITY), period=period)
    assert (data.since, data.until, data.timezone) == (*period, "UTC")
    assert [(row.date, row.requests) for row in data.rows] == [
        ("2026-10-21", 31),
        ("2026-11-18", 13),
        ("2026-11-19", 5),
    ]
    row = data.rows[-1]
    assert (row.model, row.model_permaslug, row.provider_name, row.endpoint_id) == (
        ROW["model"],
        ROW["model_permaslug"],
        ROW["provider_name"],
        ROW["endpoint_id"],
    )
    assert (row.prompt_tokens, row.completion_tokens, row.cached_tokens, row.reasoning_tokens) == (
        10,
        125,
        49,
        135,
    )
    assert (row.usage_usd, row.byok_usage_usd) == (0.015, 0.012)
    empty = openrouter.parse_report("activity", _payload([]), period=period)
    assert empty.rows == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"date": "2026-11-19T00:00:00Z"},
        {"date": "2026-02-30"},
        {"date": "2026-11-9"},
        {"model_permaslug": None},
        {"provider_name": None},
        {"endpoint_id": 37},
        {"prompt_tokens": -1},
        {"completion_tokens": False},
        {"cached_tokens": 9.0},
        {"reasoning_tokens": None},
        {"requests": 1.5},
        {"usage": float("-inf")},
        {"byok_usage_inference": True},
    ],
)
def test_activity_rejects_invalid_required_row_fields(overrides):
    with pytest.raises(InvalidPayload):
        openrouter.parse_report(
            "activity",
            _payload([ROW | overrides]),
            period=openrouter.activity_bounds(NOW, 1),
        )


@pytest.mark.parametrize("field", ["model_permaslug", "provider_name", "endpoint_id"])
def test_activity_requires_endpoint_dimensions(field):
    row = {key: value for key, value in ROW.items() if key != field}
    with pytest.raises(InvalidPayload):
        openrouter.parse_report(
            "activity",
            _payload([row]),
            period=openrouter.activity_bounds(NOW, 1),
        )


def test_credits_balance_can_be_negative_and_zero_is_known():
    assert openrouter.parse_report("credits", _payload(CREDITS)) == CreditsData(25.5, 40.5, -15)
    zeros = openrouter.parse_report("credits", _payload({"total_credits": 0, "total_usage": 0}))
    assert zeros == CreditsData(0, 0, 0)


def test_cli_credits_balance_uses_decimal_subtraction(monkeypatch, openrouter_cli):
    monkeypatch.setenv(MANAGEMENT_VAR, MANAGEMENT_KEY)
    replies = {"credits": (200, {}, {"data": {"total_credits": 100.1, "total_usage": 100.2}})}
    result, calls = openrouter_cli("--report", "credits", "--json", replies=replies)
    assert (result.returncode, calls) == (0, ["credits"])
    (credits,) = json.loads(result.stdout)["results"]
    assert credits["data"]["balance_usd"] == -0.1

    result, calls = openrouter_cli("--report", "credits", "--cached")
    assert (result.returncode, calls) == (0, [])
    assert result.stdout.splitlines()[-1] == "  Balance -$0.1"


@pytest.mark.parametrize(
    "data",
    [
        {"total_credits": None, "total_usage": 25},
        {"total_credits": True, "total_usage": 25},
        {"total_credits": 14, "total_usage": float("nan")},
        {"total_credits": 1e308, "total_usage": -1e308},
        {"total_credits": 14},
    ],
)
def test_credits_rejects_malformed_or_nonfinite_values(data):
    with pytest.raises(InvalidPayload):
        openrouter.parse_report("credits", _payload(data))


def test_cli_both_credentials_json_golden_and_text(monkeypatch, openrouter_cli, golden):
    monkeypatch.setenv(KEY_VAR, KEY)
    monkeypatch.setenv(MANAGEMENT_VAR, MANAGEMENT_KEY)
    result, calls = openrouter_cli("--source", "openrouter", "--days", "1", "--json")
    assert (result.returncode, result.stderr, calls) == (0, "", ["key", "activity", "credits"])
    root = json.loads(result.stdout)
    assert (root["version"], root["kind"]) == (1, "token")
    key, activity, credits = root["results"]
    assert key["identity"] != activity["identity"] == credits["identity"]
    assert (key["data"]["limit_usd"], key["data"]["usage_daily_usd"]) == (None, 0.0)
    golden("usage/token_v1", result.stdout)

    text, calls = openrouter_cli("--days", "1")
    assert (text.returncode, text.stderr, calls) == (0, "", [])
    assert "last fetched credential:" in text.stdout
    assert "Key spending cap unlimited   Remaining key cap unavailable" in text.stdout
    assert "OpenRouter $0.0   BYOK $0.00000005" in text.stdout
    assert "Management key scope (account or workspace)" in text.stdout
    assert "2026-11-19 through 2026-11-19 UTC (inclusive)" in text.stdout
    assert "Prompt 10   Completion 125   Cached 49   Reasoning 135   Requests 5" in text.stdout
    assert "Purchased $25.5   Used $40.5" in text.stdout
    assert "Balance -$15.0" in text.stdout
    for secret in (KEY, MANAGEMENT_KEY):
        assert secret not in text.stdout + text.stderr + result.stdout + result.stderr
        assert secret not in RemoteCache().path.read_text()


def test_cached_key_labels_use_the_snapshot_utc_day_week_and_month(monkeypatch, openrouter_cli):
    monkeypatch.setenv(KEY_VAR, KEY)
    before = datetime(2026, 5, 31, 23, 59, 59, tzinfo=UTC)
    fresh, calls = openrouter_cli("--report", "key", now=before)
    assert (fresh.returncode, calls) == (0, ["key"])

    cached, calls = openrouter_cli("--report", "key", now=before + timedelta(seconds=2))
    assert (cached.returncode, calls) == (0, [])
    lines = cached.stdout.splitlines()
    assert lines[1].endswith("at 2026-05-31T23:59:59+00:00 (cached)")
    assert [line.split("OpenRouter")[0].strip() for line in lines[3:7]] == [
        "All time",
        "2026-05-31 (UTC)",
        "Week of 2026-05-25 (UTC)",
        "2026-05 (UTC)",
    ]


@pytest.mark.parametrize(
    ("variable", "value", "requested", "statuses"),
    [
        (KEY_VAR, KEY, ["key"], ["ok", "not_configured", "not_configured"]),
        (MANAGEMENT_VAR, MANAGEMENT_KEY, ["activity", "credits"], ["not_configured", "ok", "ok"]),
    ],
)
def test_cli_all_reports_treat_other_missing_key_as_informational(
    monkeypatch,
    openrouter_cli,
    variable,
    value,
    requested,
    statuses,
):
    monkeypatch.setenv(variable, value)
    result, calls = openrouter_cli("--report", "all", "--source", "openrouter", "--json")
    assert (result.returncode, result.stderr, calls) == (0, "", requested)
    assert [item["status"] for item in json.loads(result.stdout)["results"]] == statuses


@pytest.mark.parametrize(
    ("report", "scope", "variable"),
    [
        (["--report", "key"], "key", KEY_VAR),
        (["--report", "activity"], "activity", MANAGEMENT_VAR),
        (["--report", "credits"], "credits", MANAGEMENT_VAR),
    ],
)
def test_unmodified_cli_missing_report_credential(report, scope, variable):
    result = _plain_cli("usage", "token", *report, "--json")
    assert (result.returncode, result.stderr) == (1, "")
    (entry,) = json.loads(result.stdout)["results"]
    assert (entry["scope"], entry["status"]) == (scope, "not_configured")
    assert entry["diagnostic"]["message"] == f"Set {variable} to read OpenRouter {scope}."


def test_unmodified_cli_default_missing_config_and_cold_cache():
    online = _plain_cli("usage", "token", "--json")
    assert (online.returncode, online.stderr) == (1, "")
    assert [item["status"] for item in json.loads(online.stdout)["results"]] == [
        "not_configured",
        "not_configured",
        "not_configured",
    ]
    cached = _plain_cli("usage", "token", "--cached", "--days", "7", "--json")
    assert (cached.returncode, cached.stderr) == (0, "")
    assert [item["status"] for item in json.loads(cached.stdout)["results"]] == [
        "uncached",
        "uncached",
        "uncached",
    ]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--source", "claude"], "invalid choice"),
        (["--report", "unknown"], "invalid choice"),
        (["--days", "0"], "invalid choice"),
        (["--days", "31"], "invalid choice"),
        (["--days", "1.5"], "invalid int value"),
        (["--report", "key", "--days", "30"], "--days applies only to the activity report"),
        (["--report", "credits", "--days", "1"], "--days applies only to the activity report"),
        (["--cached", "--refresh"], "not allowed with argument --cached"),
    ],
)
def test_unmodified_cli_argument_errors(args, message):
    result = _plain_cli("usage", "token", *args)
    assert result.returncode == 2
    assert message in result.stderr


def test_unmodified_cli_help_and_command_discovery():
    assert "usage token" in commands.command_paths()
    root_help = _plain_cli("usage", "--help")
    assert root_help.returncode == 0 and "subscription, token," in root_help.stdout
    help_result = _plain_cli("usage", "token", "--help")
    assert help_result.returncode == 0
    assert all(
        flag in help_result.stdout for flag in ("--source", "--report", "--days", "--cached")
    )


def test_cli_partial_scope_denied_retains_other_reports(monkeypatch, openrouter_cli):
    monkeypatch.setenv(KEY_VAR, KEY)
    monkeypatch.setenv(MANAGEMENT_VAR, MANAGEMENT_KEY)
    replies = VALID_REPLIES | {"activity": (403, {}, {"error": MANAGEMENT_KEY})}
    result, calls = openrouter_cli("--json", replies=replies)
    assert (result.returncode, result.stderr, calls) == (1, "", ["key", "activity", "credits"])
    key, activity, credits = json.loads(result.stdout)["results"]
    assert (key["status"], activity["status"], credits["status"]) == ("ok", "error", "ok")
    assert activity["diagnostic"]["code"] == "scope_denied"
    assert MANAGEMENT_KEY not in result.stdout + result.stderr + RemoteCache().path.read_text()


@pytest.mark.parametrize(
    ("scope", "raw", "secret"),
    [
        ("key", KEY_SPEND | {"limit_reset": MANAGEMENT_KEY}, MANAGEMENT_KEY),
        ("activity", [ROW | {"provider_name": KEY}], KEY),
        ("activity", [ROW | {"model": MANAGEMENT_KEY}], MANAGEMENT_KEY),
    ],
)
def test_report_rejects_known_keys_in_parseable_fields(
    monkeypatch,
    openrouter_cli,
    scope,
    raw,
    secret,
):
    monkeypatch.setenv(KEY_VAR, KEY)
    monkeypatch.setenv(MANAGEMENT_VAR, MANAGEMENT_KEY)
    replies = VALID_REPLIES | {scope: (200, {}, {"data": raw})}
    result, calls = openrouter_cli("--report", scope, "--refresh", "--json", replies=replies)
    assert (result.returncode, result.stderr, calls) == (1, "", [scope])
    (entry,) = json.loads(result.stdout)["results"]
    assert (entry["status"], entry["data"], entry["diagnostic"]["code"]) == (
        "error",
        None,
        "invalid_payload",
    )
    assert secret not in result.stdout + result.stderr + RemoteCache().path.read_text()


def test_invalid_management_key_does_not_block_ordinary_key(monkeypatch, openrouter_cli):
    monkeypatch.setenv(KEY_VAR, KEY)
    monkeypatch.setenv(MANAGEMENT_VAR, f"{MANAGEMENT_KEY}\nforged header")
    result, calls = openrouter_cli("--json")
    assert (result.returncode, result.stderr, calls) == (1, "", ["key"])
    key, activity, credits = json.loads(result.stdout)["results"]
    assert key["status"] == "ok"
    assert [entry["diagnostic"]["code"] for entry in (activity, credits)] == [
        "invalid_credentials",
        "invalid_credentials",
    ]
    assert MANAGEMENT_KEY not in result.stdout + RemoteCache().path.read_text()


def test_cache_only_does_not_read_credentials_and_keeps_stored_requested_period(monkeypatch):
    monkeypatch.setenv(MANAGEMENT_VAR, MANAGEMENT_KEY)
    calls = _inject_replies(
        monkeypatch,
        ("activity", HttpReply(200, {}, _payload(ACTIVITY))),
        ("activity", HttpReply(200, {}, _payload(ACTIVITY))),
        ("activity", HttpReply(200, {}, _payload(ACTIVITY))),
    )
    (old,) = _run_reports("activity", days=7, now=NOW)
    monkeypatch.setenv(MANAGEMENT_VAR, ROTATED_KEY)
    (new,) = _run_reports("activity", days=7, now=NOW + timedelta(days=1))
    later = NOW + timedelta(days=2)
    (different_days,) = _run_reports("activity", days=2, now=later)
    assert (old.data.until, new.data.until, different_days.data.until) == (
        "2026-11-19",
        "2026-11-20",
        "2026-11-21",
    )
    assert old.identity != new.identity

    def refuse(environment):
        pytest.fail("cache-only tried to resolve OpenRouter keys")

    monkeypatch.setattr(token, "_read_keys", refuse)
    (saved,) = _run_reports("activity", cached=True, days=7, now=later)
    assert (
        saved.status,
        saved.cached,
        saved.stale,
        saved.fetched_at,
        saved.data,
        saved.identity,
    ) == (
        "ok",
        True,
        True,
        new.fetched_at,
        new.data,
        new.identity,
    )
    (cold,) = _run_reports("activity", cached=True, days=30, now=later)
    assert (cold.status, cold.diagnostic.code) == ("uncached", "cache_miss")
    assert calls == ["activity", "activity", "activity"]


def test_online_cache_is_partitioned_by_query_bounds_and_credential(monkeypatch):
    monkeypatch.setenv(MANAGEMENT_VAR, MANAGEMENT_KEY)
    calls = _inject_replies(
        monkeypatch,
        ("activity", HttpReply(200, {}, _payload(ACTIVITY))),
        ("activity", HttpReply(200, {}, _payload(ACTIVITY))),
        ("activity", HttpReply(403, {}, b"raw upstream")),
    )
    before = NOW - timedelta(seconds=5)
    (first,) = _run_reports("activity", days=2, now=before)
    (fresh,) = _run_reports("activity", days=2, now=before + timedelta(seconds=1))
    assert (fresh.cached, fresh.stale, fresh.data) == (True, False, first.data)
    (next_day,) = _run_reports("activity", days=2, now=NOW)
    assert (next_day.cached, next_day.data.until) == (False, "2026-11-19")
    assert next_day.identity == first.identity

    monkeypatch.setenv(MANAGEMENT_VAR, ROTATED_KEY)
    (denied,) = _run_reports("activity", days=2, now=NOW + timedelta(seconds=1))
    assert (denied.status, denied.cached, denied.data) == ("error", False, None)
    assert denied.identity != first.identity
    assert calls == ["activity", "activity", "activity"]


def test_activity_backoff_survives_day_change_without_blocking_credits(monkeypatch, openrouter_cli):
    monkeypatch.setenv(MANAGEMENT_VAR, MANAGEMENT_KEY)
    before = NOW - timedelta(seconds=5)
    success, calls = openrouter_cli("--report", "activity", "--days", "1", "--json", now=before)
    assert (success.returncode, calls) == (0, ["activity"])
    (saved,) = json.loads(success.stdout)["results"]
    limited_replies = VALID_REPLIES | {
        "activity": (429, {"Retry-After": "60"}, {"error": MANAGEMENT_KEY}),
    }
    failed, calls = openrouter_cli(
        "--days",
        "1",
        "--refresh",
        "--json",
        now=before + timedelta(seconds=1),
        replies=limited_replies,
    )
    assert (failed.returncode, failed.stderr, calls) == (1, "", ["activity", "credits"])
    _, activity, credits = json.loads(failed.stdout)["results"]
    assert (activity["status"], activity["cached"], activity["stale"]) == ("error", True, True)
    assert (activity["fetched_at"], activity["data"]) == (saved["fetched_at"], saved["data"])
    assert credits["status"] == "ok"
    assert activity["diagnostic"]["code"] == "rate_limited"

    backed_off, calls = openrouter_cli("--days", "2", "--refresh", "--json")
    assert (backed_off.returncode, backed_off.stderr, calls) == (1, "", ["credits"])
    _, activity, credits = json.loads(backed_off.stdout)["results"]
    assert (activity["status"], activity["data"], credits["status"]) == ("error", None, "ok")
    assert activity["diagnostic"] == {
        "code": "backoff",
        "message": "OpenRouter requests for this report are paused.",
        "retry_at": (before + timedelta(seconds=61)).isoformat(),
    }
    assert MANAGEMENT_KEY not in failed.stdout + backed_off.stdout + RemoteCache().path.read_text()


def test_activity_reads_snapshot_and_backoff_from_one_cache_version(monkeypatch):
    monkeypatch.setenv(MANAGEMENT_VAR, MANAGEMENT_KEY)
    calls = _inject_replies(monkeypatch, ("activity", HttpReply(200, {}, _payload(ACTIVITY))))
    (first,) = _run_reports("activity", days=1)

    now = NOW + timedelta(seconds=1)
    cache = RemoteCache(clock=lambda: now)
    cache_read = RemoteCache._read
    retry_at = (now + timedelta(seconds=60)).isoformat()
    scope_key = fingerprint("openrouter", MANAGEMENT_VAR, MANAGEMENT_KEY)
    activity_key = CacheKey("openrouter", "activity", scope_key, "2026-11-19", "2026-11-19", 1)
    backoff = BackoffKey("openrouter", "activity", scope_key)
    failed = ProviderResult(
        "openrouter",
        "activity",
        first.identity,
        "error",
        None,
        False,
        False,
        None,
        Diagnostic("rate_limited", "Rate limited.", retry_at),
    )

    def read_then_publish(instance):
        raw = cache_read(instance)
        monkeypatch.setattr(RemoteCache, "_read", cache_read)
        assert (
            cache.save(activity_key, failed, now=now, backoff_key=backoff, retry_at=retry_at)
            is None
        )
        return raw

    monkeypatch.setattr(RemoteCache, "_read", read_then_publish)
    (still_fresh,) = _run_reports("activity", days=1, now=now)
    assert (still_fresh.status, still_fresh.cached, still_fresh.data) == ("ok", True, first.data)
    (blocked,) = _run_reports("activity", days=2, now=now, refresh=True)
    assert (blocked.diagnostic.code, blocked.diagnostic.retry_at) == ("backoff", retry_at)
    assert calls == ["activity"]


def test_activity_at_http_limit_is_cached_with_previously_saved_credits(monkeypatch):
    monkeypatch.setenv(MANAGEMENT_VAR, MANAGEMENT_KEY)
    row = ROW | {"model": "🛰" * 100, "usage": 0, "byok_usage_inference": 0}
    row_bytes = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode()) + 1
    row_count = (remote_http.MAX_BODY - len(b'{"data":[]}') + 1) // row_bytes
    body = json.dumps(
        {"data": [row] * row_count}, ensure_ascii=False, separators=(",", ":")
    ).encode()
    assert len(body) <= remote_http.MAX_BODY
    _inject_replies(
        monkeypatch,
        ("credits", HttpReply(200, {}, _payload(CREDITS))),
        ("activity", HttpReply(200, {}, body)),
    )
    (credits,) = _run_reports("credits")
    (activity,) = _run_reports("activity", days=1)
    assert (activity.status, len(activity.data.rows), activity.diagnostic) == (
        "ok",
        row_count,
        None,
    )
    reads = []
    read = RemoteCache._read

    def read_once(cache):
        reads.append(cache.path)
        return read(cache)

    monkeypatch.setattr(RemoteCache, "_read", read_once)
    key, saved_activity, saved_credits = _run_reports(cached=True, days=1)
    assert key.status == "uncached"
    assert (saved_activity.cached, saved_activity.data) == (True, activity.data)
    assert (saved_credits.cached, saved_credits.data) == (True, credits.data)
    assert reads == [RemoteCache().path]


@pytest.mark.parametrize("crossing_scope", ["key", "activity"])
def test_reports_observe_utc_midnight_independently(monkeypatch, crossing_scope):
    monkeypatch.setenv(KEY_VAR, KEY)
    monkeypatch.setenv(MANAGEMENT_VAR, MANAGEMENT_KEY)
    before = NOW - timedelta(seconds=4)
    now = before
    calls = []

    def clock():
        return now

    def fetch(url, headers, *, deadline):
        nonlocal now
        scope = url.rsplit("/", 1)[-1]
        calls.append(scope)
        if scope == crossing_scope:
            now = NOW
        status, hdrs, body = VALID_REPLIES[scope]
        return HttpReply(status, hdrs, json.dumps(body).encode())

    monkeypatch.setattr(remote_http, "get", fetch)
    key, activity, credits = _run_reports(now=before, clock=clock)
    assert calls == ["key", "activity", "credits"]
    assert (key.status, credits.status) == ("ok", "ok")
    assert (key.fetched_at, credits.fetched_at) == (before.isoformat(), NOW.isoformat())
    if crossing_scope == "key":
        assert (activity.status, activity.fetched_at, activity.data.until) == (
            "ok",
            NOW.isoformat(),
            "2026-11-19",
        )
    else:
        assert (activity.status, activity.data, activity.diagnostic.code) == (
            "error",
            None,
            "period_changed",
        )


def test_activity_lookup_crossing_utc_midnight_fetches_the_new_period(monkeypatch):
    monkeypatch.setenv(MANAGEMENT_VAR, MANAGEMENT_KEY)
    calls = _inject_replies(monkeypatch, ("activity", HttpReply(200, {}, _payload(ACTIVITY))))
    before = NOW - timedelta(seconds=4)
    now = before
    cache_read = RemoteCache._read

    def read_at_midnight(instance):
        nonlocal now
        raw = cache_read(instance)
        now = NOW
        return raw

    monkeypatch.setattr(RemoteCache, "_read", read_at_midnight)
    (activity,) = _run_reports("activity", days=1, now=before, clock=lambda: now)
    assert calls == ["activity"]
    assert (activity.status, activity.fetched_at, activity.data.until) == (
        "ok",
        NOW.isoformat(),
        "2026-11-19",
    )
    (saved,) = _run_reports("activity", days=1, cached=True)
    assert (saved.status, saved.data) == ("ok", activity.data)


def test_rate_limited_activity_crossing_utc_midnight_keeps_backoff(monkeypatch):
    monkeypatch.setenv(MANAGEMENT_VAR, MANAGEMENT_KEY)
    before = NOW - timedelta(seconds=4)
    now = before
    calls = []

    def fetch(url, headers, *, deadline):
        nonlocal now
        calls.append(url)
        now = NOW
        return HttpReply(429, {"Retry-After": "60"}, b"")

    monkeypatch.setattr(remote_http, "get", fetch)
    (failed,) = _run_reports("activity", days=1, now=before, clock=lambda: now)
    retry_at = (NOW + timedelta(seconds=60)).isoformat()
    assert (failed.status, failed.diagnostic.code, failed.diagnostic.retry_at) == (
        "error",
        "period_changed",
        retry_at,
    )
    (blocked,) = _run_reports("activity", days=2, refresh=True)
    assert (blocked.diagnostic.code, blocked.diagnostic.retry_at) == ("backoff", retry_at)
    assert calls == ["https://openrouter.ai/api/v1/activity"]


def test_cache_warning_preserves_fresh_report_and_invalid_config_is_error(
    monkeypatch,
    openrouter_cli,
):
    monkeypatch.setenv(KEY_VAR, KEY)
    path = RemoteCache().path
    path.parent.mkdir(parents=True)
    path.write_text("invalid cache")
    cached, calls = openrouter_cli("--report", "key", "--cached", "--json")
    assert (cached.returncode, cached.stderr, calls) == (1, "", [])
    (error,) = json.loads(cached.stdout)["results"]
    assert (error["status"], error["diagnostic"]["code"]) == ("error", "cache_invalid")

    online, calls = openrouter_cli("--report", "key", "--json")
    assert (online.returncode, online.stderr, calls) == (0, "", ["key"])
    (fresh,) = json.loads(online.stdout)["results"]
    assert (fresh["status"], fresh["diagnostic"]["code"]) == ("ok", "cache_repaired")
    assert fresh["data"]["usage_usd"] == 25.5

    save_config(Config(usage_claude_auth="irrelevant"))
    assert main(["usage", "token", "--cached", "--report", "key"]) == 0
    save_config(Config(usage_cache_seconds=-1))
    result, calls = openrouter_cli("--report", "key", "--json")
    assert (result.returncode, result.stderr, calls) == (1, "", [])
    (error,) = json.loads(result.stdout)["results"]
    assert (error["status"], error["diagnostic"]["code"]) == ("error", "invalid_config")
