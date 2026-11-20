from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from ..config import load_config, usage_preference
from . import openrouter, remote_http
from .openrouter import TokenReport
from .provider_environment import TokenScope
from .remote_cache import BackoffKey, CacheError, CacheKey, RemoteCache
from .remote_types import (
    Diagnostic,
    InvalidPayload,
    JsonGet,
    ProviderResult,
    ReportMode,
    Status,
    fingerprint,
    opaque_identity,
    result_to_dict,
)

INVALID_PAYLOAD = Diagnostic("invalid_payload", "OpenRouter returned invalid usage data.")


@dataclass(frozen=True)
class _Report:
    scope: TokenScope
    days: int | None
    cache_seconds: int
    refresh: bool
    transport: JsonGet


def _problem(
    scope: TokenScope, diagnostic: Diagnostic, *, status: Status = "error"
) -> ProviderResult:
    return ProviderResult("openrouter", scope, None, status, None, False, False, None, diagnostic)


def _cached(
    reports: Sequence[_Report], cache: RemoteCache, *, now: datetime, cache_seconds: int
) -> tuple[ProviderResult, ...]:
    try:
        snapshots = cache.latest_many(
            [("openrouter", report.scope, report.days) for report in reports],
            now=now,
            cache_seconds=cache_seconds,
        )
    except CacheError as exc:
        return tuple(_problem(report.scope, exc.diagnostic) for report in reports)
    return tuple(
        snapshot
        or _problem(
            report.scope,
            Diagnostic("cache_miss", "No saved OpenRouter snapshot. Refresh to fetch usage."),
            status="uncached",
        )
        for report, snapshot in zip(reports, snapshots, strict=True)
    )


def _read_keys(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        name: environment.get(name, "") for name in dict.fromkeys(openrouter.KEY_VARIABLES.values())
    }


def _valid_key(key: str) -> bool:
    return (
        0 < len(key) <= remote_http.MAX_HEADER_VALUE - len("Bearer ")
        and remote_http.printable_ascii(key)
        and " " not in key
    )


def _with_stale_data(result: ProviderResult, snapshot: ProviderResult | None) -> ProviderResult:
    if snapshot is None:
        return result
    return replace(
        result,
        data=snapshot.data,
        fetched_at=snapshot.fetched_at,
        cached=True,
        stale=True,
    )


def _fetch(
    key: CacheKey,
    report: _Report,
    access_token: str,
    secrets: Sequence[str],
    attempt_at: datetime,
    clock: Callable[[], datetime],
) -> ProviderResult:
    scope = report.scope
    identity = opaque_identity(key.fingerprint)
    period = (key.since, key.until) if key.since is not None and key.until is not None else None
    try:
        body = openrouter.fetch_report(
            scope,
            access_token,
            attempt_at=attempt_at,
            clock=clock,
            transport=report.transport,
        )
        data = openrouter.parse_report(scope, body, period=period, secrets=secrets)
        result = ProviderResult(
            "openrouter",
            scope,
            identity,
            "ok",
            attempt_at.astimezone(UTC).isoformat(),
            False,
            False,
            data,
            None,
        )
        result_to_dict(result, secrets=secrets)
        return result
    except openrouter.OpenRouterUsageError as exc:
        diagnostic = exc.diagnostic
    except InvalidPayload:
        diagnostic = INVALID_PAYLOAD
    return replace(_problem(scope, diagnostic), identity=identity)


def _online(
    report: _Report,
    cache: RemoteCache,
    keys: Mapping[str, str],
    *,
    clock: Callable[[], datetime],
) -> ProviderResult:
    scope = report.scope
    variable = openrouter.KEY_VARIABLES[scope]
    access_token = keys[variable]
    if not access_token:
        return _problem(
            scope,
            Diagnostic("not_configured", f"Set {variable} to read OpenRouter {scope}."),
            status="not_configured",
        )
    if not _valid_key(access_token):
        return _problem(
            scope, Diagnostic("invalid_credentials", f"{variable} contains an invalid key.")
        )

    observed = clock()
    since, until = (
        openrouter.activity_bounds(observed, report.days)
        if report.days is not None
        else (None, None)
    )
    scope_key = fingerprint("openrouter", variable, access_token)
    key = CacheKey("openrouter", scope, scope_key, since, until, report.days)
    backoff = BackoffKey("openrouter", scope, scope_key)
    secrets = tuple(value for value in keys.values() if value and not value.isspace())
    snapshot, warning, blocked_until = None, None, None
    try:
        snapshot, blocked_until = cache.lookup_with_backoff(
            key, backoff, now=observed, cache_seconds=report.cache_seconds
        )
        if snapshot is not None:
            result_to_dict(snapshot, secrets=secrets)
    except CacheError as exc:
        warning = exc.diagnostic
    except InvalidPayload:
        snapshot = None
        warning = CacheError("cache_invalid").diagnostic

    attempt_at = clock()
    if report.days is not None:
        since, until = openrouter.activity_bounds(attempt_at, report.days)
        if (since, until) != (key.since, key.until):
            key = replace(key, since=since, until=until)
            snapshot = None
    if blocked_until is not None:
        blocked = _problem(
            scope,
            Diagnostic("backoff", "OpenRouter requests for this report are paused.", blocked_until),
        )
        return _with_stale_data(replace(blocked, identity=opaque_identity(scope_key)), snapshot)
    if (
        not report.refresh
        and report.cache_seconds
        and snapshot is not None
        and snapshot.status == "ok"
        and not snapshot.stale
        and warning is None
    ):
        return snapshot

    result = _fetch(key, report, access_token, secrets, attempt_at, clock)
    saved = cache.save(
        key,
        result,
        now=attempt_at,
        backoff_key=backoff,
        retry_at=result.diagnostic.retry_at if result.diagnostic else None,
        secrets=secrets,
    )
    if result.status == "error":
        return _with_stale_data(result, snapshot)
    return replace(result, diagnostic=saved or warning)


def token_results(
    report: TokenReport,
    *,
    days: int,
    mode: ReportMode,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
    transport: JsonGet | None = None,
) -> tuple[ProviderResult, ...]:
    mode = ReportMode(mode)

    def fixed_clock() -> datetime:
        return now

    current_time = clock if clock is not None else fixed_clock
    scopes = openrouter.SCOPES if report == "all" else (report,)
    try:
        config = load_config()
        usage_preference("usage.cache_seconds", config.usage_cache_seconds)
    except (OSError, ValueError, AttributeError, TypeError, RecursionError):
        return tuple(
            _problem(
                scope,
                Diagnostic(
                    "invalid_config",
                    "Usage configuration is invalid. Check usage.cache_seconds and config.json.",
                ),
            )
            for scope in scopes
        )
    cache = RemoteCache(clock=current_time)
    fetch = remote_http.get if transport is None else transport
    requested = tuple(
        _Report(
            scope,
            days if scope == "activity" else None,
            config.usage_cache_seconds,
            mode is ReportMode.REFRESH,
            fetch,
        )
        for scope in scopes
    )
    if mode is ReportMode.CACHED:
        return _cached(
            requested, cache, now=current_time(), cache_seconds=config.usage_cache_seconds
        )
    keys = _read_keys(os.environ)
    return tuple(_online(item, cache, keys, clock=current_time) for item in requested)
