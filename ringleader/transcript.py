from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sources.base import iso_timestamp


@dataclass
class TranscriptInfo:
    session_id: str
    cwd: str | None = None
    git_branch: str | None = None
    custom_title: str | None = None
    ai_title: str | None = None
    summary: str | None = None
    last_prompt: str | None = None
    message_count: int = 0
    tail_text: str = ""
    started_at: str | None = None


def _text_blocks(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
        return " ".join(p for p in parts if p)
    return ""


def parse_transcript(path: str | Path, tail_turns: int = 30) -> TranscriptInfo:
    path = Path(path)
    info = TranscriptInfo(session_id=path.stem)
    turns: deque[tuple[str, str]] = deque(maxlen=tail_turns if tail_turns > 0 else None)
    last_user: str | None = None

    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue

            if info.started_at is None:
                info.started_at = iso_timestamp(o.get("timestamp"))

            if info.cwd is None and o.get("cwd"):
                info.cwd = o["cwd"]
            if o.get("gitBranch"):
                info.git_branch = o["gitBranch"]

            t = o.get("type")
            if t == "custom-title":
                info.custom_title = o.get("customTitle") or info.custom_title
            elif t == "ai-title":
                info.ai_title = o.get("aiTitle") or info.ai_title
            elif t == "summary":
                info.summary = o.get("summary") or info.summary
            elif t == "last-prompt":
                info.last_prompt = o.get("lastPrompt") or info.last_prompt
            elif t == "user":
                info.message_count += 1
                txt = _text_blocks(o.get("message", {})).strip()
                if txt:
                    turns.append(("USER", txt))
                    last_user = txt
            elif t == "assistant":
                info.message_count += 1
                txt = _text_blocks(o.get("message", {})).strip()
                if txt:
                    turns.append(("ASSISTANT", txt))

    if not info.last_prompt and last_user:
        info.last_prompt = last_user[:500]

    info.tail_text = "\n".join(f"{s}: {x}" for s, x in turns)
    return info


def first_cwd(path: str | Path) -> str | None:
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            try:
                o = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict) and isinstance(cwd := o.get("cwd"), str) and cwd:
                return cwd
    return None
