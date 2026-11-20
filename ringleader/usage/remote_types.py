from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal, Protocol

Provider = Literal["claude", "codex", "openrouter"]
Scope = Literal["subscription", "key", "activity", "credits"]
Status = Literal["ok", "not_configured", "uncached", "error"]
ReportKind = Literal["subscription", "token"]
MAX_WINDOWS = 100
MAX_ACTIVITY_ROWS = 20000


class ReportMode(StrEnum):
    DEFAULT = "default"
    CACHED = "cached"
    REFRESH = "refresh"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    retry_at: str | None = None


@dataclass(frozen=True)
class QuotaWindow:
    key: str
    label: str
    used_percent: float | None
    duration_minutes: int | None
    resets_at: str | None


@dataclass(frozen=True)
class SubscriptionData:
    plan: str | None
    account_label: str | None
    windows: tuple[QuotaWindow, ...]


@dataclass(frozen=True)
class KeySpendData:
    usage_usd: float
    usage_daily_usd: float
    usage_weekly_usd: float
    usage_monthly_usd: float
    byok_usage_usd: float
    byok_usage_daily_usd: float
    byok_usage_weekly_usd: float
    byok_usage_monthly_usd: float
    limit_usd: float | None
    limit_remaining_usd: float | None
    limit_reset: str | None
    include_byok_in_limit: bool


@dataclass(frozen=True)
class ActivityRow:
    date: str
    model: str
    model_permaslug: str
    provider_name: str
    endpoint_id: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    requests: int
    usage_usd: float
    byok_usage_usd: float


@dataclass(frozen=True)
class ActivityData:
    since: str
    until: str
    timezone: Literal["UTC"]
    account_scope: str
    rows: tuple[ActivityRow, ...]


@dataclass(frozen=True)
class CreditsData:
    total_credits_usd: float
    total_usage_usd: float
    balance_usd: float


ReportData = SubscriptionData | KeySpendData | ActivityData | CreditsData


@dataclass(frozen=True)
class ProviderResult:
    provider: Provider
    scope: Scope
    identity: str | None
    status: Status
    fetched_at: str | None
    cached: bool
    stale: bool
    data: ReportData | None
    diagnostic: Diagnostic | None


@dataclass(frozen=True)
class HttpReply:
    status: int
    headers: Mapping[str, str]
    body: bytes


class JsonGet(Protocol):
    def __call__(self, url: str, headers: Mapping[str, str], *, deadline: float) -> HttpReply: ...


@dataclass(frozen=True, repr=False)
class Credential:
    access_token: str
    scope_key: str
    expires_at: datetime | None
    scopes: frozenset[str] | None


class InvalidPayload(ValueError):
    def __init__(self) -> None:
        super().__init__("Provider returned invalid usage data.")


def number(value: object, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidPayload
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise InvalidPayload from exc
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise InvalidPayload
    return result


def count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise InvalidPayload
    return value


def boolean(value: object) -> bool:
    if type(value) is not bool:
        raise InvalidPayload
    return value


def safe_string(value: object, *, secrets: Sequence[str] = (), limit: int = 512) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise InvalidPayload
    cleaned = "".join(char for char in value if char.isprintable())
    if any(secret and (secret in value or secret in cleaned) for secret in secrets):
        raise InvalidPayload
    return cleaned


def optional_string(value: object, *, secrets: Sequence[str] = ()) -> str | None:
    return None if value is None else safe_string(value, secrets=secrets)


def timestamp(value: object) -> str:
    text = safe_string(value, limit=64)
    if text != value:
        raise InvalidPayload
    date_text, separator, _ = text.partition("T")
    if not separator:
        raise InvalidPayload
    try:
        date.fromisoformat(date_text)
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidPayload from exc
    if parsed.tzinfo is None:
        raise InvalidPayload
    return parsed.astimezone(UTC).isoformat()


def optional_timestamp(value: object) -> str | None:
    return None if value is None else timestamp(value)


def day(value: object) -> str:
    text = safe_string(value, limit=10)
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text) is None:
        raise InvalidPayload
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise InvalidPayload from exc


def object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise InvalidPayload
    return value


