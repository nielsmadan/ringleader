from __future__ import annotations

import argparse
import shlex
import sys
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .. import memory_catalog, memory_scan, registry
from ..config import load_config
from ..memory_sources.transactions import MAX_FILE
from ..memory_types import MemoryRecord, MemoryRef
from .mem_output import MemoryParser, terminal_text
from .mem_scan import detail_text, entry_metadata

if TYPE_CHECKING:
    from ..memory_lifecycle import MemoryOutcome, RemovalReview

type Action = Literal["harvest", "complete", "delete", "mark harvested"]

_DESCRIPTIONS: dict[Action, str] = {
    "harvest": "Save approved knowledge to the shared harvest queue.",
    "complete": "Remove native memory after its approved knowledge is durably saved.",
    "delete": "Discard one reviewed memory entry with configurable recovery.",
    "mark harvested": "Record exact approved Markdown and existing direct-integration destinations.",
}

_DISPOSITIONS = {
    "harvest": "save-to-queue",
    "complete": "remove-with-saved-knowledge",
    "delete": "discard",
    "mark harvested": "record-direct-integration",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _read_markdown(path: str) -> str:
    if path == "-":
        buffer = getattr(sys.stdin, "buffer", None)
        raw: bytes = (
            sys.stdin.read(MAX_FILE + 1).encode("utf-8")
            if buffer is None
            else buffer.read(MAX_FILE + 1)
        )
    else:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise ValueError("Approved Markdown exceeds the recovery size limit")
    markdown = raw.decode("utf-8")
    if not markdown.strip():
        raise ValueError("Approved Markdown must be nonempty")
    return markdown


def _generate_markdown(observed: MemoryRecord) -> str:
    from .. import harvesting  # noqa: PLC0415 -- only model-based drafting needs these modules
    from ..models import ModelError, active_model  # noqa: PLC0415

    model = active_model(load_config(), "harvest")
    if model is None:
        raise ValueError("No harvest model is available. Use `rl mem detail` and `--from-file`.")
    try:
        with redirect_stdout(sys.stderr):
            markdown = harvesting.memory_harvest_markdown(observed, model)
    except ModelError as exc:
        raise ValueError(f"The harvest model failed: {exc}") from exc
    if markdown is None:
        raise ValueError("The harvest model returned no approved Markdown")
    return markdown


def _guarded_ref(args: argparse.Namespace, row: dict[str, Any]) -> MemoryRef:
    if not row.get("fingerprint") or not row.get("incarnation") or not row.get("nativeIncarnation"):
        raise ValueError("Run `rl mem refresh` to review this entry")
    ref = memory_catalog.reviewed_ref(row)
    if (args.if_fingerprint is not None and args.if_fingerprint != ref.fingerprint) or (
        args.if_incarnation is not None and args.if_incarnation != ref.incarnation
    ):
        raise ValueError("Memory review is stale. Run `rl mem refresh` and review this revision.")
    if row.get("mutationBlock"):
        raise ValueError(row["mutationBlock"])
    return ref


def _removed_ref(args: argparse.Namespace, reg: dict[str, Any]) -> MemoryRef | None:
    if not args.id:
        return None
    matches = [
        memory_catalog.reviewed_ref(row)
        for row in reg.get("memoryDecisions", {}).values()
        if row.get("sourceState") in {"removed", "restored"}
        and row.get("fingerprint") == args.if_fingerprint
        and row.get("incarnation") == args.if_incarnation
        and (
            row.get("memoryId", "").startswith(args.id)
            or (row.get("source") == "claude-code" and row.get("sourceRef") == args.id)
        )
    ]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous memory operation for {args.id}")
    return next(iter(matches), None)


def _confirm(action: Action, preview: str, args: argparse.Namespace) -> bool:
    question = {
        "harvest": "Save this Markdown to the harvest queue?",
        "complete": "Remove this native memory after checking the saved knowledge?",
        "delete": "Discard this native memory?",
        "mark harvested": "Record that this Markdown has been integrated at the listed paths?",
    }[action]
    if action == "mark harvested":
        preview += "\n\nDestinations\n" + "\n".join(str(path) for path in args.paths)
    print(terminal_text(f"{preview}\n\n{question} [y/N] "), file=sys.stderr)
    try:
        return input().strip().casefold() in {"y", "yes"}
    except EOFError:
        return False


def _review_removal(
    ref: MemoryRef, args: argparse.Namespace, *, complete: bool, markdown: str | None
) -> tuple[RemovalReview, str]:
    from ..memory_lifecycle import MemoryLifecycle  # noqa: PLC0415

    review = (
        MemoryLifecycle()
        .review_removal(
            ref,
            args.registry,
            "harvest" if complete else "discard",
            load_config().memory_recovery_retention_days,
        )
        .approve(markdown)
    )
    retention = review.retention
    details = []
    expected = getattr(args, "if_harvest_token", None)
    if expected is not None and (not review.queue or expected != review.queue[1]):
        raise ValueError("Pending harvest token changed")
    if review.queue:
        saved, token = review.queue
        details.append(f"Saved Markdown (token {token})\n\n{saved}")
    elif review.destinations:
        details.append(
            "Verified destinations\n" + "\n".join(path for path, _ in review.destinations)
        )
    elif review.receipt_id:
        details.append(
            f"Published in receipt {review.receipt_id} (approved digest {review.approval_hash})"
        )
    details.append(
        f"Recovery for {retention} days." if retention else "No recovery copy after removal."
    )
    return review, "\n\n".join(details)


def _apply(
    action: Action,
    ref: MemoryRef,
    args: argparse.Namespace,
    markdown: str | None,
    review: RemovalReview | None = None,
) -> MemoryOutcome:
    from .. import memory_lifecycle as lifecycle  # noqa: PLC0415

    path = args.registry
    now = _now()
    if action in ("delete", "complete"):
        days = load_config().memory_recovery_retention_days
        if action == "delete":
            return lifecycle.discard_memory(
                ref,
                registry_path=path,
                now=now,
                retention_days=days,
                review=review,
            )
        queue = review.queue if review else None
        return lifecycle.complete_memory(
            ref,
            markdown=markdown,
            expected_harvest_token=queue[1] if queue else args.if_harvest_token,
            registry_path=path,
            now=now,
            retention_days=days,
            review=review,
        )
    if markdown is None:
        raise ValueError("Supply approved Markdown")
    if action == "mark harvested":
        return lifecycle.mark_memory_integrated(
            ref, markdown=markdown, paths=args.paths, registry_path=path, now=now
        )
    return lifecycle.save_memory_harvest(
        ref,
        markdown=markdown,
        expected_harvest_token=args.if_harvest_token,
        registry_path=path,
        now=now,
    )


def _retry_command(verb: str, operation_id: str, path: Path) -> str:
    return shlex.join(["rl", "mem", verb, operation_id, "--registry", str(path.absolute())])


def _outcome(out: MemoryOutcome, path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "operationId": out.operation_id,
        "memoryId": out.memory_id or None,
        "state": out.state,
        "cleanup": out.cleanup,
        "harvestPath": out.harvest_path,
        "message": out.message,
    }
    if out.pending_action:
        verb = "restore" if out.pending_action == "restore" else "recover"
        result.update(pendingAction=out.pending_action, retryVerb=verb)
        if out.pending_action == "restore" and out.operation_id:
            result["message"] = (
                f"{out.message.rstrip('.')}. Retry with `{_retry_command(verb, out.operation_id, path)}`."
            )
    return result


def _outcome_line(result: dict[str, Any]) -> str:
    op = f" ({result['operationId']})" if result["operationId"] else ""
    harvest = f" -> {result['harvestPath']}" if result["harvestPath"] else ""
    cleanup = f" | consolidation {result['cleanup']}" if result["cleanup"] != "not-needed" else ""
    state = "restore deferred" if result.get("pendingAction") == "restore" else result["state"]
    return f"{state}{op}{harvest}{cleanup}: {result['message']}"


def _emit_outcome(parser: MemoryParser, out: MemoryOutcome, path: Path) -> int:
    data = _outcome(out, path)
    if out.state in {"failed", "refused"}:
        return parser.fail(out.message, outcome=data)
    parser.emit({"outcome": data}, _outcome_line(data))
    return 0


def _decision_text(record: dict[str, Any], markdown: str | None) -> str:
    text = detail_text(record)
    return text if markdown is None else f"{text}\n\nApproved Markdown\n\n{markdown}"


def _preview(
    parser: MemoryParser,
    args: argparse.Namespace,
    action: Action,
    record: dict[str, Any],
    markdown: str | None,
) -> int:
    destinations: tuple[str, ...] = ()
    disposition = _DISPOSITIONS[action]
    if action == "mark harvested" or (action == "complete" and markdown is None):
        from ..memory_lifecycle import MemoryLifecycle  # noqa: PLC0415

        destinations = MemoryLifecycle().preview_evidence(
            memory_catalog.reviewed_ref(record),
            args.registry,
            direct=args.paths if action == "mark harvested" else None,
            expected_harvest_token=getattr(args, "if_harvest_token", None),
        )
    elif action == "complete":
        disposition = "save-to-queue-and-remove"
    if action == "harvest" and markdown is not None and not parser.as_json:
        print(terminal_text(markdown) if sys.stdout.isatty() else markdown, end="")
    else:
        text = f"{_decision_text(record, markdown)}\n\nDisposition: {disposition}"
        if destinations:
            text += "\nDestinations\n" + "\n".join(destinations)
        parser.emit(
            {
                "dryRun": True,
                "record": record,
                "markdown": markdown,
                "disposition": disposition,
                "destinations": list(destinations),
            },
            text,
        )
    return 0


def _decide(
    parser: MemoryParser, args: argparse.Namespace, action: Action, *, interactive: bool
) -> int:
    reg = registry.load_registry(args.registry)
    try:
        row = memory_catalog.select_entry(reg, args.id)
    except memory_catalog.UnknownMemoryEntry:
        if not (interactive or args.dry_run) and action in ("complete", "delete"):
            ref = _removed_ref(args, reg)
            if ref is not None:
                markdown = _input_markdown(args)
                return _emit_outcome(parser, _apply(action, ref, args, markdown), args.registry)
        raise
    ref = _guarded_ref(args, row)
    markdown = _input_markdown(args)
    review = None
    generate = action == "harvest" and markdown is None
    if args.dry_run or interactive or generate:
        observed = memory_scan.read_record(reg, row)
        if observed.mutation_block:
            raise ValueError(observed.mutation_block)
        if generate:
            markdown = _generate_markdown(observed)
        if args.dry_run or interactive:
            record = entry_metadata(row, reg)
            record.update(content=observed.content, mutationBlock=observed.mutation_block)
            if args.dry_run:
                return _preview(parser, args, action, record, markdown)
            preview = _decision_text(record, markdown)
            if action in ("delete", "complete"):
                review, description = _review_removal(
                    ref, args, complete=action == "complete", markdown=markdown
                )
                preview += "\n\n" + description
            if not _confirm(action, preview, args):
                parser.emit({"cancelled": True}, "Cancelled")
                return 0
    return _emit_outcome(parser, _apply(action, ref, args, markdown, review), args.registry)


def _input_markdown(args: argparse.Namespace) -> str | None:
    path = getattr(args, "from_file", None)
    return _read_markdown(path) if path is not None else None


def _decision_command(action: Action, argv: list[str]) -> int:
    parser = MemoryParser(action, argv, _DESCRIPTIONS[action])
    parser.add_argument("id", help="unique memory ID prefix or legacy Claude memory file path")
    parser.add_argument("--dry-run", action="store_true", help="preview without changing state")
    if action != "delete":
        parser.add_argument(
            "--from-file",
            required=action == "mark harvested",
            help="approved UTF-8 Markdown file or '-' for stdin",
        )
    if action in ("harvest", "complete"):
        parser.add_argument(
            "--if-harvest-token", help="reviewed queue token for replacement or queued removal"
        )
    if action == "mark harvested":
        parser.add_argument(
            "--paths", type=Path, nargs="+", required=True, help="existing integration files"
        )
    guard_help = " (required for noninteractive removal and direct integration)"
    parser.add_argument("--if-fingerprint", help="fingerprint from mem detail" + guard_help)
    parser.add_argument("--if-incarnation", help="review incarnation from mem detail" + guard_help)
    args = parser.parse_args(argv)
    interactive = not parser.as_json and sys.stdin.isatty() and sys.stdout.isatty()
    needs_guard = action in ("complete", "delete", "mark harvested") and not interactive
    if needs_guard and not (args.if_fingerprint and args.if_incarnation):
        return parser.fail(
            "Supply both --if-fingerprint and --if-incarnation from mem detail", code=2
        )
    return parser.run(lambda: _decide(parser, args, action, interactive=interactive))


def cmd_harvest(argv: list[str]) -> int:
    return _decision_command("harvest", argv)


def cmd_complete(argv: list[str]) -> int:
    return _decision_command("complete", argv)


def cmd_delete(argv: list[str]) -> int:
    return _decision_command("delete", argv)


def cmd_mark_harvested(argv: list[str]) -> int:
    return _decision_command("mark harvested", argv)


def cmd_restore(argv: list[str]) -> int:
    from ..memory_lifecycle import restore_memory  # noqa: PLC0415 -- mutation commands only

    parser = MemoryParser("restore", argv, "Restore the selected native entry from recovery.")
    parser.add_argument("operation_id")
    args = parser.parse_args(argv)
    return parser.run(
        lambda: _emit_outcome(
            parser,
            restore_memory(args.operation_id, registry_path=args.registry, now=_now()),
            args.registry,
        )
    )


def _recover(parser: MemoryParser, args: argparse.Namespace) -> int:
    from ..memory_lifecycle import recover_memory  # noqa: PLC0415 -- mutation commands only
    from ..memory_operations import cleanup_checkpoints, recovery_targets  # noqa: PLC0415

    now = _now()
    if args.operation_id is not None:
        return _emit_outcome(
            parser,
            recover_memory(args.operation_id, registry_path=args.registry, now=now),
            args.registry,
        )
    cleanup_checkpoints()
    outcomes = [
        _outcome(recover_memory(op, registry_path=args.registry, now=now), args.registry)
        for op in recovery_targets(args.registry)
    ]
    message = "\n".join(
        _outcome_line(out) for out in outcomes if out["state"] not in {"refused", "failed"}
    )
    parser.emit(
        {"outcomes": outcomes},
        message or ("No memory operations to recover" if not outcomes else ""),
    )
    failed = [out for out in outcomes if out["state"] in {"refused", "failed"}]
    for out in failed:
        print(terminal_text(f"{parser.prog}: {_outcome_line(out)}"), file=sys.stderr)
    return int(bool(failed))


def cmd_recover(argv: list[str]) -> int:
    parser = MemoryParser(
        "recover", argv, "Retry approved native operations and check consolidation."
    )
    parser.add_argument("operation_id", nargs="?")
    args = parser.parse_args(argv)
    return parser.run(lambda: _recover(parser, args))


def _show_recovery(parser: MemoryParser, path: Path) -> int:
    from ..memory_operations import recovery_listing  # noqa: PLC0415 -- recovery metadata only

    listing = recovery_listing(path, _now())
    records = [dict(row, retry=row["operationId"] in listing.retry) for row in listing.rows]
    lines = [
        f"{row['operationId']}  {row['source']}  {row['name']}  {row['sourceState']}  "
        f"recovery {'until ' + row['recoveryExpiresAt'] if row['restorable'] else 'unavailable'}  "
        f"consolidation {row['cleanup']}"
        + (f"  superseded by {row['supersededBy']}" if row["supersededBy"] else "")
        + (
            f"  retry with {_retry_command('recover', row['operationId'], path)}"
            if row["retry"]
            else ""
        )
        + (f"  {row['diagnostic']}" if row.get("diagnostic") else "")
        for row in records
    ]
    parser.emit({"records": records}, "\n".join(lines) or "No memory removals or recovery pending")
    return 0


def _prune_recovery(parser: MemoryParser, args: argparse.Namespace) -> int:
    from ..memory_lifecycle import prune_memory_recovery  # noqa: PLC0415 -- recovery commands only

    eligible = prune_memory_recovery(registry_path=args.registry, now=_now(), delete=args.delete)
    lines = [
        f"{'Purged' if args.delete else 'Would purge'} recovery payload for {op}" for op in eligible
    ]
    if not args.delete and eligible:
        lines.append("Run `rl mem recovery prune --delete` to apply")
    parser.emit(
        {"deleted": args.delete, "operationIds": list(eligible)},
        "\n".join(lines) or "No expired recovery payloads",
    )
    return 0


def _cmd_recovery_prune(argv: list[str]) -> int:
    parser = MemoryParser("recovery prune", argv, "Preview or purge expired recovery payloads.")
    parser.add_argument("--delete", action="store_true", help="purge the expired payloads")
    args = parser.parse_args(argv)
    return parser.run(lambda: _prune_recovery(parser, args))


_RECOVERY_ACTIONS = {"prune": _cmd_recovery_prune}
RECOVERY_SUBCOMMANDS = tuple(_RECOVERY_ACTIONS)


def cmd_recovery(argv: list[str]) -> int:
    if argv and (sub := _RECOVERY_ACTIONS.get(argv[0])):
        return sub(argv[1:])
    parser = MemoryParser(
        "recovery", argv, "Show native removals, retention and restoration status."
    )
    parser.epilog = "Use `rl mem recovery prune` to preview expired payload cleanup."
    args = parser.parse_args(argv)
    return parser.run(lambda: _show_recovery(parser, args.registry))
