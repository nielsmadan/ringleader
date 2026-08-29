from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .publish_state import PublishReceipt


@dataclass(frozen=True)
class CheckoutState:
    root: str
    branch: str
    head: str
    status: bytes
    upstream: str | None
    push_remote: str | None


class PublishGitError(RuntimeError):
    pass


_OID = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")
_OPERATIONS = ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD")
_DISALLOWED_BASENAMES = {"agents.md", "claude.md"}
_STATUS_PREFIX_LENGTH = 3
_RAW_DIFF_FIELD_COUNT = 5


def _run(checkout: str | Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=False,
        capture_output=True,
        env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
    )


def _git_bytes(checkout: str | Path, *args: str) -> bytes:
    result = _run(checkout, *args)
    if result.returncode != 0:
        raise PublishGitError("Git command failed")
    return result.stdout


def _git_text(checkout: str | Path, *args: str) -> str:
    try:
        return _git_bytes(checkout, *args).decode("utf-8").rstrip("\n")
    except UnicodeDecodeError as error:
        raise PublishGitError("Git returned invalid text") from error


def _git_ok(checkout: str | Path, *args: str) -> bool:
    return _run(checkout, *args).returncode == 0


def _optional_git_text(checkout: str | Path, *args: str) -> str | None:
    result = _run(checkout, *args)
    if result.returncode != 0:
        return None
    try:
        return result.stdout.decode("utf-8").rstrip("\n") or None
    except UnicodeDecodeError as error:
        raise PublishGitError("Git returned invalid text") from error


def _git_path(checkout: str | Path, name: str) -> Path:
    path = Path(_git_text(checkout, "rev-parse", "--git-path", name))
    return path if path.is_absolute() else Path(checkout).resolve() / path


def _operation_in_progress(checkout: str | Path) -> bool:
    return any(_git_path(checkout, name).exists() for name in _OPERATIONS)


def _push_remote(checkout: str | Path, branch: str) -> str | None:
    short_branch = branch.removeprefix("refs/heads/")
    for key in (
        f"branch.{short_branch}.pushRemote",
        "remote.pushDefault",
        f"branch.{short_branch}.remote",
    ):
        remote = _optional_git_text(checkout, "config", "--get", key)
        if remote:
            return remote
    return None


def inspect_checkout(checkout: str | Path) -> CheckoutState:
    try:
        root = _git_text(checkout, "rev-parse", "--show-toplevel")
    except PublishGitError as error:
        raise PublishGitError("checkout is not a Git repository") from error
    branch = _optional_git_text(checkout, "symbolic-ref", "-q", "HEAD")
    head = _optional_git_text(checkout, "rev-parse", "--verify", "HEAD")
    if not branch or not branch.startswith("refs/heads/") or not head:
        raise PublishGitError("checkout is not on an attached branch with a commit")
    if _operation_in_progress(checkout):
        raise PublishGitError("checkout has an operation in progress")
    try:
        status = _git_bytes(
            checkout,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
        )
    except PublishGitError as error:
        raise PublishGitError("could not inspect checkout status") from error
    return CheckoutState(
        root=str(Path(root).resolve()),
        branch=branch,
        head=head,
        status=status,
        upstream=_optional_git_text(
            checkout,
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
        ),
        push_remote=_push_remote(checkout, branch),
    )


def require_clean_base(
    checkout: str | Path,
    *,
    branch: str | None = None,
    head: str | None = None,
) -> CheckoutState:
    state = inspect_checkout(checkout)
    if state.root != str(Path(checkout).resolve()):
        raise PublishGitError("checkout root does not match the recorded checkout")
    if branch is not None and state.branch != branch:
        raise PublishGitError("checkout branch does not match the recorded branch")
    if head is not None and state.head != head:
        raise PublishGitError("checkout HEAD does not match the recorded base")
    if state.status:
        raise PublishGitError("checkout is not clean")
    return state


def _decode_status_path(path: bytes) -> str:
    try:
        return path.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PublishGitError("Git status contains a non-UTF-8 path") from error


def status_paths(status: bytes) -> set[str]:
    paths: set[str] = set()
    records = status.split(b"\0")
    index = 0
    while index < len(records) - 1:
        record = records[index]
        if len(record) <= _STATUS_PREFIX_LENGTH or record[2:3] != b" ":
            raise PublishGitError("Git returned malformed porcelain status")
        paths.add(_decode_status_path(record[_STATUS_PREFIX_LENGTH:]))
        if record[:2].find(b"R") >= 0 or record[:2].find(b"C") >= 0:
            index += 1
            if index >= len(records) - 1 or not records[index]:
                raise PublishGitError("Git returned malformed rename status")
            paths.add(_decode_status_path(records[index]))
        index += 1
    if records[-1] != b"":
        raise PublishGitError("Git returned malformed porcelain status")
    return paths


def _validated_report_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value:
        raise PublishGitError("integrator report has an unsafe path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value == "."
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or path.as_posix() != value
    ):
        raise PublishGitError("integrator report has an unsafe path")
    return path


def read_report_paths(path: Path) -> tuple[str, ...]:
    if not path.is_file():
        raise PublishGitError("integrator report is missing")
    try:
        rows = tuple(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError) as error:
        raise PublishGitError("integrator report cannot be read") from error
    if not rows or any(not row for row in rows) or len(rows) != len(set(rows)):
        raise PublishGitError("integrator report has empty or duplicate paths")
    return tuple(_validated_report_path(row).as_posix() for row in rows)


