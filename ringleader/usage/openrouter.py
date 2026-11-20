from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from typing import Literal

from . import remote_http
from .provider_environment import OPENROUTER_KEY_VARIABLES, TokenScope
from .remote_cache import retry_after
from .remote_types import (
    MAX_ACTIVITY_ROWS,
    ActivityData,
    ActivityRow,
    CreditsData,
    Diagnostic,
    InvalidPayload,
    JsonGet,
    KeySpendData,
    boolean,
    count,
    day,
    number,
    object_dict,
    optional_string,
    safe_string,
)

TokenReport = Literal["all", "key", "activity", "credits"]
OpenRouterData = KeySpendData | ActivityData | CreditsData
SCOPES: tuple[TokenScope, ...] = ("key", "activity", "credits")
KEY_VARIABLES = OPENROUTER_KEY_VARIABLES
ACTIVITY_SCOPE = "Management key scope (account or workspace)"


class OpenRouterUsageError(Exception):
    def __init__(self, diagnostic: Diagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic.message)


def activity_bounds(now: datetime, days: int) -> tuple[str, str]:
    today = now.astimezone(UTC).date()
    return (today - timedelta(days=days)).isoformat(), (today - timedelta(days=1)).isoformat()


def _key_spend(raw: dict[str, object], secrets: Sequence[str]) -> KeySpendData:
    limit, remaining = raw["limit"], raw["limit_remaining"]
    return KeySpendData(
        usage_usd=number(raw["usage"]),
        usage_daily_usd=number(raw["usage_daily"]),
        usage_weekly_usd=number(raw["usage_weekly"]),
        usage_monthly_usd=number(raw["usage_monthly"]),
        byok_usage_usd=number(raw["byok_usage"]),
        byok_usage_daily_usd=number(raw["byok_usage_daily"]),
        byok_usage_weekly_usd=number(raw["byok_usage_weekly"]),
        byok_usage_monthly_usd=number(raw["byok_usage_monthly"]),
        limit_usd=None if limit is None else number(limit),
        limit_remaining_usd=None if remaining is None else number(remaining),
        limit_reset=optional_string(raw["limit_reset"], secrets=secrets),
        include_byok_in_limit=boolean(raw["include_byok_in_limit"]),
    )


def _activity_row(value: object, secrets: Sequence[str]) -> ActivityRow:
    raw = object_dict(value)
    return ActivityRow(
        date=day(raw["date"]),
        model=safe_string(raw["model"], secrets=secrets),
        model_permaslug=safe_string(raw["model_permaslug"], secrets=secrets),
        provider_name=safe_string(raw["provider_name"], secrets=secrets),
        endpoint_id=safe_string(raw["endpoint_id"], secrets=secrets),
        prompt_tokens=count(raw["prompt_tokens"]),
        completion_tokens=count(raw["completion_tokens"]),
        cached_tokens=count(raw["cached_tokens"]),
        reasoning_tokens=count(raw["reasoning_tokens"]),
        requests=count(raw["requests"]),
        usage_usd=number(raw["usage"]),
        byok_usage_usd=number(raw["byok_usage_inference"]),
    )


def _activity(raw: object, period: tuple[str, str], secrets: Sequence[str]) -> ActivityData:
    if not isinstance(raw, list) or len(raw) > MAX_ACTIVITY_ROWS:
        raise InvalidPayload
    since, until = period
    rows = tuple(
        row for value in raw if since <= (row := _activity_row(value, secrets)).date <= until
    )
    return ActivityData(since, until, "UTC", ACTIVITY_SCOPE, rows)


def _credits(raw: dict[str, object]) -> CreditsData:
    purchased, used = raw["total_credits"], raw["total_usage"]
    credits, usage = number(purchased), number(used)
    balance = Decimal(str(purchased)) - Decimal(str(used))
    return CreditsData(credits, usage, number(float(balance)))


def parse_report(
    scope: TokenScope,
    contents: bytes,
    *,
    period: tuple[str, str] | None = None,
    secrets: Sequence[str] = (),
) -> OpenRouterData:
    try:
        raw = object_dict(json.loads(contents))["data"]
        if scope == "activity":
            if period is None:
                raise InvalidPayload
            return _activity(raw, period, secrets)
        if scope == "key":
            return _key_spend(object_dict(raw), secrets)
        return _credits(object_dict(raw))
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError):
        raise InvalidPayload from None


def _http_diagnostic(scope: TokenScope, status: int, retry_at: str | None) -> Diagnostic:
    variable = KEY_VARIABLES[scope]
    if status == HTTPStatus.UNAUTHORIZED:
        return Diagnostic("auth_denied", f"OpenRouter rejected {variable}. Check the key.")
    if status == HTTPStatus.FORBIDDEN:
        return Diagnostic(
            "scope_denied", f"OpenRouter denied {scope} access. Check permissions for {variable}."
        )
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        return Diagnostic("rate_limited", "OpenRouter rate limited the request.", retry_at)
    if status == HTTPStatus.SERVICE_UNAVAILABLE:
        return Diagnostic("service_unavailable", "OpenRouter is temporarily unavailable.", retry_at)
    return Diagnostic("http_error", "OpenRouter usage request failed.")


def _check_period(
    scope: TokenScope, started: datetime, finished: datetime, retry_at: str | None = None
) -> None:
    if scope == "activity" and started.astimezone(UTC).date() != finished.astimezone(UTC).date():
        raise OpenRouterUsageError(
            Diagnostic(
                "period_changed",
                "The UTC day changed during the OpenRouter activity refresh. Run the report again.",
                retry_at,
            )
        )


def fetch_report(
    scope: TokenScope,
    access_token: str,
    *,
    attempt_at: datetime,
    clock: Callable[[], datetime],
    transport: JsonGet,
) -> bytes:
    try:
        reply = transport(
            f"https://openrouter.ai/api/v1/{scope}",
            {"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            deadline=time.monotonic() + remote_http.HTTP_SECONDS,
        )
    except remote_http.RemoteHttpError as exc:
        _check_period(scope, attempt_at, clock())
        raise OpenRouterUsageError(Diagnostic(exc.code, str(exc))) from None
    finished = clock()
    retry_at = retry_after(reply.status, reply.headers, now=finished)
    _check_period(scope, attempt_at, finished, retry_at)
    if reply.status == HTTPStatus.OK:
        return reply.body
    raise OpenRouterUsageError(_http_diagnostic(scope, reply.status, retry_at))
