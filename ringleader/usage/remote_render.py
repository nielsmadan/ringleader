from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from .remote_types import (
    ActivityData,
    CreditsData,
    InvalidPayload,
    KeySpendData,
    ProviderResult,
    QuotaWindow,
    ReportKind,
    SubscriptionData,
    boolean,
    count,
    object_dict,
    result_from_dict,
    result_to_dict,
    results_json,
)

_PROVIDER_NAMES = {"claude": "Claude", "codex": "Codex", "openrouter": "OpenRouter"}
INBOX_ACTIVITY_ROWS = 30


@dataclass(frozen=True)
class InboxReport:
    result: ProviderResult
    activity_total: int | None = None
    activity_saved: bool | None = None


def inbox_reports_json(results: Sequence[ProviderResult]) -> str:
    reports = []
    for result in results:
        data = result.data
        total = None
        saved = None
        display_result = result
        if isinstance(data, ActivityData):
            total = len(data.rows)
            saved = result.cached or (
                result.status == "ok"
                and (result.diagnostic is None or result.diagnostic.code == "cache_repaired")
            )
            display_result = replace(
                result, data=replace(data, rows=data.rows[:INBOX_ACTIVITY_ROWS])
            )
        reports.append(
            {
                "result": result_to_dict(display_result),
                "activity_total": total,
                "activity_saved": saved,
            }
        )
    return json.dumps(reports, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def inbox_reports(output: str) -> tuple[InboxReport, ...]:
    raw = json.loads(output)
    if not isinstance(raw, list):
        raise InvalidPayload
    reports = []
    for item in raw:
        entry = object_dict(item)
        result = result_from_dict(entry.get("result"))
        total = entry.get("activity_total")
        saved = entry.get("activity_saved")
        if isinstance(result.data, ActivityData):
            total = count(total)
            saved = boolean(saved)
            if len(result.data.rows) != min(total, INBOX_ACTIVITY_ROWS):
                raise InvalidPayload
        elif total is not None or saved is not None:
            raise InvalidPayload
        reports.append(InboxReport(result, total, saved))
    return tuple(reports)


def remote_exit_code(results: Sequence[ProviderResult], *, cached: bool, explicit: bool) -> int:
    if any(
        result.status == "error" or (explicit and result.status == "not_configured")
        for result in results
    ):
        return 1
    if cached and all(result.status == "uncached" for result in results):
        return 0
    return 0 if any(result.status == "ok" for result in results) else 1


def _quota_text(window: QuotaWindow, now: datetime) -> str:
    duration = "" if window.duration_minutes is None else f" ({window.duration_minutes:,} min)"
    title = f"  {window.label}{duration}"
    if window.resets_at is not None and datetime.fromisoformat(window.resets_at) <= now:
        return f"{title}   expired at {window.resets_at}"
    percent = "usage unknown" if window.used_percent is None else f"{window.used_percent:g}% used"
    reset = "reset unknown" if window.resets_at is None else f"resets {window.resets_at}"
    return f"{title}   {percent}   {reset}"


def _usd(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${Decimal(str(abs(value))):,f}"


def _key_text(data: KeySpendData, fetched_day: date) -> list[str]:
    lines = ["  API key spend (USD)"]
    week_start = fetched_day - timedelta(days=fetched_day.weekday())
    for label, usage, byok in (
        ("All time", data.usage_usd, data.byok_usage_usd),
        (f"{fetched_day} (UTC)", data.usage_daily_usd, data.byok_usage_daily_usd),
        (f"Week of {week_start} (UTC)", data.usage_weekly_usd, data.byok_usage_weekly_usd),
        (f"{fetched_day:%Y-%m} (UTC)", data.usage_monthly_usd, data.byok_usage_monthly_usd),
    ):
        lines.append(f"    {label:<24} OpenRouter {_usd(usage)}   BYOK {_usd(byok)}")
    limit = "unlimited" if data.limit_usd is None else _usd(data.limit_usd)
    remaining = (
        "unavailable" if data.limit_remaining_usd is None else _usd(data.limit_remaining_usd)
    )
    reset = "none" if data.limit_reset is None else data.limit_reset
    return [
        *lines,
        f"  Key spending cap {limit}   Remaining key cap {remaining}",
        f"  Key cap reset {reset}",
        f"  BYOK included in key cap {'yes' if data.include_byok_in_limit else 'no'}",
    ]


def _activity_lines(data: ActivityData, note: str) -> Iterator[str]:
    yield f"  {data.account_scope}"
    yield f"  {data.since} through {data.until} UTC (inclusive)"
    if note:
        yield note
    if not data.rows:
        yield "  No activity reported for these days."
    for row in data.rows:
        yield f"    {row.date}   {row.model}   {row.provider_name}"
        yield f"      Model version {row.model_permaslug}   Endpoint {row.endpoint_id}"
        yield (
            f"      Prompt {row.prompt_tokens:,}   Completion {row.completion_tokens:,}"
            f"   Cached {row.cached_tokens:,}   Reasoning {row.reasoning_tokens:,}"
            f"   Requests {row.requests:,}"
        )
        yield f"      OpenRouter {_usd(row.usage_usd)}   BYOK {_usd(row.byok_usage_usd)}"


def _result_lines(
    result: ProviderResult, now: datetime, *, activity_note: str = ""
) -> Iterator[str]:
    yield f"{_PROVIDER_NAMES[result.provider]} {result.scope}"
    if result.fetched_at is not None:
        identity = result.identity or "unknown identity"
        when = f"fetched {result.fetched_at}"
        if result.cached:
            when = f"last fetched {identity} at {result.fetched_at} (cached)"
        elif result.identity is not None:
            when = f"{identity}, {when}"
        stale = " (stale)" if result.stale else ""
        yield f"  {when}{stale}"
    if result.diagnostic is not None:
        yield f"  {result.diagnostic.message}"
        if result.diagnostic.retry_at is not None:
            yield f"  Retry after {result.diagnostic.retry_at}"
    data = result.data
    if isinstance(data, SubscriptionData):
        if data.plan is not None:
            yield f"  Plan {data.plan}"
        if not data.windows:
            yield "  No quota windows reported."
        yield from (_quota_text(window, now) for window in data.windows)
    elif isinstance(data, KeySpendData):
        assert result.fetched_at is not None
        yield from _key_text(data, datetime.fromisoformat(result.fetched_at).astimezone(UTC).date())
    elif isinstance(data, ActivityData):
        yield from _activity_lines(data, activity_note)
    elif isinstance(data, CreditsData):
        yield "  Credits for the authenticated user (management key, USD)"
        yield f"  Purchased {_usd(data.total_credits_usd)}   Used {_usd(data.total_usage_usd)}"
        yield f"  Balance {_usd(data.balance_usd)}"


def inbox_report_text(report: InboxReport, now: datetime, *, previous: bool = False) -> str:
    result = report.result
    if previous:
        result = replace(
            result,
            cached=result.cached or report.activity_saved is not False,
            stale=True,
            diagnostic=result.diagnostic if report.activity_saved is False else None,
        )
    note = ""
    data, total = result.data, report.activity_total
    if isinstance(data, ActivityData) and total is not None and total > len(data.rows):
        hint = (
            "Run rl usage token --report activity --cached to read the latest saved report."
            if report.activity_saved
            else "Run rl usage token --report activity --refresh to fetch a new full report."
        )
        note = f"  Showing {len(data.rows):,} of {total:,} rows. {hint}"
    return "\n".join(_result_lines(result, now, activity_note=note))


def render_results(
    results: Sequence[ProviderResult], *, kind: ReportKind, json_output: bool, now: datetime
) -> None:
    if json_output:
        print(results_json(results, kind=kind))
        return
    for index, result in enumerate(results):
        if index:
            print()
        for line in _result_lines(result, now):
            print(line)
