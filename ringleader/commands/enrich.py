from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from .. import enrichment, projects, refresh, registry
from ..config import load_config
from ..models import active_model, describe, proc
from ..progress import never_cancelled
from . import default_registry

_PROGRESS_EVERY = 10


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _report_progress(done: int, total: int) -> None:
    if done % _PROGRESS_EVERY == 0 or done == total:
        print(f"enriching {done}/{total}", file=sys.stderr)


def cmd_enrich(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session enrich")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--project", action="append", default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    cfg = load_config()
    model = active_model(cfg, "enrich")
    if model is None:
        print(
            "no model configured — set one with `rl config`, or classify via the session-sync skill"
        )
        return 0

    refreshed = refresh.refresh_registry(args.registry, _now(), name_filters=cfg.session_filters)
    pending = registry.sessions_needing_classification(refreshed.reg)
    if args.project:
        pending = projects.filter_by_projects(pending, args.project)
    unavailable = [t for t in pending if registry.identity(t)[0] in refreshed.unavailable_sources]
    pending = [t for t in pending if registry.identity(t)[0] not in refreshed.unavailable_sources]
    if args.limit is not None:
        pending = pending[: args.limit]

    try:
        outcome = enrichment.enrich_sessions(
            args.registry,
            [registry.identity(t) for t in pending],
            model=model,
            concurrency=enrichment.default_concurrency(model),
            progress=_report_progress,
            cancelled=never_cancelled,
        )
    except KeyboardInterrupt:
        proc.kill_all_children()
        raise
    for error in outcome.errors:
        print(f"skipped {error}", file=sys.stderr)
    if outcome.aborted:
        print(f"stopped early: {outcome.aborted}", file=sys.stderr)
    skipped = outcome.failed + outcome.stale + len(unavailable)
    print(f"enriched {outcome.classified}, skipped {skipped} (model: {describe(model)})")
    return 0
