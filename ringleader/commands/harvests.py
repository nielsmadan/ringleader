from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from .. import harvests as store
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
    begin_publish,
    consume_publish,
    list_receipts,
    load_receipt,
    prepare_publish,
)


def _write_json(data: object) -> int:
    json.dump(data, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _receipt_json(receipt) -> dict[str, object]:
    return {
        "receiptId": receipt.receipt_id,
        "repoKey": receipt.repo_key,
        "checkout": receipt.checkout,
        "branch": receipt.branch,
        "baseCommit": receipt.base_commit,
        "registryPath": receipt.registry_path,
        "items": [
            {
                "harvestId": item.harvest_id,
                "digest": item.digest,
                "token": item.token,
                "sessionId": item.session_id,
                "relativePath": item.relative_path,
            }
            for item in receipt.items
        ],
        "state": receipt.state,
        "outcome": receipt.outcome,
        "expectedTree": receipt.expected_tree,
        "changedPaths": list(receipt.changed_paths),
        "commit": receipt.commit,
        "createdAt": receipt.created_at,
    }


def _cmd_snapshot(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl harvests snapshot")
    ap.add_argument("--json", action="store_true")
    ap.parse_args(argv)
    receipts, _ = list_receipts()
    claimed_by = {item.token: receipt.receipt_id for receipt in receipts for item in receipt.items}
    snapshot = store.snapshot_harvests()
    return _write_json(
        {
            "items": [
                {
                    "sessionId": item.session_id,
                    "name": item.name,
                    "folderPath": item.folder_path,
                    "repoUrl": item.repo_url,
                    "cloneUrl": item.clone_url,
                    "harvestedAt": item.harvested_at,
                    "path": str(item.path),
                    "harvestId": item.harvest_id,
                    "digest": item.digest,
                    "token": item.token,
                    "contentBytes": item.content_bytes,
                    "repoKey": item.repo_key,
                    "claimedBy": claimed_by.get(item.token),
                }
                for item in snapshot.items
            ],
            "problems": [
                {"path": str(problem.path), "message": problem.message}
                for problem in snapshot.problems
            ],
        }
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
    return _write_json({"receipt": _receipt_json(receipt)})


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
    return _write_json({"receipt": _receipt_json(prepared)})


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
    ap.parse_args(argv)
    receipts, problems = list_receipts()
    return _write_json(
        {
            "receipts": [_receipt_json(receipt) for receipt in receipts],
            "problems": [
                {"path": str(problem.path), "message": problem.message} for problem in problems
            ],
        }
    )


_ACTIONS: dict[str, Callable[[list[str]], int]] = {
    "snapshot": _cmd_snapshot,
    "begin": _cmd_begin,
    "prepare": _cmd_prepare,
    "consume": _cmd_consume,
    "abandon": _cmd_abandon,
    "recover": _cmd_recover,
}


def _cmd_list(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl harvests")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rows = store.load_harvests()
    if args.json:
        json.dump(
            [
                {
                    "sessionId": h.session_id,
                    "name": h.name,
                    "folderPath": h.folder_path,
                    "repoUrl": h.repo_url,
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
        print(f"{h.name or h.session_id}  [{h.repo_url or h.folder_path or '?'}]")
        print(f"  {h.folder_path or ''}  — {preview}")
    print(f"\n{len(rows)} harvest(s)")
    return 0


def cmd_harvests(argv: list[str]) -> int:
    if argv and argv[0] in _ACTIONS:
        try:
            return _ACTIONS[argv[0]](argv[1:])
        except (PublishStateError, PublishGitError) as error:
            print(f"rl-harvests: {error}", file=sys.stderr)
            return 1
    return _cmd_list(argv)