def fingerprint(*parts: str) -> str:
    encoded = json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def opaque_identity(scope_key: str, *, account: bool = False) -> str:
    return f"{'account' if account else 'credential'}:{fingerprint(scope_key)[:16]}"


def _identity(value: object) -> str | None:
    if value is None:
        return None
    text = safe_string(value)
    if re.fullmatch(r"(?:account|credential):[a-f0-9]{12,64}", text) is None:
        raise InvalidPayload
    return text


def _optional_number(value: object) -> float | None:
    return None if value is None else number(value)


def _window(payload: object, secrets: Sequence[str]) -> QuotaWindow:
    raw = object_dict(payload)
    duration = raw["duration_minutes"]
    return QuotaWindow(
        safe_string(raw["key"], secrets=secrets),
        safe_string(raw["label"], secrets=secrets),
        None if raw["used_percent"] is None else number(raw["used_percent"], nonnegative=True),
        None if duration is None else count(duration),
        optional_timestamp(raw["resets_at"]),
    )


def _activity_row(payload: object, secrets: Sequence[str]) -> ActivityRow:
    raw = object_dict(payload)
    return ActivityRow(
        day(raw["date"]),
        safe_string(raw["model"], secrets=secrets),
        safe_string(raw["model_permaslug"], secrets=secrets),
        safe_string(raw["provider_name"], secrets=secrets),
        safe_string(raw["endpoint_id"], secrets=secrets),
        count(raw["prompt_tokens"]),
        count(raw["completion_tokens"]),
        count(raw["cached_tokens"]),
        count(raw["reasoning_tokens"]),
        count(raw["requests"]),
        number(raw["usage_usd"]),
        number(raw["byok_usage_usd"]),
    )


def _data(scope: Scope, payload: object, secrets: Sequence[str]) -> ReportData:
    raw = object_dict(payload)
    if scope == "subscription":
        windows = raw["windows"]
        if not isinstance(windows, list) or len(windows) > MAX_WINDOWS:
            raise InvalidPayload
        return SubscriptionData(
            optional_string(raw["plan"], secrets=secrets),
            _identity(raw["account_label"]),
            tuple(_window(window, secrets) for window in windows),
        )
    if scope == "key":
        return KeySpendData(
            number(raw["usage_usd"]),
            number(raw["usage_daily_usd"]),
            number(raw["usage_weekly_usd"]),
            number(raw["usage_monthly_usd"]),
            number(raw["byok_usage_usd"]),
            number(raw["byok_usage_daily_usd"]),
            number(raw["byok_usage_weekly_usd"]),
            number(raw["byok_usage_monthly_usd"]),
            _optional_number(raw["limit_usd"]),
            _optional_number(raw["limit_remaining_usd"]),
            optional_string(raw["limit_reset"], secrets=secrets),
            boolean(raw["include_byok_in_limit"]),
        )
    if scope == "credits":
        return CreditsData(
            *(number(raw[key]) for key in ("total_credits_usd", "total_usage_usd", "balance_usd"))
        )
    rows = raw["rows"]
    if not isinstance(rows, list) or len(rows) > MAX_ACTIVITY_ROWS or raw["timezone"] != "UTC":
        raise InvalidPayload
    since, until = day(raw["since"]), day(raw["until"])
    if since > until:
        raise InvalidPayload
    return ActivityData(
        since,
        until,
        "UTC",
        safe_string(raw["account_scope"], secrets=secrets),
        tuple(_activity_row(row, secrets) for row in rows),
    )


