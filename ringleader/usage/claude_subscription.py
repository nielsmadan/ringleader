from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from http import HTTPStatus

from . import remote_http
from .remote_cache import retry_after
from .remote_types import (
    MAX_WINDOWS,
    Credential,
    Diagnostic,
    InvalidPayload,
    JsonGet,
    QuotaWindow,
    SubscriptionData,
    number,
    object_dict,
    optional_string,
    optional_timestamp,
    safe_string,
)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
WEEK_MINUTES = 7 * 24 * 60
_FLAT_WINDOWS = (
    ("five_hour", "5-hour", 5 * 60),
    ("seven_day", "Weekly", WEEK_MINUTES),
    ("seven_day_opus", "Opus weekly", WEEK_MINUTES),
    ("seven_day_sonnet", "Sonnet weekly", WEEK_MINUTES),
)
_HTTP_ERRORS: dict[int, tuple[str, str]] = {
    HTTPStatus.UNAUTHORIZED: (
        "auth_denied",
        "Claude rejected the OAuth credential. Renew the Claude sign-in or token.",
    ),
    HTTPStatus.FORBIDDEN: (
        "scope_denied",
        "Claude refused usage access. Use a token with user:profile access.",
    ),
    HTTPStatus.TOO_MANY_REQUESTS: ("rate_limited", "Claude rate limited the usage request."),
    HTTPStatus.SERVICE_UNAVAILABLE: (
        "service_unavailable",
        "Claude usage service is temporarily unavailable.",
    ),
}


class ClaudeUsageError(Exception):
    def __init__(self, diagnostic: Diagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic.message)


def _window(
    key: str, label: str, duration: int, raw: dict[str, object], percent_field: str
) -> QuotaWindow:
    usage = raw.get(percent_field)
    return QuotaWindow(
        key,
        label,
        None if usage is None else number(usage, nonnegative=True),
        duration,
        optional_timestamp(raw.get("resets_at")),
    )


def _slug(model: str) -> str:
    return re.sub(r"[\W_]+", "-", model.lower()).strip("-")


def _fill_window(primary: QuotaWindow, other: QuotaWindow) -> QuotaWindow:
    return replace(
        primary,
        used_percent=primary.used_percent
        if primary.used_percent is not None
        else other.used_percent,
        resets_at=primary.resets_at or other.resets_at,
    )


def _model_window(
    raw: dict[str, object], secrets: Sequence[str]
) -> tuple[str, str, str, QuotaWindow] | None:
    model = object_dict(object_dict(raw.get("scope")).get("model"))
    model_id = optional_string(model.get("id"), secrets=secrets) or ""
    name = optional_string(model.get("display_name"), secrets=secrets) or ""
    normalized_id, normalized_name = _slug(model_id), _slug(name)
    slug = normalized_id or normalized_name
    if not slug:
        raise InvalidPayload
    shown_name = safe_string(name.strip() or model_id.strip(), limit=480)
    window = _window(f"weekly_scoped:{slug}", f"{shown_name} weekly", WEEK_MINUTES, raw, "percent")
    if normalized_name == "all-models" or slug == "all-models" or slug.endswith("-all-models"):
        return None
    return slug, normalized_id, normalized_name, window


def _scoped_windows(
    raw: object, secrets: Sequence[str]
) -> tuple[dict[str, QuotaWindow], dict[str, str]]:
    if raw is None:
        return {}, {}
    if not isinstance(raw, list):
        raise InvalidPayload
    windows: dict[str, QuotaWindow] = {}
    identifiers: defaultdict[str, set[str]] = defaultdict(set)
    aliases: defaultdict[str, set[str]] = defaultdict(set)
    for value in raw:
        limit = object_dict(value)
        group, kind = limit.get("group", ""), limit.get("kind", "")
        if not isinstance(group, str) or not isinstance(kind, str):
            raise InvalidPayload
        if group != "weekly" or kind != "weekly_scoped":
            continue
        scoped = _model_window(limit, secrets)
        if scoped is not None:
            key, model_id, name, window = scoped
            previous = windows.get(key)
            windows[key] = _fill_window(previous, window) if previous is not None else window
            if model_id:
                identifiers[model_id.removeprefix("claude-")].add(key)
            if name:
                aliases[name.removeprefix("claude-")].add(key)
        if len(windows) > MAX_WINDOWS:
            raise InvalidPayload
    matches = {
        name: next(iter(owners))
        for name, owners in (aliases | identifiers).items()
        if len(owners) == 1
    }
    return windows, matches


def parse_usage(
    contents: bytes, *, plan: str | None = None, secrets: Sequence[str] = ()
) -> SubscriptionData:
    try:
        raw = object_dict(json.loads(contents))
        flat = [
            _window(key, label, duration, object_dict(value), "utilization")
            for key, label, duration in _FLAT_WINDOWS
            if (value := raw.get(key)) is not None
        ]
        scoped, matches = _scoped_windows(raw.get("limits"), secrets)
        unscoped = []
        for window in flat:
            match = matches.get(window.key.removeprefix("seven_day_"))
            if match is None:
                unscoped.append(window)
            else:
                scoped[match] = _fill_window(scoped[match], window)
        windows = (*unscoped, *scoped.values())
        if len(windows) > MAX_WINDOWS:
            raise InvalidPayload
        return SubscriptionData(plan, None, windows)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise InvalidPayload from None


def fetch_usage(
    credential: Credential,
    plan: str | None,
    *,
    clock: Callable[[], datetime],
    deadline: float,
    transport: JsonGet,
) -> SubscriptionData:
    try:
        reply = transport(
            USAGE_URL,
            {
                "Authorization": f"Bearer {credential.access_token}",
                "anthropic-beta": "oauth-2025-04-20",
                "Accept": "application/json",
            },
            deadline=deadline,
        )
    except remote_http.RemoteHttpError as exc:
        raise ClaudeUsageError(Diagnostic(exc.code, str(exc))) from None
    if reply.status == HTTPStatus.OK:
        return parse_usage(reply.body, plan=plan, secrets=(credential.access_token,))
    code, message = _HTTP_ERRORS.get(reply.status, ("http_error", "Claude usage request failed."))
    raise ClaudeUsageError(
        Diagnostic(code, message, retry_after(reply.status, reply.headers, now=clock()))
    )
