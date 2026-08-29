from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ringleader import harvests, registry
from ringleader.commands.harvests import cmd_harvests
from ringleader.publish_state import list_receipts


def _git(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _checkout(tmp_path: Path) -> tuple[Path, str, str]:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "-q")
    _git(checkout, "config", "user.email", "test@example.com")
    _git(checkout, "config", "user.name", "Test")
    (checkout / "README.md").write_text("before\n")
    _git(checkout, "add", "README.md")
    _git(checkout, "commit", "-qm", "initial")
    return checkout, _git(checkout, "symbolic-ref", "HEAD"), _git(checkout, "rev-parse", "HEAD")


def _write_items(items=("s1", "first", "s2", "second")):
    return [
        harvests.write_harvest(
            session_id=session_id,
            name=session_id,
            folder_path="/work/repo",
            repo_url="github.com/me/repo",
            harvested_at="2026-08-29T00:00:00+00:00",
            content=content,
        )
        for session_id, content in zip(items[::2], items[1::2], strict=True)
    ]


def _archive_receipt_sessions(receipt, registry_path: Path) -> None:
    reg = registry.load_registry(registry_path)
    for item in receipt["items"]:
        registry.archive_session(
            reg,
            item["sessionId"],
            archive_path=f"/archive/{item['sessionId']}",
            archived_at="2026-08-29T00:00:00+00:00",
        )
    registry.save_registry(registry_path, reg)


def _set_live_harvested(items, registry_path: Path) -> None:
    reg = registry.empty_registry()
    reg["threads"] = [
        {
            "sessionId": item.session_id,
            "decision": "harvested",
            "harvestPath": [str(item.path)],
        }
        for item in items
    ]
    registry.save_registry(registry_path, reg)


def test_changed_publish_recovery_consumes_only_after_the_exact_commit(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    checkout, branch, base = _checkout(tmp_path)
    paths = _write_items()
    items = {item.path: item for item in harvests.snapshot_harvests().items}
    selected = [items[path] for path in paths]
    registry_path = tmp_path / "registry.json"
    _set_live_harvested(selected, registry_path)
    bundle = tmp_path / "private.md"

    assert (
        cmd_harvests(
            [
                "begin",
                "--repo-key",
                "github.com/me/repo",
                "--checkout",
                str(checkout),
                "--branch",
                branch,
                "--base",
                base,
                "--bundle",
                str(bundle),
                "--registry",
                str(registry_path),
                "--item",
                selected[0].token,
                "--item",
                selected[1].token,
            ]
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)["receipt"]
    assert bundle.read_text().count("<!-- BEGIN HARVEST -->") == 2

    (checkout / "README.md").write_text("after\n")
    paths_file = tmp_path / "paths.nul"
    paths_file.write_bytes(b"README.md\0")
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "--literal-pathspecs",
            "add",
            "-A",
            "--pathspec-from-file=" + str(paths_file),
            "--pathspec-file-nul",
        ],
        check=True,
    )
    report = tmp_path / "report.txt"
    report.write_text("README.md\n")
    tree = _git(checkout, "write-tree")
    assert (
        cmd_harvests(
            [
                "prepare",
                receipt["receiptId"],
                "--outcome",
                "changed",
                "--tree",
                tree,
                "--paths-file",
                str(report),
            ]
        )
        == 0
    )
    capsys.readouterr()
    _git(checkout, "commit", "-qm", "chore: update docs")
    commit = _git(checkout, "rev-parse", "HEAD")

    assert cmd_harvests(["recover", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["receipts"][0]["receiptId"] == receipt["receiptId"]
    _archive_receipt_sessions(receipt, registry_path)

    assert cmd_harvests(["consume", receipt["receiptId"], "--commit", commit]) == 0
    assert json.loads(capsys.readouterr().out)["consumed"] == 2
    assert _git(checkout, "rev-list", "--count", "HEAD") == "2"
    assert _git(checkout, "status", "--porcelain") == ""
    assert _git(checkout, "remote") == ""
    assert harvests.snapshot_harvests().items == ()
    assert list_receipts()[0] == ()


def test_no_change_publish_consumes_without_creating_a_commit(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    checkout, branch, base = _checkout(tmp_path)
    path = _write_items(("s1", "first"))[0]
    selected = next(item for item in harvests.snapshot_harvests().items if item.path == path)
    registry_path = tmp_path / "registry.json"
    _set_live_harvested([selected], registry_path)

    assert (
        cmd_harvests(
            [
                "begin",
                "--repo-key",
                "github.com/me/repo",
                "--checkout",
                str(checkout),
                "--branch",
                branch,
                "--base",
                base,
                "--bundle",
                str(tmp_path / "private.md"),
                "--registry",
                str(registry_path),
                "--item",
                selected.token,
            ]
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)["receipt"]
    assert cmd_harvests(["prepare", receipt["receiptId"], "--outcome", "no-change"]) == 0
    capsys.readouterr()
    _archive_receipt_sessions(receipt, registry_path)

    assert cmd_harvests(["consume", receipt["receiptId"]]) == 0
    assert _git(checkout, "rev-list", "--count", "HEAD") == "1"
    assert harvests.snapshot_harvests().items == ()
    assert list_receipts()[0] == ()
