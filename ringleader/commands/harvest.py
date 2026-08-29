from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from .. import export, harvests, registry
from ..config import load_config
from ..models import active_model
from ..models.base import ModelError
from ..stateio import publish_lock
from . import default_registry

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


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _prompt(thread: dict, condensed: str) -> str:
    return (
        "Extract the DURABLE, reusable knowledge from this finished coding session for the "
        "repo's docs. Keep only what's worth remembering later; discard routine steps.\n"
        f"project: {thread.get('project')}\n\n"
        "Condensed transcript:\n"
        f"{condensed[:24000]}\n\n"
        "Return: summary (one paragraph), gotchas (non-obvious things), decisions "
        "(+rationale), whatChanged."
    )


def _render_md(thread: dict, data: dict) -> str:
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


def _prepare_harvest(session_id: str, registry_path: str) -> tuple[int, str | None]:
    model = active_model(load_config())
    if model is None:
        print(
            "no model configured — set one with `rl config`, or harvest via the triage/cleanup skill"
        )
        return 0, None

    reg = registry.load_registry(registry_path)
    thread = next((item for item in reg["threads"] if item["sessionId"] == session_id), None)
    if thread is None:
        print(f"rl-harvest: unknown session {session_id}", file=sys.stderr)
        return 1, None
    condensed = export.render_conversation(thread["file"])
    try:
        data = model.complete_json(_prompt(thread, condensed), HARVEST_SCHEMA)
    except (ModelError, KeyError) as error:
        print(f"rl-harvest: {error}", file=sys.stderr)
        return 1, None
    return 0, _render_md(thread, data)


def _save_harvest_unlocked(session_id: str, registry_path: str, markdown: str) -> tuple[dict, Path]:
    reg = registry.load_registry(registry_path)
    thread = next((item for item in reg["threads"] if item["sessionId"] == session_id), None)
    if thread is None:
        raise KeyError(session_id)
    path = harvests._write_harvest_unlocked(
        session_id=thread["sessionId"],
        name=thread.get("title") or thread.get("suggestedName") or "",
        folder_path=thread.get("gitRoot") or thread.get("project"),
        repo_url=thread.get("gitRemote"),
        clone_url=thread.get("gitCloneUrl"),
        harvested_at=_now(),
        content=markdown,
    )
    registry.append_harvest_path(reg, thread["sessionId"], str(path))
    registry.save_registry(registry_path, reg)
    return thread, path


def cmd_harvest(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session harvest")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    rc, markdown = _prepare_harvest(args.id, args.registry)
    if rc != 0 or markdown is None:
        return rc

    if args.dry_run:
        sys.stdout.write(markdown + "\n")
        return 0

    with publish_lock():
        try:
            thread, path = _save_harvest_unlocked(args.id, args.registry, markdown)
        except KeyError:
            print(f"rl-harvest: unknown session {args.id}", file=sys.stderr)
            return 1
    print(f"harvested {thread['sessionId']} -> {path}")
    return 0
