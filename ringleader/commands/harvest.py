from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from .. import export, harvests, registry
from ..config import load_config
from ..models import active_model
from ..models.base import ModelError
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


def cmd_harvest(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session harvest")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    model = active_model(load_config())
    if model is None:
        print(
            "no model configured — set one with `rl config`, or harvest via the triage/cleanup skill"
        )
        return 0

    reg = registry.load_registry(args.registry)
    t = next((x for x in reg["threads"] if x["sessionId"] == args.id), None)
    if t is None:
        print(f"rl-harvest: unknown session {args.id}", file=sys.stderr)
        return 1

    condensed = export.render_conversation(t["file"])
    try:
        data = model.complete_json(_prompt(t, condensed), HARVEST_SCHEMA)
    except (ModelError, KeyError) as e:
        print(f"rl-harvest: {e}", file=sys.stderr)
        return 1
    md = _render_md(t, data)

    if args.dry_run:
        sys.stdout.write(md + "\n")
        return 0

    path = harvests.write_harvest(
        session_id=t["sessionId"],
        name=t.get("title") or t.get("suggestedName") or "",
        folder_path=t.get("gitRoot") or t.get("project"),
        repo_url=t.get("gitRemote"),
        harvested_at=_now(),
        content=md,
    )
    registry.mark_harvested(reg, t["sessionId"], [str(path)])
    registry.save_registry(args.registry, reg)
    print(f"harvested {t['sessionId']} -> {path}")
    return 0
