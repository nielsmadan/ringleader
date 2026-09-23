from __future__ import annotations

import json
from pathlib import Path

from . import scanner
from .sources.base import ConversationRenderingSource
from .transcript import _text_blocks


def _turn(role: object, txt: str) -> str | None:
    if not txt:
        return None
    if role == "user":
        return f"## USER\n\n{txt}\n"
    if role == "assistant":
        return f"## ASSISTANT\n\n{txt}\n"
    return None


def _tool_note(msg: dict) -> str | None:
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    names = [
        name
        for b in content
        if isinstance(b, dict)
        and b.get("type") == "tool_use"
        and isinstance(name := b.get("name"), str)
        and name
    ]
    return f"_(tools: {', '.join(names)})_\n" if names else None


def _codex_text(payload: dict) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(c.get("text", "") for c in content if isinstance(c, dict)).strip()


def _record_chunks(o: dict) -> list[str]:
    t = o.get("type")
    if t == "user":
        return [c for c in (_turn("user", _text_blocks(o.get("message", {})).strip()),) if c]
    if t == "assistant":
        msg = o.get("message", {})
        parts = (_turn("assistant", _text_blocks(msg).strip()), _tool_note(msg))
        return [c for c in parts if c]
    if t == "message":  # pi and droid
        msg = o.get("message")
        if not isinstance(msg, dict):
            return []
        return [c for c in (_turn(msg.get("role"), _text_blocks(msg).strip()),) if c]
    if t == "response_item":  # codex
        payload = o.get("payload") or {}
        if payload.get("type") != "message":
            return []
        return [c for c in (_turn(payload.get("role"), _codex_text(payload)),) if c]
    return []


def _render_jsonl(path) -> str:
    out: list[str] = []
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            raw = raw_line.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except json.JSONDecodeError:
                continue
            out.extend(_record_chunks(o))
    return "\n".join(out)


def render_conversation(subject, *, source=None) -> str:
    if not isinstance(subject, dict):
        return _render_jsonl(subject)

    source_name = subject.get("source") or "claude-code"
    src = source or scanner.source_by_name(source_name)
    if src and "renders_conversation" in src.capabilities:
        if not isinstance(src, ConversationRenderingSource):
            raise RuntimeError(f"{source_name} advertises conversation rendering but cannot render")
        source_ref = subject.get("sourceRef") or subject.get("file")
        if not isinstance(source_ref, str) or not source_ref:
            raise RuntimeError(f"{source_name} session has no source ref")
        return src.render_conversation(source_ref)

    file_path = subject.get("file")
    if not isinstance(file_path, str) or not file_path:
        raise RuntimeError(f"{source_name} session has no readable conversation")
    return _render_jsonl(file_path)
