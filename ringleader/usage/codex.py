from __future__ import annotations

import contextlib
import itertools
import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import paths, stateio
from ..sources.base import stat_fingerprint
from ..sources.codex import _meta, default_codex_dir
from .base import (
    TokenCounts,
    UsageRecord,
    WindowSnapshot,
    as_dict,
    as_float_or_none,
    as_int,
    latest_day_of,
    newest_first,
    parse_iso,
    session_files,
)

_WEEKLY_MINUTES = 1440
_UUID_DASH_PARTS = 5
_KINDS = ("5h", "weekly")
_LONGEST_WINDOW = timedelta(weeks=1)
_REPLAY_BURST = timedelta(seconds=1)
_CACHED_FIELDS = 2
REPLAY_VERSION = 2


def rollout_session_id(path: Path) -> str:
    stem = path.stem  # rollout-<ISO-ts>-<uuid>
    parts = stem.split("-")
    return "-".join(parts[-_UUID_DASH_PARTS:]) if len(parts) >= _UUID_DASH_PARTS else stem


def _ordinal_start(
    first: tuple[int, Mapping[str, Any]],
    rest: Iterator[tuple[int, Mapping[str, Any]]],
    ordinal: int,
) -> int:
    if first[0] >= ordinal:
        return max(ordinal, 0)
    begun = parse_iso(first[1].get("timestamp"))
    burst: int | None = None
    last, count = first[0], 1
    for index, record in rest:
        if index >= ordinal:
            return max(ordinal, 0)
        last, count = index, count + 1
        at = parse_iso(record.get("timestamp")) if burst is None and begun else None
        if at is not None and begun is not None and at - begun > _REPLAY_BURST:
            burst = index
    if begun is None:
        return count
    return last + 1 if burst is None else burst


def _settings_applied(record: Mapping[str, Any], own_id: object) -> bool:
    payload = as_dict(record.get("payload"))
    return payload.get("type") == "thread_settings_applied" and payload.get("thread_id") == own_id


def own_events_start(records: Iterable[tuple[int, Mapping[str, Any]]]) -> int:
    rows = iter(records)
    first = next(rows, None)
    if first is None:
        return 0
    meta = as_dict(first[1].get("payload"))
    if not meta.get("forked_from_id"):
        return 0
    ordinal = meta.get("subagent_history_start_ordinal")
    if isinstance(ordinal, int) and not isinstance(ordinal, bool):
        return _ordinal_start(first, rows, ordinal)
    own_id = meta.get("id")
    return next((index for index, record in rows if _settings_applied(record, own_id)), 0)


def codex_tokens(last: Mapping[str, Any]) -> TokenCounts:
    raw_input = as_int(last.get("input_tokens", 0))
    cache_read = as_int(last.get("cached_input_tokens", 0))
    return TokenCounts(
        input=max(raw_input - cache_read, 0),
        output=as_int(last.get("output_tokens", 0)),
        cache_read=cache_read,
        cache_write=as_int(last.get("cache_write_input_tokens", 0)),
        context=raw_input,
        reasoning=as_int(last.get("reasoning_output_tokens", 0)),
    )


def turn_key(timestamp: object, info: Mapping[str, Any]) -> tuple[Any, ...]:
    return (timestamp, as_dict(info.get("last_token_usage")), info.get("total_token_usage"))


def own_thread(payload: Mapping[str, Any], own_id: str) -> bool:
    thread = payload.get("thread_id")
    return not thread or thread == own_id


def _head(f: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    try:
        fh = f.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for index, line in enumerate(fh):
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict):
                yield index, o


type Head = Callable[[Path], Iterable[tuple[int, Mapping[str, Any]]]]


def _fingerprint(f: Path) -> str | None:
    try:
        return stat_fingerprint(f.stat())
    except OSError:
        return None