def result_from_dict(payload: object, *, secrets: Sequence[str] = ()) -> ProviderResult:
    try:
        raw = object_dict(payload)
        provider, scope, status = raw["provider"], raw["scope"], raw["status"]
        if provider not in ("claude", "codex", "openrouter"):
            raise InvalidPayload
        if scope not in ("subscription", "key", "activity", "credits"):
            raise InvalidPayload
        if (provider == "openrouter") == (scope == "subscription"):
            raise InvalidPayload
        if status not in ("ok", "not_configured", "uncached", "error"):
            raise InvalidPayload
        diagnostic = None
        if raw["diagnostic"] is not None:
            diag = object_dict(raw["diagnostic"])
            diagnostic = Diagnostic(
                safe_string(diag["code"], secrets=secrets, limit=64),
                safe_string(diag["message"], secrets=secrets),
                optional_timestamp(diag["retry_at"]),
            )
        data = None if raw["data"] is None else _data(scope, raw["data"], secrets)
        fetched_at = optional_timestamp(raw["fetched_at"])
        if status == "ok" and (data is None or fetched_at is None):
            raise InvalidPayload
        return ProviderResult(
            provider,
            scope,
            _identity(raw["identity"]),
            status,
            fetched_at,
            boolean(raw["cached"]),
            boolean(raw["stale"]),
            data,
            diagnostic,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise InvalidPayload from exc


def _data_dict(data: ReportData) -> dict[str, object]:
    if isinstance(data, SubscriptionData):
        return {
            "plan": data.plan,
            "account_label": data.account_label,
            "windows": [
                {
                    "key": window.key,
                    "label": window.label,
                    "used_percent": window.used_percent,
                    "duration_minutes": window.duration_minutes,
                    "resets_at": window.resets_at,
                }
                for window in data.windows
            ],
        }
    if isinstance(data, ActivityData):
        return {
            "since": data.since,
            "until": data.until,
            "timezone": data.timezone,
            "account_scope": data.account_scope,
            "rows": [
                {
                    "date": row.date,
                    "model": row.model,
                    "model_permaslug": row.model_permaslug,
                    "provider_name": row.provider_name,
                    "endpoint_id": row.endpoint_id,
                    "prompt_tokens": row.prompt_tokens,
                    "completion_tokens": row.completion_tokens,
                    "cached_tokens": row.cached_tokens,
                    "reasoning_tokens": row.reasoning_tokens,
                    "requests": row.requests,
                    "usage_usd": row.usage_usd,
                    "byok_usage_usd": row.byok_usage_usd,
                }
                for row in data.rows
            ],
        }
    if isinstance(data, CreditsData):
        return {
            "total_credits_usd": data.total_credits_usd,
            "total_usage_usd": data.total_usage_usd,
            "balance_usd": data.balance_usd,
        }
    return {
        "usage_usd": data.usage_usd,
        "usage_daily_usd": data.usage_daily_usd,
        "usage_weekly_usd": data.usage_weekly_usd,
        "usage_monthly_usd": data.usage_monthly_usd,
        "byok_usage_usd": data.byok_usage_usd,
        "byok_usage_daily_usd": data.byok_usage_daily_usd,
        "byok_usage_weekly_usd": data.byok_usage_weekly_usd,
        "byok_usage_monthly_usd": data.byok_usage_monthly_usd,
        "limit_usd": data.limit_usd,
        "limit_remaining_usd": data.limit_remaining_usd,
        "limit_reset": data.limit_reset,
        "include_byok_in_limit": data.include_byok_in_limit,
    }


def _result_dict(result: ProviderResult) -> dict[str, object]:
    diagnostic = result.diagnostic
    return {
        "provider": result.provider,
        "scope": result.scope,
        "identity": result.identity,
        "status": result.status,
        "fetched_at": result.fetched_at,
        "cached": result.cached,
        "stale": result.stale,
        "data": None if result.data is None else _data_dict(result.data),
        "diagnostic": None
        if diagnostic is None
        else {
            "code": diagnostic.code,
            "message": diagnostic.message,
            "retry_at": diagnostic.retry_at,
        },
    }


def result_to_dict(result: ProviderResult, *, secrets: Sequence[str] = ()) -> dict[str, object]:
    return _result_dict(result_from_dict(_result_dict(result), secrets=secrets))


def results_json(
    results: Sequence[ProviderResult], *, kind: ReportKind, secrets: Sequence[str] = ()
) -> str:
    if kind not in ("subscription", "token") or any(
        (result.scope == "subscription") != (kind == "subscription") for result in results
    ):
        raise InvalidPayload
    return json.dumps(
        {
            "version": 1,
            "kind": kind,
            "results": [result_to_dict(result, secrets=secrets) for result in results],
        },
        indent=2,
        allow_nan=False,
    )
