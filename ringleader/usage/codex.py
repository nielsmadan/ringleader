from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..sources.codex import _meta, default_codex_dir
from .base import UsageRecord, WindowSnapshot, as_dict, as_float_or_none, as_int, total_of

_WEEKLY_MINUTES = 1440
_UUID_DASH_PARTS = 5


def _session_id(path: Path) -> str:
    stem = path.stem  # rollout-<ISO-ts>-<uuid>
    parts = stem.split("-")
    return "-".join(parts[-_UUID_DASH_PARTS:]) if len(parts) >= _UUID_DASH_PARTS else stem


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(int(epoch), UTC).isoformat()


@dataclass
class CodexUsageReader:
    name: str = "codex"
    sessions_dir: Path = field(default_factory=default_codex_dir)

    def __post_init__(self) -> None:
        self.sessions_dir = Path(self.sessions_dir)
        self._meta_path: Path | None = None
        self._meta_cwd: str | None = None

    def available(self) -> bool:
        return self.sessions_dir.exists()

    def _payloads(self) -> Iterator[tuple[Path, int, dict[str, Any], dict[str, Any]]]:
        if not self.available():
            return
        for f in sorted(self.sessions_dir.glob("**/*.jsonl")):
            try:
                fh = f.open(encoding="utf-8", errors="replace")
            except OSError:
                continue
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

    def _project(self, path: Path) -> str | None:
        if path != self._meta_path:
            self._meta_path = path
            try:
                self._meta_cwd = _meta(path).get("cwd")
            except (OSError, json.JSONDecodeError):
                self._meta_cwd = None
        return self._meta_cwd

    def records(self) -> Iterator[UsageRecord]:
        prev: tuple[Any, ...] | None = None
        prev_file: Path | None = None
        for f, ordinal, o, payload in self._payloads():
            info = as_dict(payload.get("info"))
            last = as_dict(info.get("last_token_usage"))
            if not last:
                continue
            ts = o.get("timestamp")
            if not ts:
                continue
            # codex writes one token_count per rate-limit family; same turn, repeated
            turn = (
                ts,
                json.dumps(last, sort_keys=True),
                json.dumps(info.get("total_token_usage"), sort_keys=True),
            )
            if f == prev_file and turn == prev:
                continue
            prev_file, prev = f, turn
            cache_read = as_int(last.get("cached_input_tokens", 0))
            # codex nests cached inside input; the schema wants disjoint buckets
            inp = max(as_int(last.get("input_tokens", 0)) - cache_read, 0)
            cw = as_int(last.get("cache_write_input_tokens", 0))
            out = as_int(last.get("output_tokens", 0))
            yield UsageRecord(
                source="codex",
                session_id=_session_id(f),
                project=self._project(f),
                timestamp=ts,
                provider="openai",
                model=None,
                input=inp,
                output=out,
                cache_read=cache_read,
                cache_write=cw,
                reasoning=as_int(last.get("reasoning_output_tokens", 0)),
                total=total_of(input=inp, cache_read=cache_read, cache_write=cw, output=out),
                record_id=f"{f}|{ordinal}",
            )

    def windows(self) -> Iterator[WindowSnapshot]:
        for _f, _ordinal, o, payload in self._payloads():
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
