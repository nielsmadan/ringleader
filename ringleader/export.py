from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import scanner
from .agents import source_of
from .sources.base import ConversationRenderingSource, Source
from .transcript import text_blocks


def _turn(role: object, txt: str) -> str | None:
    if not txt:
        return None
    if role == "user":
        return f"## USER\n\n{txt}\n"
    if role == "assistant":
        return f"## ASSISTANT\n\n{txt}\n"
    return None


def _tool_note(msg: dict[str, Any]) -> str | None:
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


def _codex_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(c.get("text", "") for c in content if isinstance(c, dict)).strip()


def _record_chunks(o: dict[str, Any]) -> list[str]:
    t = o.get("type")
    if t == "user":
        return [c for c in (_turn("user", text_blocks(o.get("message", {})).strip()),) if c]
    if t == "assistant":
        msg = o.get("message", {})
        parts = (_turn("assistant", text_blocks(msg).strip()), _tool_note(msg))
        return [c for c in parts if c]
    if t == "message":  # pi and droid
        msg = o.get("message")
        if not isinstance(msg, dict):
            return []
        return [c for c in (_turn(msg.get("role"), text_blocks(msg).strip()),) if c]
    if t == "response_item":  # codex
        payload = o.get("payload") or {}
        if payload.get("type") != "message":
            return []
        return [c for c in (_turn(payload.get("role"), _codex_text(payload)),) if c]
    return []


TAIL_CHUNK_BYTES = 262_144


def _parse_line(raw: str) -> dict[str, Any] | None:
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        record = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _line_chunks(lines: Iterable[str]) -> list[str]:
    return [
        chunk
        for line in lines
        if (record := _parse_line(line)) is not None
        for chunk in _record_chunks(record)
    ]


def _render_jsonl(path: str | Path) -> str:
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        return "\n".join(_line_chunks(fh))


def _last(text: str, max_chars: int) -> str:
    return text[-max_chars:] if max_chars > 0 else ""


def _decoded(data: bytes) -> list[str]:
    return [line.decode("utf-8", errors="replace") for line in data.split(b"\n")]


def _jsonl_tail(path: str | Path, max_chars: int) -> str:
    chunks: list[str] = []
    window = TAIL_CHUNK_BYTES
    with Path(path).open("rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        end = size
        while True:
            start = max(0, size - window)
            fh.seek(start)
            data = fh.read(end - start)
            if start > 0:
                cut = data.find(b"\n")
                if cut >= 0:
                    chunks = _line_chunks(_decoded(data[cut + 1 :])) + chunks
                    end = start + cut + 1
            else:
                chunks = _line_chunks(_decoded(data)) + chunks
            text = "\n".join(chunks)
            if start == 0 or len(text) >= max_chars:
                return _last(text, max_chars)
            window *= 2


def _jsonl_file(subject: dict[str, Any], source_name: str) -> str:
    file_path = subject.get("file")
    if not isinstance(file_path, str) or not file_path:
        raise RuntimeError(f"{source_name} session has no readable conversation")
    return file_path


def _native_renderer(subject: dict[str, Any], source: Source | None) -> Source | None:
    src = source or scanner.source_for_thread(subject)
    return src if src and "renders_conversation" in src.capabilities else None


def render_conversation(
    subject: dict[str, Any] | str | Path, *, source: Source | None = None
) -> str:
    if not isinstance(subject, dict):
        return _render_jsonl(subject)

    source_name = source_of(subject)
    src = _native_renderer(subject, source)
    if src is not None:
        if not isinstance(src, ConversationRenderingSource):
            raise RuntimeError(f"{source_name} advertises conversation rendering but cannot render")
        source_ref = subject.get("sourceRef") or subject.get("file")
        if not isinstance(source_ref, str) or not source_ref:
            raise RuntimeError(f"{source_name} session has no source ref")
        return src.render_conversation(source_ref)

    return _render_jsonl(_jsonl_file(subject, source_name))


def render_tail(thread: dict[str, Any], max_chars: int) -> str:
    src = _native_renderer(thread, None)
    if src is not None:
        return _last(render_conversation(thread, source=src), max_chars)
    return _jsonl_tail(_jsonl_file(thread, source_of(thread)), max_chars)
