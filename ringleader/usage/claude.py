from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..sources.claude_code import default_projects_dir
from .base import UsageRecord, as_dict, as_int, total_of


def decode_project(raw: str) -> str:
    return "/" + raw.strip("-").replace("-", "/")


@dataclass
class ClaudeUsageReader:
    name: str = "claude"
    projects_dir: Path = field(default_factory=default_projects_dir)

    def __post_init__(self) -> None:
        self.projects_dir = Path(self.projects_dir)

    def available(self) -> bool:
        return self.projects_dir.exists()

    def records(self) -> Iterator[UsageRecord]:
        if not self.available():
            return
        seen: set[tuple] = set()
        for f in sorted(self.projects_dir.glob("*/*.jsonl")):
            try:
                fh = f.open(encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"assistant"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rec = self._record(o, f)
                    if rec is None:
                        continue
                    key = (as_dict(o.get("message")).get("id"), o.get("requestId"))
                    if key in seen:
                        continue
                    seen.add(key)
                    rec.record_id = f"{key[0]}|{key[1]}"
                    yield rec

    def _record(self, o: dict, f: Path) -> UsageRecord | None:
        if o.get("type") != "assistant":
            return None
        msg = as_dict(o.get("message"))
        usage = as_dict(msg.get("usage"))
        model = msg.get("model") or ""
        if not usage or "claude" not in model:
            return None
        ts = o.get("timestamp")
        if not ts:
            return None
        inp = as_int(usage.get("input_tokens", 0))
        out = as_int(usage.get("output_tokens", 0))
        cr = as_int(usage.get("cache_read_input_tokens", 0))
        cw = as_int(usage.get("cache_creation_input_tokens", 0))
        return UsageRecord(
            source="claude",
            session_id=f.stem,
            project=o.get("cwd") or decode_project(f.parent.name),
            timestamp=ts,
            provider="anthropic",
            model=model,
            input=inp,
            output=out,
            cache_read=cr,
            cache_write=cw,
            reasoning=0,
            total=total_of(input=inp, cache_read=cr, cache_write=cw, output=out),
        )