def is_documentation_path(path: str) -> bool:
    candidate = _validated_report_path(path)
    parts = candidate.parts
    basename = parts[-1].casefold()
    if ".git" in {part.casefold() for part in parts} or basename in _DISALLOWED_BASENAMES:
        return False
    if len(parts) > 1 and parts[0].casefold() in {"docs", "doc", "documentation"}:
        return True
    return basename.startswith(("readme", "changelog", "contributing", "security", "support"))


def _report_paths_are_contained(checkout_root: str, paths: Sequence[str]) -> bool:
    root = Path(checkout_root).resolve()
    for value in paths:
        path = _validated_report_path(value)
        resolved = (root / path).resolve()
        if not resolved.is_relative_to(root):
            return False
    return True


def _diff_is_empty(checkout: str | Path, *args: str) -> bool:
    result = _run(checkout, "diff", *args, "--quiet")
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise PublishGitError("could not inspect Git diff")


def _has_staged_gitlink(checkout: str | Path) -> bool:
    raw_diff = _git_bytes(
        checkout,
        "diff",
        "--cached",
        "--raw",
        "-z",
        "--no-abbrev",
        "--no-renames",
    )
    records = raw_diff.split(b"\0")
    if records[-1] != b"" or (len(records) - 1) % 2:
        raise PublishGitError("Git returned malformed raw diff")
    for index in range(0, len(records) - 1, 2):
        header = records[index]
        fields = header[1:].split(b" ") if header.startswith(b":") else ()
        if len(fields) != _RAW_DIFF_FIELD_COUNT:
            raise PublishGitError("Git returned malformed raw diff")
        if b"160000" in fields[:2]:
            return True
    return False


def _raw_commit_details(checkout: str | Path, commit: str) -> tuple[str, tuple[str, ...]]:
    payload = _git_bytes(checkout, "cat-file", "commit", commit)
    header, separator, _ = payload.partition(b"\n\n")
    if not separator:
        raise PublishGitError("published commit is invalid")
    tree: str | None = None
    parents: list[str] = []
    for line in header.splitlines():
        if line.startswith(b"tree "):
            tree = _decode_status_path(line.removeprefix(b"tree "))
        elif line.startswith(b"parent "):
            parents.append(_decode_status_path(line.removeprefix(b"parent ")))
    if tree is None:
        raise PublishGitError("published commit is invalid")
    return tree, tuple(parents)


def validate_prepared_changes(
    receipt: PublishReceipt,
    report_paths: Sequence[str],
    expected_tree: str,
) -> None:
    state = inspect_checkout(receipt.checkout)
    if state.root != str(Path(receipt.checkout).resolve()):
        raise PublishGitError("checkout root does not match the recorded checkout")
    if state.branch != receipt.branch or state.head != receipt.base_commit:
        raise PublishGitError("integrator changed branch or HEAD")
    try:
        contained = _report_paths_are_contained(state.root, report_paths)
    except PublishGitError:
        raise
    if not contained:
        raise PublishGitError("integrator report has an unsafe checkout path")
    if _has_staged_gitlink(receipt.checkout):
        raise PublishGitError("integrator changed a submodule")
    actual_paths = status_paths(state.status)
    if actual_paths != set(report_paths):
        raise PublishGitError("integrator report does not match Git status")
    if any(not is_documentation_path(path) for path in report_paths):
        raise PublishGitError("integrator changed a non-documentation path")
    if _diff_is_empty(receipt.checkout, "--cached"):
        raise PublishGitError("staged documentation diff is empty")
    if not _diff_is_empty(receipt.checkout):
        raise PublishGitError("unstaged changes remain")
    if _git_bytes(receipt.checkout, "ls-files", "--others", "--exclude-standard", "-z"):
        raise PublishGitError("untracked changes remain")
    if _git_text(receipt.checkout, "write-tree") != expected_tree:
        raise PublishGitError("staged tree does not match the prepared tree")


def _require_oid(value: str, message: str) -> None:
    if not _OID.fullmatch(value):
        raise PublishGitError(message)


def verify_published_commit(receipt: PublishReceipt, commit: str) -> str:
    _require_oid(commit, "published commit is invalid")
    _require_oid(receipt.base_commit, "published commit is not a direct child of the base")
    if receipt.expected_tree is None:
        raise PublishGitError("published commit has the wrong tree")
    _require_oid(receipt.expected_tree, "published commit has the wrong tree")
    if not receipt.branch.startswith("refs/heads/"):
        raise PublishGitError("published commit is not reachable from the recorded branch")
    try:
        tree, parents = _raw_commit_details(receipt.checkout, commit)
    except PublishGitError as error:
        raise PublishGitError("published commit is invalid") from error
    if len(parents) != 1:
        raise PublishGitError("published commit must have exactly one parent")
    if parents[0] != receipt.base_commit:
        raise PublishGitError("published commit is not a direct child of the base")
    if tree != receipt.expected_tree:
        raise PublishGitError("published commit has the wrong tree")
    if _git_text(receipt.checkout, "show", "-s", "--format=%s", commit) != "chore: update docs":
        raise PublishGitError("published commit has the wrong subject")
    if not _git_ok(receipt.checkout, "rev-parse", "--verify", f"{receipt.branch}^{{commit}}"):
        raise PublishGitError("published commit is not reachable from the recorded branch")
    if not _git_ok(receipt.checkout, "merge-base", "--is-ancestor", commit, receipt.branch):
        raise PublishGitError("published commit is not reachable from the recorded branch")
    return commit


def can_abandon(receipt: PublishReceipt) -> bool:
    try:
        require_clean_base(
            receipt.checkout,
            branch=receipt.branch,
            head=receipt.base_commit,
        )
    except PublishGitError:
        return False
    return receipt.state == "begun"
