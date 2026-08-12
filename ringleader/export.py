from __future__ import annotations

import json
from pathlib import Path

from .transcript import _text_blocks


def render_conversation(path) -> str:
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
                        b.get("name")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "tool_use"
                    ]
                    if isinstance(content, list)
                    else []
                )
                tools = [n for n in tools if n]
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
