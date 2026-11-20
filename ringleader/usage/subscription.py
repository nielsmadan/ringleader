from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ..config import Config, load_config, usage_preference
from . import claude_credentials, claude_subscription, codex_subscription, remote_http
from .remote_cache import BackoffKey, CacheError, CacheKey, RemoteCache
from .remote_types import (
    Diagnostic,
    InvalidPayload,
    ProviderResult,
    ReportMode,
    Status,
    SubscriptionData,
    opaque_identity,
    result_to_dict,
)

SubscriptionProvider = Literal["claude", "codex"]
SOURCES: tuple[SubscriptionProvider, ...] = ("claude", "codex")
INVALID_PAYLOAD = Diagnostic(
    "invalid_payload", "Provider returned invalid subscription usage data."
)


def _result(
    provider: SubscriptionProvider,
    status: Status,
    *,
    identity: str | None = None,
    diagnostic: Diagnostic | None = None,
    data: SubscriptionData | None = None,
    fetched_at: str | None = None,
) -> ProviderResult:
    return ProviderResult(
        provider, "subscription", identity, status, fetched_at, False, False, data, diagnostic
    )


def _cached(
    providers: Sequence[SubscriptionProvider],
    cache: RemoteCache,
    *,
    now: datetime,
    cache_seconds: int,
) -> tuple[ProviderResult, ...]:
    try:
        snapshots = cache.latest_many(
            [(provider, "subscription", None) for provider in providers],
            now=now,
            cache_seconds=cache_seconds,
        )
    except CacheError as exc:
        return tuple(
            _result(provider, "error", diagnostic=exc.diagnostic) for provider in providers
        )
    return tuple(
        snapshot
        or _result(
            provider,
            "uncached",
            diagnostic=Diagnostic(
                "cache_miss", "No saved subscription snapshot. Refresh to fetch usage."
            ),
        )
        for provider, snapshot in zip(providers, snapshots, strict=True)
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


def _claude(
    config: Config, cache: RemoteCache, *, refresh: bool, clock: Callable[[], datetime]
) -> ProviderResult:
    now = clock()
    try:
        credential, plan = claude_credentials.resolve_credential(
            config.usage_claude_auth, os.environ, platform=sys.platform, home=Path.home(), now=now
        )
    except claude_credentials.CredentialError as exc:
        return _result("claude", exc.status, diagnostic=exc.diagnostic)
    identity = opaque_identity(credential.scope_key)
    key = CacheKey("claude", "subscription", credential.scope_key)
    backoff = BackoffKey("claude", "usage", credential.scope_key)
    secrets = (credential.access_token,)
    snapshot, warning, blocked_until = None, None, None
    try:
        observed = clock()
        snapshot, blocked_until = cache.lookup_with_backoff(
            key, backoff, now=observed, cache_seconds=config.usage_cache_seconds
        )
        if snapshot is not None:
            result_to_dict(snapshot, secrets=secrets)
    except CacheError as exc:
        warning = exc.diagnostic
    except InvalidPayload:
        snapshot = None
        warning = CacheError("cache_invalid").diagnostic
    if blocked_until is not None:
        return _with_stale_data(
            _result(
                "claude",
                "error",
                identity=identity,
                diagnostic=Diagnostic(
                    "backoff",
                    "Claude usage requests are paused until the retry time.",
                    blocked_until,
                ),
            ),
            snapshot,
        )
    if (
        not refresh
        and config.usage_cache_seconds
        and snapshot is not None
        and snapshot.status == "ok"
        and not snapshot.stale
        and warning is None
    ):
        return snapshot

    attempt_at = clock()
    try:
        data = claude_subscription.fetch_usage(
            credential,
            plan,
            clock=clock,
            deadline=time.monotonic() + remote_http.HTTP_SECONDS,
            transport=remote_http.get,
        )
        result = _result(
            "claude",
            "ok",
            identity=identity,
            data=data,
            fetched_at=attempt_at.astimezone(UTC).isoformat(),
        )
        result_to_dict(result, secrets=secrets)
    except claude_subscription.ClaudeUsageError as exc:
        result = _result("claude", "error", identity=identity, diagnostic=exc.diagnostic)
    except InvalidPayload:
        result = _result("claude", "error", identity=identity, diagnostic=INVALID_PAYLOAD)
    saved = cache.save(
        key,
        result,
        now=attempt_at,
        backoff_key=backoff,
        secrets=secrets,
        retry_at=result.diagnostic.retry_at if result.diagnostic else None,
    )
    if result.status == "error":
        return _with_stale_data(result, snapshot)
    return replace(result, diagnostic=saved or warning)


def _codex(config: Config, cache: RemoteCache, *, now: datetime) -> ProviderResult:
    if config.usage_codex_auth != "agent":
        return _result(
            "codex",
            "not_configured",
            diagnostic=Diagnostic(
                "not_configured", "Enable Codex usage with rl config set usage.codex_auth agent."
            ),
        )
    try:
        payload = codex_subscription.read_rate_limits()
        profile = os.path.abspath(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        scope, data = codex_subscription.parse_usage(payload, profile=profile)
    except codex_subscription.CodexUsageError as exc:
        status: Status = "not_configured" if exc.diagnostic.code == "not_configured" else "error"
        return _result("codex", status, diagnostic=exc.diagnostic)
    except InvalidPayload:
        return _result("codex", "error", diagnostic=INVALID_PAYLOAD)
    result = _result(
        "codex",
        "ok",
        identity=data.account_label,
        data=data,
        fetched_at=now.astimezone(UTC).isoformat(),
    )
    warning = cache.save(CacheKey("codex", "subscription", scope or "unknown"), result, now=now)
    return replace(result, diagnostic=warning)


def _online(
    provider: SubscriptionProvider,
    config: Config,
    cache: RemoteCache,
    *,
    refresh: bool,
    clock: Callable[[], datetime],
) -> ProviderResult:
    key = f"usage.{provider}_auth"
    try:
        usage_preference(key, getattr(config, key.replace(".", "_")))
    except ValueError as exc:
        return _result(provider, "error", diagnostic=Diagnostic("invalid_config", str(exc)))
    if provider == "claude":
        return _claude(config, cache, refresh=refresh, clock=clock)
    return _codex(config, cache, now=clock())


def subscription_results(
    sources: Sequence[SubscriptionProvider],
    *,
    mode: ReportMode,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
) -> tuple[ProviderResult, ...]:
    mode = ReportMode(mode)

    def fixed_clock() -> datetime:
        return now

    current_time = clock if clock is not None else fixed_clock
    requested = tuple(dict.fromkeys(sources)) or SOURCES
    try:
        config = load_config()
        usage_preference("usage.cache_seconds", config.usage_cache_seconds)
    except (OSError, ValueError, AttributeError, TypeError, RecursionError):
        return tuple(
            _result(
                provider,
                "error",
                diagnostic=Diagnostic(
                    "invalid_config",
                    "Usage configuration is invalid. Check usage.cache_seconds and config.json.",
                ),
            )
            for provider in requested
        )
    cache = RemoteCache(clock=current_time)
    if mode is ReportMode.CACHED:
        return _cached(
            requested, cache, now=current_time(), cache_seconds=config.usage_cache_seconds
        )
    return tuple(
        _online(provider, config, cache, refresh=mode is ReportMode.REFRESH, clock=current_time)
        for provider in requested
    )
