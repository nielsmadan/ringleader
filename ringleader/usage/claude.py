from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..sources.claude_code import default_projects_dir
from .base import (
    UsageRecord,
    as_dict,
    as_int,
    files_since,
    latest_day_of,
    project_dir,
    total_of,
)


def decode_project(raw: str) -> str:
    return "/" + raw.strip("-").replace("-", "/")


def _subagents_dir(path: Path, root: Path) -> Path | None:
    try:
        ancestors = path.relative_to(root).parents
    except ValueError:
        return None
    return next((a for a in ancestors if a.name == "subagents"), None)


def session_of(path: Path, root: Path) -> str:
    sub = _subagents_dir(path, root)
    return sub.parent.name if sub else path.stem


def _credit_scope(files: list[Path], session_prefix: str, root: Path) -> list[Path]:
    hits = [i for i, f in enumerate(files) if session_of(f, root).startswith(session_prefix)]
    if not hits:
        return []
    dirs = {project_dir(files[i], root) for i in hits}
    return [f for f in files[: hits[-1] + 1] if project_dir(f, root) in dirs]


@dataclass
class ClaudeUsageReader:
    name: str = "claude"
    projects_dir: Path = field(default_factory=default_projects_dir)

    def __post_init__(self) -> None:
        self.projects_dir = Path(self.projects_dir)

    def available(self) -> bool:
        return self.projects_dir.exists()

    def _files(self) -> list[Path]:
        return [
            *self.projects_dir.glob("*/*.jsonl"),
            *self.projects_dir.glob("*/*/subagents/agent-*.jsonl"),
            *self.projects_dir.glob("*/*/subagents/workflows/*/agent-*.jsonl"),
        ]

    def records(
        self, since: date | None = None, session_prefix: str | None = None
    ) -> Iterator[UsageRecord]:
        if not self.available():
            return
        files = sorted(files_since(self._files(), since), key=str)
        if session_prefix:
            files = _credit_scope(files, session_prefix, self.projects_dir)
        seen: set[tuple[Any, ...]] = set()
        credited = (r for f in files for r in self._file_records(f, seen))
        yield from (r for r in credited if r.session_id.startswith(session_prefix or ""))

    def latest_day(self) -> str | None:
        if not self.available():
            return None
        return latest_day_of(self._files(), lambda f: self._file_records(f, set()))

    def _file_records(self, f: Path, seen: set[tuple[Any, ...]]) -> Iterator[UsageRecord]:
        try:
            fh = f.open(encoding="utf-8", errors="replace")
        except OSError:
            return
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

    def _record(self, o: dict[str, Any], f: Path) -> UsageRecord | None:
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
        cw_1h = min(
            as_int(as_dict(usage.get("cache_creation")).get("ephemeral_1h_input_tokens")), cw
        )
        return UsageRecord(
            source="claude",
            session_id=session_of(f, self.projects_dir),
            project=o.get("cwd") or decode_project(project_dir(f, self.projects_dir)),
            timestamp=ts,
            provider="anthropic",
            model=model,
            input=inp,
            output=out,
            cache_read=cr,
            cache_write=cw,
            cache_write_1h=cw_1h,
            nested=_subagents_dir(f, self.projects_dir) is not None,
            reasoning=0,
            total=total_of(input=inp, cache_read=cr, cache_write=cw, output=out),
        )
