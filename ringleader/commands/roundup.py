from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import FrameType

from .. import refresh, revalidate
from .. import roundup as roundup_api
from ..config import load_config
from ..models import active_model, proc
from ..models.base import Model
from ..paths import archive_root
from ..presence import PresenceClient
from . import default_registry

_ENRICH_PROGRESS_EVERY = 10
_CANCELLED_EXIT = 130


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _live_sessions() -> frozenset[tuple[str, str]]:
    return PresenceClient().require_running_session_ids()


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask_scope(plan: roundup_api.RoundupPlan) -> roundup_api.EnrichScope:
    prompt = (
        f"enrich: [a]ll {len(plan.enrich_all_ids):,} "
        f"(~{roundup_api.duration(plan.estimated_seconds('all'))}) / "
        f"[r]ecent {len(plan.enrich_recent_ids):,} "
        f"(~{roundup_api.duration(plan.estimated_seconds('recent'))}) / [s]kip? [r] "
    )
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        answer = ""
    if answer in ("a", "all"):
        return "all"
    if answer in ("s", "skip"):
        return "none"
    return "recent"


def _choose_scope(
    plan: roundup_api.RoundupPlan, explicit: roundup_api.EnrichScope | None, *, yes: bool
) -> roundup_api.EnrichScope:
    if explicit is not None:
        return explicit
    if not plan.enrich_warning:
        return "all"
    if _interactive() and not yes:
        return _ask_scope(plan)
    rest = len(plan.enrich_all_ids) - len(plan.enrich_recent_ids)
    rest_seconds = plan.estimated_seconds("all") - plan.estimated_seconds("recent")
    print(
        f"enrich: {len(plan.enrich_all_ids):,} pending — enriching the "
        f"{len(plan.enrich_recent_ids):,} from the last {roundup_api.RECENT_DAYS} days; "
        f"run `rl roundup --enrich all` for the rest "
        f"({rest:,}, about {roundup_api.duration(max(rest_seconds, 0.0))})"
    )
    return "recent"


def _print_progress(event: roundup_api.RoundupProgress) -> None:
    if event.phase == "enrich":
        if event.done % _ENRICH_PROGRESS_EVERY == 0 or event.done == event.total:
            print(f"enriching {event.done}/{event.total}", file=sys.stderr)
        return
    if event.done == 0 and event.detail is None:
        print(f"{event.phase}…", file=sys.stderr)


@contextmanager
def _sigint_cancels(cancel: threading.Event) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handle(signum: int, frame: FrameType | None) -> None:
        if cancel.is_set():
            signal.signal(signal.SIGINT, signal.default_int_handler)
            raise KeyboardInterrupt
        cancel.set()
        print("stopping after the current item… (Ctrl-C again to abort)", file=sys.stderr)

    previous = signal.signal(signal.SIGINT, handle)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _report(result: roundup_api.RoundupResult) -> None:
    phases = result.phases
    for warning in phases.warnings:
        print(warning, file=sys.stderr)
    for item, reason in phases.skipped:
        print(f"skipped {item.label}: {reason}", file=sys.stderr)
    enriched = result.enrich
    if enriched is not None:
        for error in enriched.errors:
            print(f"skipped {error}", file=sys.stderr)
        if enriched.aborted:
            print(f"enrich stopped early: {enriched.aborted}", file=sys.stderr)
    classified, failed, stale = (
        (enriched.classified, enriched.failed, enriched.stale) if enriched else (0, 0, 0)
    )
    print(
        f"swept {phases.swept}, pruned {phases.deleted}, purged {phases.purged}, "
        f"enriched {classified} ({failed} failed, {stale} stale)"
    )


def _run(plan: roundup_api.RoundupPlan, scope: roundup_api.EnrichScope, model: Model | None) -> int:
    cancel = threading.Event()
    with _sigint_cancels(cancel):
        try:
            result = roundup_api.run_roundup(
                plan,
                scope,
                model=model,
                presence=revalidate.probe(_live_sessions),
                unknown="allow",
                progress=_print_progress,
                cancelled=cancel.is_set,
                archive_root_path=str(archive_root()),
            )
        except roundup_api.RoundupBusy as error:
            print(f"rl roundup: {error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            proc.kill_all_children()
            raise
    _report(result)
    if not result.stamped:
        print("roundup cancelled — not stamped; run it again to finish", file=sys.stderr)
        return _CANCELLED_EXIT
    return 1 if result.phases.errors else 0


def cmd_roundup(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl roundup")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--yes", action="store_true", help="never prompt")
    ap.add_argument(
        "--enrich",
        choices=("all", "recent", "none"),
        default=None,
        help="which pending sessions to classify (default: all, or recent above "
        f"{roundup_api.ENRICH_WARN_THRESHOLD} pending)",
    )
    ap.add_argument(
        "--preview", action="store_true", help="print the plan and stop (the scan still persists)"
    )
    args = ap.parse_args(argv)

    cfg = load_config()
    model = active_model(cfg, "enrich")
    now = _now()
    refreshed = refresh.refresh_registry(args.registry, now, name_filters=cfg.session_filters)
    plan = roundup_api.plan_roundup(
        args.registry,
        refreshed.reg,
        now,
        scan_note=refreshed.note,
        model=model,
        now_epoch=time.time(),
        retention_days=cfg.archive_retention_days,
        unavailable_sources=frozenset(refreshed.unavailable_sources),
    )
    for line in roundup_api.describe_plan(plan):
        print(line)
    if args.preview:
        return 0
    scope = _choose_scope(plan, args.enrich, yes=args.yes)
    return _run(plan, scope, model)
