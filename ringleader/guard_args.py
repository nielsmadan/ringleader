from __future__ import annotations

import argparse


def add_guard_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument(
        "--json",
        action="store_true",
        help="print the outcome as one JSON document (needs --if-mtime)",
    )
    ap.add_argument(
        "--if-mtime",
        default=None,
        metavar="MTIME",
        help=(
            "the mtime the caller saw; archive, delete and harvest act only if the session still"
            " has it (compared as an exact string) and is not running; snooze, keep and mark reviewed take it as the stamp only"
            " (mark reviewed records it)"
        ),
    )
    ap.add_argument(
        "--if-fingerprint",
        default=None,
        metavar="FP",
        help=(
            "with --if-mtime, act only if the transcript still has this content fingerprint"
            " (archive, delete and harvest; accepted and ignored by snooze, keep and mark reviewed)"
        ),
    )


def guarded_requested(args: argparse.Namespace) -> bool:
    return bool(args.json) or args.if_mtime is not None or args.if_fingerprint is not None
