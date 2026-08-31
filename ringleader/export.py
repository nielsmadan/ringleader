from __future__ import annotations

import json
from pathlib import Path

from . import scanner
from .sources.base import ConversationRenderingSource
from .transcript import _text_blocks


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
            t = o.get("type")
            if t == "user":
                txt = _text_blocks(o.get("message", {})).strip()
                if txt:
                    out.append(f"## USER\n\n{txt}\n")
            elif t == "assistant":
                msg = o.get("message", {})
                txt = _text_blocks(msg).strip()
                if txt:
                    out.append(f"## ASSISTANT\n\n{txt}\n")
                content = msg.get("content")
                tools = (
                    [
                        name
                        for b in content
                        if isinstance(b, dict)
                        and b.get("type") == "tool_use"
                        and isinstance(name := b.get("name"), str)
                        and name
                    ]
                    if isinstance(content, list)
                    else []
                )
                if tools:
                    out.append(f"_(tools: {', '.join(tools)})_\n")
            elif t == "response_item":
                p = o.get("payload", {})
                if p.get("type") == "message":
                    role = p.get("role")
                    content = p.get("content")
                    txt = (
                        " ".join(c.get("text", "") for c in content if isinstance(c, dict)).strip()
                        if isinstance(content, list)
                        else ""
                    )
                    if txt and role == "user":
                        out.append(f"## USER\n\n{txt}\n")
                    elif txt and role == "assistant":
                        out.append(f"## ASSISTANT\n\n{txt}\n")
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
