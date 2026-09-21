from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import Any

from .. import registry
from . import default_registry, unarchive


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _archived_tombstone(reg: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    return registry.find_session(reg, session_id, bucket="archived")


def cmd_keep(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl session keep",
        description="Keep a session long-term: it stays live and resumable but leaves the "
        "triage inbox for good, even if it sees new activity. Use `rl session snooze` "
        "instead to park it until a date, and `rl overview --kept` to list kept sessions.",
    )
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--yes", "-y", action="store_true", help="skip confirmation prompts")
    args = ap.parse_args(argv)

    reg = registry.load_registry(args.registry)
    t = registry.find_session(reg, args.id)

    if t is None:
        ts = _archived_tombstone(reg, args.id)
        if ts is None:
            print(f"rl-keep: unknown session {args.id}", file=sys.stderr)
            return 1
        print(f"{args.id} is archived — keeping it means restoring it first.")
        if not args.yes and input("Restore and keep? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return 0
        try:
            dest, shown = unarchive.restore(reg, ts)
        except RuntimeError as e:
            print(f"rl-keep: {e}", file=sys.stderr)
            return 1
        registry.commit_registry(args.registry, reg)
        print(f"restored {args.id} -> {dest}")
        if not shown and (ts.get("thread") or {}).get("source") == "codex":
            print(
                "  note: restored on disk, but the agent's picker entry could not be "
                "updated (it may have been archived by the agent itself)",
                file=sys.stderr,
            )
        t = registry.find_session(reg, args.id)
        if t is None:
            registry.commit_registry(args.registry, reg)
            print(
                f"restored, but {args.id} has no registry record to keep — run `rl session refresh`"
            )
            return 1

    previous = registry.keep_thread(reg, args.id, _now())
    if previous and not args.yes:
        print(f"{args.id} is currently marked '{previous}'; keeping replaces that decision.")
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return 0
        reg = registry.load_registry(args.registry)
        registry.keep_thread(reg, args.id, _now())

    registry.commit_registry(args.registry, reg)
    extra = f" (replaced decision '{previous}')" if previous else ""
    print(f"keeping {args.id} long-term{extra} — `rl session unkeep {args.id}` to undo")
    return 0


def cmd_unkeep(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl session unkeep",
        description="Undo `rl session keep`: the session returns to the triage inbox.",
    )
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    args = ap.parse_args(argv)

    reg = registry.load_registry(args.registry)
    try:
        undone = registry.unkeep_thread(reg, args.id)
    except KeyError:
        print(f"rl-unkeep: unknown session {args.id}", file=sys.stderr)
        return 1
    if not undone:
        print(f"rl-unkeep: {args.id} is not kept", file=sys.stderr)
        return 1
    registry.commit_registry(args.registry, reg)
    print(f"unkept {args.id} — back in the triage inbox")
    return 0
