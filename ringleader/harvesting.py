from __future__ import annotations

from pathlib import Path
from typing import Any

from . import harvests, registry

HARVEST_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "gotchas": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "whatChanged": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "gotchas", "decisions", "whatChanged"],
}


def build_prompt(thread: dict[str, Any], condensed: str) -> str:
    return (
        "Extract the DURABLE, reusable knowledge from this finished coding session for the "
        "repo's docs. Keep only what's worth remembering later; discard routine steps.\n"
        f"project: {thread.get('project')}\n\n"
        "Condensed transcript:\n"
        f"{condensed[:24000]}\n\n"
        "Return: summary (one paragraph), gotchas (non-obvious things), decisions "
        "(+rationale), whatChanged."
    )


def render_markdown(thread: dict[str, Any], data: dict[str, Any]) -> str:
    title = thread.get("title") or thread.get("suggestedName") or thread["sessionId"]
    parts = [f"# {title}\n", f"## Summary\n\n{data.get('summary', '').strip()}\n"]
    for key, heading in (
        ("gotchas", "Gotchas"),
        ("decisions", "Decisions"),
        ("whatChanged", "What changed"),
    ):
        items = [i for i in (data.get(key) or []) if i]
        if items:
            parts.append(f"## {heading}\n\n" + "\n".join(f"- {i}" for i in items) + "\n")
    return "\n".join(parts)


def save_harvest_unlocked(
    session_id: str, registry_path: str, markdown: str, *, now_iso: str
) -> tuple[dict[str, Any], Path]:
    reg = registry.load_registry(registry_path)
    thread = registry.find_session(reg, session_id)
    if thread is None:
        raise KeyError(session_id)
    path = harvests.write_harvest_unlocked(
        session_id=registry.qualified_id(thread),
        name=thread.get("title") or thread.get("suggestedName") or "",
        folder_path=thread.get("gitRoot") or thread.get("project"),
        repo_url=thread.get("gitRemote"),
        clone_url=thread.get("gitCloneUrl"),
        harvested_at=now_iso,
        content=markdown,
    )
    registry.append_harvest_path(reg, registry.qualified_id(thread), str(path))
    registry.commit_registry(registry_path, reg)
    return thread, path