def _load_starts(store: Path) -> dict[str, tuple[str, int]]:
    try:
        raw = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("v") != REPLAY_VERSION:
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return {}
    return {
        key: (value[0], value[1])
        for key, value in entries.items()
        if isinstance(value, list)
        and len(value) == _CACHED_FIELDS
        and isinstance(value[0], str)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    }


def _forked_start(rows: Iterator[tuple[int, Mapping[str, Any]]]) -> int | None:
    first = next(rows, None)
    if first is None or not as_dict(first[1].get("payload")).get("forked_from_id"):
        return None
    return own_events_start(itertools.chain([first], rows))


class StartCache:
    def __init__(self, store: Path | None = None, head: Head | None = None) -> None:
        self._store = store if store is not None else paths.codex_starts_path()
        self._head: Head = head or _head
        self._entries = _load_starts(self._store)
        self._dirty = False

    def start(self, f: Path) -> int:
        fingerprint = _fingerprint(f)
        cached = self._entries.get(str(f))
        if fingerprint is not None and cached is not None and cached[0] == fingerprint:
            return cached[1]
        found = _forked_start(iter(self._head(f)))
        if found is None:
            return 0
        if fingerprint is not None:
            self._entries[str(f)] = (fingerprint, found)
            self._dirty = True
        return found

    def flush(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        entries = {key: list(value) for key, value in self._entries.items() if Path(key).exists()}
        with contextlib.suppress(OSError):
            paths.ensure_analysis_dir(self._store.parent)
            stateio.atomic_write_text(
                self._store, json.dumps({"v": REPLAY_VERSION, "entries": entries})
            )


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(int(epoch), UTC).isoformat()


@dataclass
class CodexUsageReader:
    name: str = "codex"
    sessions_dir: Path = field(default_factory=default_codex_dir)
    starts: StartCache | None = None

    def __post_init__(self) -> None:
        self.sessions_dir = Path(self.sessions_dir)
        self._meta_path: Path | None = None
        self._meta_cwd: str | None = None

    def available(self) -> bool:
        return self.sessions_dir.exists()

    def _files(self) -> list[Path]:
        return list(self.sessions_dir.glob("**/*.jsonl"))

    def _payloads(self) -> Iterator[tuple[Path, int, dict[str, Any], dict[str, Any]]]:
        if not self.available():
            return
        for f in sorted(self._files()):
            yield from self._file_payloads(f)

    def _file_payloads(self, f: Path) -> Iterator[tuple[Path, int, dict[str, Any], dict[str, Any]]]:
        try:
            fh = f.open(encoding="utf-8", errors="replace")
        except OSError:
            return
        with fh:
            for ordinal, line in enumerate(fh):
                if '"token_count"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = as_dict(o.get("payload"))
                if payload.get("type") != "token_count":
                    continue
                yield f, ordinal, o, payload

    def _usage_payloads(
        self, f: Path, starts: StartCache
    ) -> Iterator[tuple[Path, int, dict[str, Any], dict[str, Any], str | None]]:
        start = starts.start(f)
        own_id = rollout_session_id(f)
        model: str | None = None
        try:
            fh = f.open(encoding="utf-8", errors="replace")
        except OSError:
            return
        with fh:
            for ordinal, line in enumerate(fh):
                if ordinal < start or (
                    '"token_count"' not in line and '"turn_context"' not in line
                ):
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = as_dict(o.get("payload"))
                if o.get("type") == "turn_context":
                    model = payload.get("model") or model
                elif payload.get("type") == "token_count" and own_thread(payload, own_id):
                    yield f, ordinal, o, payload, model

    def _project(self, path: Path) -> str | None:
        if path != self._meta_path:
            self._meta_path = path
            try:
                self._meta_cwd = _meta(path).get("cwd")
            except (OSError, json.JSONDecodeError):
                self._meta_cwd = None
        return self._meta_cwd

    def latest_day(self) -> str | None:
        if not self.available():
            return None
        return latest_day_of(self._files(), lambda f: self._records([f]))

    def _start_cache(self) -> StartCache:
        if self.starts is None:
            self.starts = StartCache()
        return self.starts

    def flush(self) -> None:
        if self.starts is not None:
            self.starts.flush()

    def records(
        self, since: date | None = None, session_prefix: str | None = None
    ) -> Iterator[UsageRecord]:
        if not self.available():
            return iter(())
        return self._records(
            session_files(self._files(), since, session_prefix, rollout_session_id)
        )

    def _records(self, files: Iterable[Path]) -> Iterator[UsageRecord]:
        prev: tuple[Any, ...] | None = None
        prev_file: Path | None = None
        starts = self._start_cache()
        for f, ordinal, o, payload, model in (
            row for path in files for row in self._usage_payloads(path, starts)
        ):
            info = as_dict(payload.get("info"))
            last = as_dict(info.get("last_token_usage"))
            if not last:
                continue
            ts = o.get("timestamp")
            if not ts:
                continue
            # codex writes one token_count per rate-limit family; same turn, repeated
            turn = turn_key(ts, info)
            if f == prev_file and turn == prev:
                continue
            prev_file, prev = f, turn
            tokens = codex_tokens(last)
            yield UsageRecord(
                source="codex",
                session_id=rollout_session_id(f),
                project=self._project(f),
                timestamp=ts,
                provider="openai",
                model=model if isinstance(model, str) else None,
                input=tokens.input,
                output=tokens.output,
                cache_read=tokens.cache_read,
                cache_write=tokens.cache_write,
                reasoning=tokens.reasoning,
                total=tokens.total,
                record_id=f"{f}|{ordinal}",
            )

    def windows(self) -> Iterator[WindowSnapshot]:
        for _f, _ordinal, o, payload in self._payloads():
            yield from _snapshots(o, payload)

    def latest_windows(self) -> list[WindowSnapshot]:
        if not self.available():
            return []
        best: dict[str, tuple[WindowSnapshot, Path]] = {}
        for ceiling, f in newest_first(self._files()):
            floor = _scan_floor(best)
            if floor is not None and ceiling < floor:
                break
            own: dict[str, WindowSnapshot] = {}
            for _f, _ordinal, o, payload in self._file_payloads(f):
                for w in _snapshots(o, payload):
                    cur = own.get(w.window)
                    if cur is None or w.timestamp > cur.timestamp:
                        own[w.window] = w
            for kind, w in own.items():
                cur_best = best.get(kind)
                if (
                    cur_best is None
                    or w.timestamp > cur_best[0].timestamp
                    or (w.timestamp == cur_best[0].timestamp and f < cur_best[1])
                ):
                    best[kind] = (w, f)
        return sorted((w for w, _ in best.values()), key=lambda w: w.timestamp, reverse=True)


def _scan_floor(best: dict[str, tuple[WindowSnapshot, Path]]) -> datetime | None:
    seen = [t for w, _ in best.values() if (t := parse_iso(w.timestamp)) is not None]
    if not seen:
        return None
    if len(seen) == len(_KINDS):
        return min(seen)
    return max(seen) - _LONGEST_WINDOW


def _snapshots(o: dict[str, Any], payload: dict[str, Any]) -> Iterator[WindowSnapshot]:
    limits = as_dict(payload.get("rate_limits"))
    ts = o.get("timestamp") or ""
    plan = limits.get("plan_type")
    for slot in ("primary", "secondary"):
        w = as_dict(limits.get(slot))
        if not w:
            continue
        used = as_float_or_none(w.get("used_percent"))
        if used is None:
            continue
        minutes = as_int(w.get("window_minutes"))
        resets = as_float_or_none(w.get("resets_at"))
        yield WindowSnapshot(
            source="codex",
            timestamp=ts,
            plan_type=plan,
            window="weekly" if minutes >= _WEEKLY_MINUTES else "5h",
            window_minutes=minutes,
            used_percent=used,
            resets_at=_iso(resets) if resets else "",
        )
