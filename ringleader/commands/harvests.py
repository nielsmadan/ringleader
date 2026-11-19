from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from .. import harvests as store
from ..memory_types import MemoryOrigin, origin_fields
from ..publish_git import (
    PublishGitError,
    can_abandon,
    read_report_paths,
    require_clean_base,
    validate_prepared_changes,
)
from ..publish_state import (
    PublishStateError,
    abandon_receipt,
    assign_memory_harvest,
    begin_publish,
    consume_publish,
    list_receipts,
    load_receipt,
    prepare_publish,
    receipt_data,
    reconcile_memory_queue_unlocked,
)
from ..stateio import publish_lock
from . import default_registry


def _write_json(data: object) -> int:
    json.dump(data, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _cmd_snapshot(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl harvests snapshot")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--registry", type=Path, default=default_registry())
    args = ap.parse_args(argv)
    receipts, _ = list_receipts()
    claimed_by = {item.token: receipt.receipt_id for receipt in receipts for item in receipt.items}
    claimed_ids = {
        item.harvest_id: receipt.receipt_id for receipt in receipts for item in receipt.items
    }
    snapshot = store.pending_snapshot(args.registry)
    return _write_json(
        {
            "items": [
                {
                    **origin_fields(item.origin),
                    "name": item.name,
                    **item.destination_fields(),
                    "harvestedAt": item.harvested_at,
                    "path": str(item.path),
                    "harvestId": item.harvest_id,
                    "digest": item.digest,
                    "token": item.token,
                    **(
                        {"approvedBodyDigest": item.body_digest}
                        if isinstance(item.origin, MemoryOrigin)
                        else {}
                    ),
                    "contentBytes": item.content_bytes,
                    "repoKey": item.repo_key,
                    "claimedBy": (
                        claimed_ids.get(item.harvest_id)
                        if isinstance(item.origin, MemoryOrigin)
                        else claimed_by.get(item.token)
                    ),
                }
                for item in snapshot.items
            ],
            "problems": [
                {"path": str(problem.path), "message": problem.message}
                for problem in snapshot.problems
            ],
        }
    )


def _cmd_assign(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl harvests assign")
    ap.add_argument("token")
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--registry", type=Path, default=default_registry())
    args = ap.parse_args(argv)
    item = assign_memory_harvest(args.token, project=args.project, registry_path=args.registry)
    return _write_json(
        {"harvestId": item.harvest_id, "token": item.token, "destinationPath": item.folder_path}
    )


def _cmd_begin(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl harvests begin")
    ap.add_argument("--repo-key", required=True)
    ap.add_argument("--checkout", required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--registry", required=True)
    ap.add_argument("--item", action="append", required=True)
    args = ap.parse_args(argv)
    require_clean_base(args.checkout, branch=args.branch, head=args.base)
    receipt = begin_publish(
        repo_key=args.repo_key,
        checkout=args.checkout,
        branch=args.branch,
        base_commit=args.base,
        registry_path=args.registry,
        tokens=args.item,
        bundle_path=Path(args.bundle),
    )
    return _write_json({"receipt": receipt_data(receipt)})


def _cmd_prepare(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl harvests prepare")
    ap.add_argument("receipt_id")
    ap.add_argument("--outcome", choices=("changed", "no-change"), required=True)
    ap.add_argument("--tree")
    ap.add_argument("--paths-file")
    args = ap.parse_args(argv)
    receipt = load_receipt(args.receipt_id)
    if args.outcome == "changed":
        if args.tree is None or args.paths_file is None:
            raise PublishStateError("changed outcome requires --tree and --paths-file")
        paths = read_report_paths(Path(args.paths_file))
        validate_prepared_changes(receipt, paths, args.tree)
        prepared = prepare_publish(
            receipt.receipt_id,
            outcome="changed",
            expected_tree=args.tree,
            changed_paths=paths,
        )
    else:
        if args.tree is not None or args.paths_file is not None:
            raise PublishStateError("no-change outcome does not accept --tree or --paths-file")
        require_clean_base(receipt.checkout, branch=receipt.branch, head=receipt.base_commit)
        prepared = prepare_publish(receipt.receipt_id, outcome="no-change")
    return _write_json({"receipt": receipt_data(prepared)})


def _cmd_consume(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl harvests consume")
    ap.add_argument("receipt_id")
    ap.add_argument("--commit")
    args = ap.parse_args(argv)
    result = consume_publish(args.receipt_id, commit=args.commit)
    return _write_json(
        {
            "receiptId": result.receipt_id,
            "consumed": result.consumed,
            "sessionIds": list(result.session_ids),
            **({"memoryIds": list(result.memory_ids)} if result.memory_ids else {}),
        }
    )


def _cmd_abandon(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl harvests abandon")
    ap.add_argument("receipt_id")
    args = ap.parse_args(argv)
    receipt = load_receipt(args.receipt_id)
    if not can_abandon(receipt):
        raise PublishStateError("receipt cannot be safely abandoned")
    abandon_receipt(receipt.receipt_id)
    return _write_json({"receiptId": receipt.receipt_id, "abandoned": True})


def _cmd_recover(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl harvests recover")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--registry", type=Path, default=default_registry())
    args = ap.parse_args(argv)
    with publish_lock():
        receipts, problems = list_receipts()
        if not problems:
            snapshot = store.snapshot_harvests()
            for receipt in receipts:
                for item in receipt.items:
                    if item.harvest_id in snapshot.ambiguous_ids:
                        raise PublishStateError(
                            f"duplicate harvest ID in receipt: {item.harvest_id}"
                        )
            reconcile_memory_queue_unlocked(args.registry, snapshot=snapshot)
    return _write_json(
        {
            "receipts": [receipt_data(receipt) for receipt in receipts],
            "problems": [
                {"path": str(problem.path), "message": problem.message} for problem in problems
            ],
        }
    )


_ACTIONS: dict[str, Callable[[list[str]], int]] = {
    "snapshot": _cmd_snapshot,
    "assign": _cmd_assign,
    "begin": _cmd_begin,
    "prepare": _cmd_prepare,
    "consume": _cmd_consume,
    "abandon": _cmd_abandon,
    "recover": _cmd_recover,
}
SUBCOMMANDS: tuple[str, ...] = tuple(_ACTIONS)


def _cmd_list(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl harvests")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--registry", type=Path, default=default_registry())
    args = ap.parse_args(argv)
    rows = store.pending_snapshot(args.registry).items
    if args.json:
        json.dump(
            [
                {
                    **origin_fields(h.origin),
                    "name": h.name,
                    **h.destination_fields(include_clone=False),
                    "harvestedAt": h.harvested_at,
                    "path": str(h.path),
                    "content": h.content,
                }
                for h in rows
            ],
            sys.stdout,
            indent=2,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
        return 0
    if not rows:
        print("no harvests yet")
        return 0
    for h in rows:
        preview = " ".join(h.content.split())[:80]
        print(f"{h.name or h.origin_id}  [{h.repo_url or h.folder_path or '?'}]")
        print(f"  {h.folder_path or ''}  — {preview}")
    print(f"\n{len(rows)} harvest(s)")
    return 0


def cmd_harvests(argv: list[str]) -> int:
    if argv and argv[0] in _ACTIONS:
        try:
            return _ACTIONS[argv[0]](argv[1:])
        except BrokenPipeError:
            raise
        except (OSError, PublishStateError, PublishGitError) as error:
            print(f"rl-harvests: {error}", file=sys.stderr)
            return 1
    return _cmd_list(argv)
