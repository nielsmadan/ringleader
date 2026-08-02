from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from .. import projects, registry, scanner
from ..sources.claude_code import ClaudeCodeSource
from . import default_registry


def _now() -> str:
    return datetime.now(UTC).isoformat()


def cmd_scan(args):
    reg = registry.load_registry(args.registry)
    scanned = scanner.scan(sources=[ClaudeCodeSource(tail_turns=args.tail)])
    tails = {ns.session_id: ns.tail_text for ns in scanned}
    reg = registry.reconcile(reg, scanned, _now())
    registry.save_registry(args.registry, reg)

    pending = registry.sessions_needing_classification(reg)
    pending = projects.filter_by_projects(pending, getattr(args, "project", None))
    emit = [
        {
            "sessionId": t["sessionId"],
            "project": t["project"],
            "gitProject": t["gitProjectLabel"],
            "gitBranch": t["gitBranch"],
            "customTitle": t["title"],
            "aiTitle": t["aiTitle"],
            "lastPrompt": t["lastPrompt"],
            "messageCount": t["messageCount"],
            "tail": tails.get(t["sessionId"], "")[:6000],
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


def cmd_apply(args):
    reg = registry.load_registry(args.registry)
    registry.apply_classification(
        reg,
        args.session,
        status=args.status,
        what_it_is=args.what,
        suggested_name=args.name,
        confidence=args.confidence,
    )
    registry.save_registry(args.registry, reg)
    print(f"applied {args.session}: {args.status}")


def cmd_sync(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl sync")
    ap.add_argument("--registry", default=str(default_registry()))
    sub = ap.add_subparsers(required=True, dest="cmd")

    s = sub.add_parser("scan", help="reconcile registry; emit pending classifications")
    s.add_argument("--tail", type=int, default=30)
    s.add_argument(
        "--project",
        action="append",
        default=None,
        help="repo/dir selector; repeatable — filters emitted pending",
    )
    s.set_defaults(fn=cmd_scan)

    a = sub.add_parser("apply", help="write one classification back")
    a.add_argument("--session", required=True)
    a.add_argument(
        "--status", required=True, choices=["active", "unfinished", "blocked", "finished"]
    )
    a.add_argument("--what", required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--confidence", type=float, default=None)
    a.set_defaults(fn=cmd_apply)

    args = ap.parse_args(argv)
    args.fn(args)
    return 0
