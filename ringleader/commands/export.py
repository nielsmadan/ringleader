from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import export, registry
from . import default_registry


def _json_tails(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    from .. import contract, contract_io  # noqa: PLC0415 -- keeps plain export off the contract stack

    if args.out is not None:
        ap.error("--out cannot be combined with --json")
    max_chars = contract.TAIL_DEFAULT_CHARS if args.max_chars is None else args.max_chars
    if not contract.TAIL_MIN_CHARS <= max_chars <= contract.TAIL_MAX_CHARS:
        ap.error(
            f"--max-chars must be between {contract.TAIL_MIN_CHARS} and {contract.TAIL_MAX_CHARS}"
        )
    if len(args.ids) > contract.TAIL_MAX_IDS:
        ap.error(f"at most {contract.TAIL_MAX_IDS} ids per call")
    if len(set(args.ids)) != len(args.ids):
        ap.error("each id may appear only once")
    return contract_io.run_json(lambda: contract_io.tails_doc(args.registry, args.ids, max_chars))


def cmd_export(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl data export")
    ap.add_argument("ids", nargs="+", metavar="ID")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--json",
        action="store_true",
        help="print the end of each transcript as one JSON document (up to 16 IDs)",
    )
    ap.add_argument(
        "--max-chars",
        type=int,
        default=None,
        help="with --json, the most characters kept from each transcript (default 200000)",
    )
    args = ap.parse_args(argv)
    if args.json:
        return _json_tails(ap, args)
    if len(args.ids) > 1:
        ap.error("more than one ID needs --json")
    if args.max_chars is not None:
        ap.error("--max-chars needs --json")
    session_id = args.ids[0]

    reg = registry.load_registry(args.registry)
    t = registry.find_session(reg, session_id)
    if t is None:
        print(f"rl-export: unknown session {session_id}", file=sys.stderr)
        return 1

    try:
        text = export.render_conversation(t)
    except (OSError, RuntimeError) as error:
        print(f"rl-export: {error}", file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0
