from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import Any

from .. import projects, registry, scanner
from ..config import load_config
from ..models import active_model
from ..models.base import ModelError
from . import default_registry

ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "unfinished", "blocked", "finished"]},
        "whatItIs": {"type": "string"},
        "suggestedName": {"type": "string"},
    },
    "required": ["status", "whatItIs", "suggestedName"],
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _prompt(thread: dict[str, Any], tail: str) -> str:
    return (
        "You are classifying a DORMANT coding-agent session for a curation tool.\n"
        f"project: {thread.get('project')}\n"
        f"git branch: {thread.get('gitBranch')}\n"
        f"last prompt: {thread.get('lastPrompt')}\n"
        f"message count: {thread.get('messageCount')}\n\n"
        "Recent transcript tail:\n"
        f"{tail[:6000]}\n\n"
        "Return: status (active|unfinished|blocked|finished), whatItIs (one line), "
        "suggestedName (short kebab-case)."
    )


def cmd_enrich(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session enrich")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--project", action="append", default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    cfg = load_config()
    model = active_model(cfg)
    if model is None:
        print(
            "no model configured — set one with `rl config`, or classify via the session-sync skill"
        )
        return 0

    now = _now()
    reg = registry.load_registry(args.registry)
    unavailable_sources: set[str] = set()
    scanned = scanner.scan(
        sources=scanner.active_sources(unavailable_sources=unavailable_sources),
        unavailable_sources=unavailable_sources,
    )
    tails = {(ns.source, ns.session_id): ns.tail_text for ns in scanned}
    reg = registry.reconcile(
        reg,
        scanned,
        now,
        unavailable_sources=unavailable_sources,
    )
    registry.apply_name_filters(reg, cfg.session_filters)

    pending = registry.sessions_needing_classification(reg)
    if args.project:
        pending = projects.filter_by_projects(pending, args.project)
    unavailable = [t for t in pending if (t.get("source") or "claude-code") in unavailable_sources]
    pending = [t for t in pending if (t.get("source") or "claude-code") not in unavailable_sources]
    if args.limit is not None:
        pending = pending[: args.limit]

    done = 0
    skipped = len(unavailable)
    for t in pending:
        try:
            source_key = (t.get("source") or "claude-code", t["sessionId"])
            out = model.complete_json(_prompt(t, tails[source_key]), ENRICH_SCHEMA)
            registry.apply_classification(
                reg,
                t["sessionId"],
                status=out["status"],
                what_it_is=out["whatItIs"],
                suggested_name=out["suggestedName"],
            )
            done += 1
        except (ModelError, KeyError) as e:
            print(f"skipped {t['sessionId']}: {e}", file=sys.stderr)
            skipped += 1
    registry.save_registry(args.registry, reg)
    print(f"enriched {done}, skipped {skipped} (model: {model.name})")
    return 0
