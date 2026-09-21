from __future__ import annotations

import argparse
import contextlib
import json
import sys
from datetime import UTC, datetime

from .. import projects, registry, scanner
from ..config import load_config
from . import default_registry, sweep


def _now() -> str:
    return datetime.now(UTC).isoformat()


def cmd_scan(args: argparse.Namespace) -> None:
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
        _now(),
        unavailable_sources=unavailable_sources,
    )
    registry.apply_name_filters(reg, load_config().session_filters)
    registry.commit_registry(args.registry, reg)

    pending = registry.sessions_needing_classification(reg)
    pending = projects.filter_by_projects(pending, getattr(args, "project", None))
    pending = [t for t in pending if registry.identity(t) in tails]
    emit = [
        {
            "sessionId": t["sessionId"],
            "source": registry.identity(t)[0],
            "project": t["project"],
            "gitProject": t["gitProjectLabel"],
            "gitBranch": t["gitBranch"],
            "customTitle": t["title"],
            "aiTitle": t["aiTitle"],
            "lastPrompt": t["lastPrompt"],
            "messageCount": t["messageCount"],
            "tail": tails[(t.get("source") or "claude-code", t["sessionId"])][:6000],
        }
        for t in pending
    ]
    json.dump(
        {"pendingCount": len(emit), "total": len(reg["threads"]), "pending": emit},
        sys.stdout,
        indent=2,
        ensure_ascii=False,
    )
    sys.stdout.write("\n")


def cmd_apply(args: argparse.Namespace) -> None:
    reg = registry.load_registry(args.registry)
    registry.apply_classification(
        reg,
        args.session,
        status=args.status,
        what_it_is=args.what,
        suggested_name=args.name,
        confidence=args.confidence,
    )
    registry.commit_registry(args.registry, reg)
    print(f"applied {args.session}: {args.status}")


def _add_scan_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tail", type=int, default=30)
    p.add_argument(
        "--project",
        action="append",
        default=None,
        help="repo/dir selector; repeatable — filters emitted pending",
    )


def _add_apply_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--session", required=True)
    p.add_argument(
        "--status", required=True, choices=["active", "unfinished", "blocked", "finished"]
    )
    p.add_argument("--what", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--confidence", type=float, default=None)


def cmd_refresh(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session refresh")
    ap.add_argument("--registry", default=str(default_registry()))
    _add_scan_opts(ap)
    args = ap.parse_args(argv)
    cmd_scan(args)
    # cmd_scan's JSON blob is a machine-readable contract on stdout — the sweep's
    # human-readable "archived ..." notes must not land after it, so redirect them to
    # stderr instead of dropping them.
    with contextlib.redirect_stdout(sys.stderr):
        return sweep.cmd_sweep(["--registry", args.registry])


def cmd_set(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session set")
    ap.add_argument("--registry", default=str(default_registry()))
    _add_apply_opts(ap)
    cmd_apply(ap.parse_args(argv))
    return 0
