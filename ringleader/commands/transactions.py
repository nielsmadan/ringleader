from __future__ import annotations

import json
from dataclasses import asdict

from .conversion import parser, render_error, render_result, service


def cmd_list(argv: list[str]) -> int:
    ap = parser(
        "data transaction list",
        "Read conversion journals without creating state or reading transcript bodies.",
    )
    args = ap.parse_args(argv)
    try:
        operations = service(args).transactions()
        if args.json:
            print(json.dumps([asdict(operation) for operation in operations], indent=2))
        else:
            for operation in operations:
                print(
                    f"{operation.id}: {operation.state}; {operation.source.agent}:{operation.source.native_id} → {operation.target.agent}:{operation.target.native_id}"
                )
                if operation.last_error:
                    print(f"  {operation.last_error}")
            if not operations:
                print("No conversion transactions.")
        return 0
    except (OSError, ValueError, RuntimeError, TypeError, LookupError) as exc:
        return render_error(exc, json_output=args.json)


def _recover(argv: list[str], *, rollback: bool) -> int:
    verb = "rollback" if rollback else "resume"
    ap = parser(
        f"data transaction {verb}",
        "Rollback removes private staging only; published targets require forward resume.",
    )
    ap.add_argument("id", help="exact conversion transaction ID")
    args = ap.parse_args(argv)
    try:
        worker = service(args)
        if rollback:
            operation = worker.rollback(args.id)
            print(
                json.dumps(asdict(operation), indent=2)
                if args.json
                else f"{operation.id}: {operation.state}"
            )
            if operation.last_error and not args.json:
                print(operation.last_error)
        else:
            result = worker.resume(args.id)
            print(json.dumps(asdict(result), indent=2) if args.json else render_result(result))
        return 0
    except (OSError, ValueError, RuntimeError, TypeError, LookupError) as exc:
        return render_error(exc, json_output=args.json)


def cmd_resume(argv: list[str]) -> int:
    return _recover(argv, rollback=False)


def cmd_rollback(argv: list[str]) -> int:
    return _recover(argv, rollback=True)
