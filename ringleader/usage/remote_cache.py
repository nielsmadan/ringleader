from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import cast

from ..paths import state_dir
from ..stateio import atomic_write_bytes
from .remote_types import (
    ActivityData,
    Diagnostic,
    InvalidPayload,
    Provider,
    ProviderResult,
    Scope,
    day,
    fingerprint,
    object_dict,
    result_from_dict,
    result_to_dict,
    safe_string,
    timestamp,
)

MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 128
MAX_ACTIVITY_DAYS = 30
MAX_RETRY_HEADER = 512
_BACKOFF_ENDPOINTS = frozenset({"usage", "key", "activity", "credits", "rateLimits"})


class CacheError(Exception):
    def __init__(self, code: str = "cache_invalid") -> None:
        self.diagnostic = Diagnostic(
            code,
            {
                "cache_invalid": "Saved usage cache is invalid; refresh to replace it.",
                "cache_repaired": "Replaced the invalid saved usage cache.",
                "cache_version": "Saved usage cache uses an unsupported version.",
                "cache_read": "Saved usage cache could not be read.",
                "cache_future": "Saved usage cache contains a future timestamp.",
                "cache_busy": "Usage cache is busy; the fetched result was not saved.",
                "cache_write": "Usage cache could not be saved.",
            }.get(code, "Usage cache is unavailable."),
        )
        super().__init__(self.diagnostic.message)


@dataclass(frozen=True)
class CacheKey:
    provider: Provider
    scope: Scope
    fingerprint: str
    since: str | None = None
    until: str | None = None
    days: int | None = None

    def fields(self) -> dict[str, object]:
        if re.fullmatch(r"[a-f0-9]{64}|unknown", self.fingerprint) is None:
            raise InvalidPayload
        if self.provider not in ("claude", "codex", "openrouter"):
            raise InvalidPayload
        if self.scope not in ("subscription", "key", "activity", "credits"):
            raise InvalidPayload
        if (self.provider == "openrouter") == (self.scope == "subscription"):
            raise InvalidPayload
        if self.scope == "activity":
            if (
                self.since is None
                or self.until is None
                or type(self.days) is not int
                or not 1 <= self.days <= MAX_ACTIVITY_DAYS
                or day(self.since) > day(self.until)
            ):
                raise InvalidPayload
        elif self.since is not None or self.until is not None or self.days is not None:
            raise InvalidPayload
        return {
            "provider": self.provider,
            "scope": self.scope,
            "fingerprint": self.fingerprint,
            "since": self.since,
            "until": self.until,
            "days": self.days,
        }

    def encoded(self) -> str:
        return fingerprint(json.dumps(self.fields(), sort_keys=True))


@dataclass(frozen=True)
class BackoffKey:
    provider: Provider
    endpoint: str
    fingerprint: str

    def encoded(self) -> str:
        if (
            self.provider not in ("claude", "codex", "openrouter")
            or self.endpoint not in _BACKOFF_ENDPOINTS
            or re.fullmatch(r"[a-f0-9]{64}|unknown", self.fingerprint) is None
        ):
            raise InvalidPayload
        return fingerprint(self.provider, self.endpoint, self.fingerprint)


def retry_after(status: int, headers: Mapping[str, str], *, now: datetime) -> str | None:
    if status not in (429, 503):
        return None
    value = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
    if value is None or len(value) > MAX_RETRY_HEADER:
        return None
    try:
        if re.fullmatch(r"[0-9]+", value):
            delay: float = min(int(value), 86400)
        else:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                return None
            delay = min(max((parsed - now).total_seconds(), 0), 86400)
        return (now + timedelta(seconds=delay)).astimezone(UTC).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _time(value: object, now: datetime) -> datetime:
    parsed = datetime.fromisoformat(timestamp(value))
    if parsed > now:
        raise CacheError("cache_future")
    return parsed


