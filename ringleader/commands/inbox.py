from __future__ import annotations

import argparse
import shutil

from ..launch import exec_agent
from . import default_registry


def cmd_inbox(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl inbox",
        description="The daily home for dormant sessions: what's waiting, the next step, "
        "triage, resume, roundup, prune, harvests and usage. ctrl+p for more.",
    )
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument(
        "--read-only",
        action="store_true",
        help="browse without writing the registry; launches become a dry run",
    )
    ap.add_argument(
        "--json", action="store_true", help="print the inbox as one JSON document and exit"
    )
    ap.add_argument(
        "--scan", action="store_true", help="with --json, rescan the sources before listing"
    )
    args = ap.parse_args(argv)
    if args.scan and not args.json:
        ap.error("--scan needs --json")
    if args.json and args.read_only:
        ap.error("--json cannot be combined with --read-only")
    if args.json:
        from .. import contract_io  # noqa: PLC0415 -- keeps the TUI path off the contract stack

        return contract_io.run_json(lambda: contract_io.list_doc(args.registry, scan=args.scan))

    try:
        import textual  # noqa: F401, PLC0415 -- core dep; imported lazily to keep other commands fast
    except ImportError:
        print(
            "rl inbox needs Textual — reinstall ringleader (`uv tool install --force ringleader`)"
        )
        return 2

    from .. import commands  # noqa: PLC0415 -- the package imports this module
    from ..home import launch  # noqa: PLC0415 -- only importable once textual is present
    from ..home.app import InboxApp  # noqa: PLC0415 -- only importable once textual is present
    from ..models import proc  # noqa: PLC0415 -- keeps rl inbox --json light

    app = InboxApp(args.registry, read_only=args.read_only)
    try:
        intent = app.run()
    finally:
        app.abort_background_work()
        app.writer.join_current()
        proc.kill_all_children()
    if app.return_code:
        return app.return_code
    if intent is None:
        return 0
    return launch.run_intent(
        intent,
        args.registry,
        dry_run=args.read_only,
        resume_command=commands.resume.cmd_resume,
        runner=exec_agent,
        which=shutil.which,
    )