def _has_newer_success(entries: Mapping[str, object], key: CacheKey, now: datetime) -> bool:
    scope = {"provider": key.provider, "scope": key.scope, "fingerprint": key.fingerprint}
    for value in entries.values():
        entry = object_dict(value)
        fields = object_dict(entry["key"])
        if (
            all(fields[field] == expected for field, expected in scope.items())
            and (success := entry["success"]) is not None
            and datetime.fromisoformat(timestamp(object_dict(success)["fetched_at"])) > now
        ):
            return True
    return False


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        end = time.monotonic() + 1.0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise CacheError("cache_busy") from None
                time.sleep(min(0.01, max(0, end - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def utc_now() -> datetime:
    return datetime.now(UTC)


class RemoteCache:
    def __init__(
        self, path: Path | None = None, *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self.path = path if path is not None else state_dir() / "usage" / "remote-v1.json"
        self._clock = clock

    def _read(self) -> dict[str, object]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise CacheError("cache_read")
                encoded = handle.read(MAX_CACHE_BYTES + 1)
        except FileNotFoundError:
            return {"version": 1, "entries": {}, "backoffs": {}}
        except OSError as exc:
            raise CacheError("cache_read") from exc
        try:
            now = self._clock()
            if len(encoded) > MAX_CACHE_BYTES:
                raise InvalidPayload
            raw = object_dict(json.loads(encoded))
            if type(raw["version"]) is not int or raw["version"] != 1:
                raise CacheError("cache_version")
            entries, backoffs = object_dict(raw["entries"]), object_dict(raw["backoffs"])
            if len(entries) > MAX_ENTRIES or len(backoffs) > MAX_ENTRIES:
                raise InvalidPayload
            validated = {
                key: self._validated_entry(key, value, now) for key, value in entries.items()
            }
            for key, value in backoffs.items():
                if re.fullmatch(r"[a-f0-9]{64}", key) is None:
                    raise InvalidPayload
                backoff = object_dict(value)
                if set(backoff) != {"attempt_at", "retry_at"}:
                    raise InvalidPayload
                _time(backoff["attempt_at"], now)
                until = datetime.fromisoformat(timestamp(backoff["retry_at"]))
                if until > now + timedelta(hours=24):
                    raise InvalidPayload
            return {"version": 1, "entries": validated, "backoffs": backoffs}
        except CacheError:
            raise
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise CacheError from exc

    def _validated_entry(self, key: str, value: object, now: datetime) -> dict[str, object]:
        entry = object_dict(value)
        if set(entry) != {"key", "attempt_at", "last_attempt", "success"}:
            raise InvalidPayload
        fields = object_dict(entry["key"])
        if set(fields) != {"provider", "scope", "fingerprint", "since", "until", "days"}:
            raise InvalidPayload
        provider, scope, scope_fingerprint = (
            fields["provider"],
            fields["scope"],
            fields["fingerprint"],
        )
        since, until, days = fields["since"], fields["until"], fields["days"]
        if (
            not isinstance(provider, str)
            or not isinstance(scope, str)
            or not isinstance(scope_fingerprint, str)
            or (since is not None and not isinstance(since, str))
            or (until is not None and not isinstance(until, str))
            or (days is not None and type(days) is not int)
        ):
            raise InvalidPayload
        cache_key = CacheKey(
            cast(Provider, provider),
            cast(Scope, scope),
            scope_fingerprint,
            since,
            until,
            days,
        )
        if cache_key.encoded() != key:
            raise InvalidPayload
        _time(entry["attempt_at"], now)
        attempt, saved = entry["last_attempt"], entry["success"]
        if saved is not None:
            success = result_from_dict(saved)
            self._check_result(cache_key, success, now)
            if success.status != "ok":
                raise InvalidPayload
        if saved is not None and attempt == saved:
            return {**entry, "last_attempt": None}
        if attempt is not None:
            self._check_result(cache_key, result_from_dict(attempt), now)
        elif saved is None:
            raise InvalidPayload
        return entry

    @staticmethod
    def _check_result(key: CacheKey, result: ProviderResult, now: datetime) -> None:
        if result.provider != key.provider or result.scope != key.scope:
            raise InvalidPayload
        if result.fetched_at is not None:
            _time(result.fetched_at, now)
        data = result.data
        if isinstance(data, ActivityData) and (data.since, data.until) != (key.since, key.until):
            raise InvalidPayload

    @staticmethod
    def _snapshot(
        entry: dict[str, object], *, now: datetime, cache_seconds: int
    ) -> ProviderResult | None:
        if entry["success"] is None:
            return None
        success = result_from_dict(entry["success"])
        assert success.fetched_at is not None
        stale = (now - datetime.fromisoformat(success.fetched_at)).total_seconds() > cache_seconds
        if entry["last_attempt"] is not None:
            attempt = result_from_dict(entry["last_attempt"])
            if attempt.status == "error":
                return replace(
                    success, status="error", cached=True, stale=True, diagnostic=attempt.diagnostic
                )
        return replace(success, cached=True, stale=stale)

    def lookup_with_backoff(
        self, key: CacheKey, backoff: BackoffKey, *, now: datetime, cache_seconds: int
    ) -> tuple[ProviderResult | None, str | None]:
        raw = self._read()
        return (
            self._lookup(raw, key, now=now, cache_seconds=cache_seconds),
            self._retry_at(raw, backoff, now=now),
        )

    def _lookup(
        self, raw: dict[str, object], key: CacheKey, *, now: datetime, cache_seconds: int
    ) -> ProviderResult | None:
        if key.fingerprint == "unknown":
            return None
        entry = object_dict(raw["entries"]).get(key.encoded())
        return (
            None
            if entry is None
            else self._snapshot(object_dict(entry), now=now, cache_seconds=cache_seconds)
        )

    def latest_many(
        self,
        queries: Sequence[tuple[Provider, Scope, int | None]],
        *,
        now: datetime,
        cache_seconds: int,
    ) -> tuple[ProviderResult | None, ...]:
        entries = object_dict(self._read()["entries"])
        return tuple(
            self._latest(entries, query, now=now, cache_seconds=cache_seconds) for query in queries
        )

    def _latest(
        self,
        entries: dict[str, object],
        query: tuple[Provider, Scope, int | None],
        *,
        now: datetime,
        cache_seconds: int,
    ) -> ProviderResult | None:
        provider, scope, days = query
        matches = [
            object_dict(value)
            for value in entries.values()
            if object_dict(object_dict(value)["key"])["provider"] == provider
            and object_dict(object_dict(value)["key"])["scope"] == scope
            and object_dict(object_dict(value)["key"])["days"] == days
            and object_dict(value)["success"] is not None
        ]
        if not matches:
            return None
        newest = max(matches, key=lambda entry: str(object_dict(entry["success"])["fetched_at"]))
        return self._snapshot(newest, now=now, cache_seconds=cache_seconds)

    @staticmethod
    def _retry_at(raw: dict[str, object], key: BackoffKey, *, now: datetime) -> str | None:
        value = object_dict(raw["backoffs"]).get(key.encoded())
        if value is None:
            return None
        until = timestamp(object_dict(value)["retry_at"])
        return until if datetime.fromisoformat(until) > now else None

    def save(
        self,
        key: CacheKey,
        result: ProviderResult,
        *,
        now: datetime,
        backoff_key: BackoffKey | None = None,
        retry_at: str | None = None,
        secrets: Sequence[str] = (),
    ) -> Diagnostic | None:
        try:
            warning = None
            normalized = result_to_dict(result, secrets=secrets)
            result = result_from_dict(normalized)
            key.fields()
            safe_string(key.fingerprint, secrets=secrets)
            if backoff_key is not None:
                safe_string(backoff_key.fingerprint, secrets=secrets)
            with _lock(self.path.with_suffix(".lock")):
                wall_now = self._clock()
                _time(now.isoformat(), wall_now)
                self._check_result(key, result, wall_now)
                if retry_at is not None:
                    until = datetime.fromisoformat(timestamp(retry_at))
                    if backoff_key is None or until > wall_now + timedelta(hours=24):
                        raise InvalidPayload
                try:
                    raw = self._read()
                except CacheError as exc:
                    if exc.diagnostic.code != "cache_invalid":
                        raise
                    warning = CacheError("cache_repaired").diagnostic
                    raw = {"version": 1, "entries": {}, "backoffs": {}}
                entries, backoffs = object_dict(raw["entries"]), object_dict(raw["backoffs"])
                entry_id = key.encoded()
                previous = entries.get(entry_id)
                entry = (
                    object_dict(previous)
                    if previous is not None
                    else {
                        "key": key.fields(),
                        "success": None,
                        "last_attempt": None,
                        "attempt_at": now.astimezone(UTC).isoformat(),
                    }
                )
                if result.status == "ok":
                    old_success = entry["success"]
                    if old_success is not None and str(
                        object_dict(old_success)["fetched_at"]
                    ) > str(normalized["fetched_at"]):
                        return None
                    entry["success"] = normalized
                if datetime.fromisoformat(timestamp(entry["attempt_at"])) <= now:
                    entry["last_attempt"], entry["attempt_at"] = (
                        None if result.status == "ok" else normalized,
                        now.astimezone(UTC).isoformat(),
                    )
                entries[entry_id] = entry
                if backoff_key is not None and (retry_at is not None or result.status == "ok"):
                    backoff_id = backoff_key.encoded()
                    self._update_backoff(backoffs, entries, backoff_id, key, now, retry_at)
                encoded = self._bounded_encoding(entries, backoffs, entry_id)
                atomic_write_bytes(self.path, encoded, mode=0o600)
            return warning
        except CacheError as exc:
            return exc.diagnostic
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return CacheError("cache_write").diagnostic

    @staticmethod
    def _update_backoff(
        backoffs: dict[str, object],
        entries: dict[str, object],
        backoff_id: str,
        key: CacheKey,
        now: datetime,
        retry_at: str | None,
    ) -> None:
        previous = backoffs.get(backoff_id)
        if (
            previous is not None
            and datetime.fromisoformat(timestamp(object_dict(previous)["attempt_at"])) > now
        ):
            return
        if retry_at is None:
            if previous is not None:
                del backoffs[backoff_id]
        elif not _has_newer_success(entries, key, now):
            backoffs[backoff_id] = {
                "attempt_at": now.astimezone(UTC).isoformat(),
                "retry_at": timestamp(retry_at),
            }

    @classmethod
    def _bounded_encoding(
        cls, entries: dict[str, object], backoffs: dict[str, object], entry_id: str
    ) -> bytes:
        retained = cls._trim(entries)
        raw = {"version": 1, "entries": retained, "backoffs": cls._trim(backoffs)}
        while True:
            encoded = json.dumps(
                raw, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
            if len(encoded) <= MAX_CACHE_BYTES:
                return encoded
            oldest = next(iter(retained), None)
            if oldest is None or oldest == entry_id or entry_id not in retained:
                raise CacheError("cache_write")
            retained.pop(oldest)

    @staticmethod
    def _trim(values: dict[str, object]) -> dict[str, object]:
        return dict(
            sorted(values.items(), key=lambda pair: str(object_dict(pair[1])["attempt_at"]))[
                -MAX_ENTRIES:
            ]
        )
