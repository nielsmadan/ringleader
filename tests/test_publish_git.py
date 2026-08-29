from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from ringleader.publish_git import (
    PublishGitError,
    can_abandon,
    inspect_checkout,
    is_documentation_path,
    read_report_paths,
    require_clean_base,
    status_paths,
    validate_prepared_changes,
    verify_published_commit,
)
from ringleader.publish_state import PublishReceipt, ReceiptItem


@dataclass(frozen=True)
class Repo:
    path: Path
    branch: str
    head: str


def run_git(
    path: Path, *args: str, input: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        input=input,
        capture_output=True,
        check=True,
    )


def git_text(path: Path, *args: str) -> str:
    return run_git(path, *args).stdout.decode().strip()


def stage_literal_paths(path: Path, paths: tuple[str, ...]) -> None:
    run_git(
        path,
        "--literal-pathspecs",
        "add",
        "-A",
        "--pathspec-from-file=-",
        "--pathspec-file-nul",
        "--",
        input=b"".join(item.encode() + b"\0" for item in paths),
    )


def commit_index(path: Path, subject: str) -> str:
    run_git(path, "commit", "-m", subject)
    return git_text(path, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path) -> Repo:
    path = tmp_path / "repo with spaces"
    path.mkdir()
    run_git(path, "init", "--initial-branch=main")
    run_git(path, "config", "user.name", "Test User")
    run_git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("initial\n")
    run_git(path, "add", "README.md")
    run_git(path, "commit", "-m", "initial")
    return Repo(
        path, git_text(path, "symbolic-ref", "-q", "HEAD"), git_text(path, "rev-parse", "HEAD")
    )


@pytest.fixture
def receipt(repo: Repo) -> PublishReceipt:
    return PublishReceipt(
        receipt_id="a" * 32,
        repo_key="example/repo",
        checkout=str(repo.path),
        branch=repo.branch,
        base_commit=repo.head,
        registry_path="/private/registry.json",
        items=(
            ReceiptItem(
                harvest_id="harvest",
                digest="b" * 64,
                token=f"harvest:{'b' * 64}",
                session_id="session",
                relative_path="harvest.md",
            ),
        ),
        state="begun",
        outcome=None,
        expected_tree=None,
        changed_paths=(),
        commit=None,
        created_at="2026-08-28T00:00:00+00:00",
    )


def test_require_clean_base_returns_attached_identity(repo):
    state = require_clean_base(repo.path)

    assert state.root == str(repo.path.resolve())
    assert state.branch == "refs/heads/main"
    assert state.head == repo.head
    assert state.status == b""


def test_require_clean_base_rejects_untracked(repo):
    (repo.path / "new.md").write_text("new")

    with pytest.raises(PublishGitError, match="not clean"):
        require_clean_base(repo.path)


def test_require_clean_base_rejects_staged_changes(repo):
    (repo.path / "README.md").write_text("changed\n")
    run_git(repo.path, "add", "README.md")

    with pytest.raises(PublishGitError, match="not clean"):
        require_clean_base(repo.path)


def test_require_clean_base_rejects_detached_head(repo):
    run_git(repo.path, "checkout", "--detach")

    with pytest.raises(PublishGitError, match="attached branch"):
        require_clean_base(repo.path)


def test_require_clean_base_rejects_a_symbolic_head_outside_local_branches(repo):
    run_git(repo.path, "update-ref", "refs/meta/publish", repo.head)
    run_git(repo.path, "symbolic-ref", "HEAD", "refs/meta/publish")

    with pytest.raises(PublishGitError, match="attached branch"):
        require_clean_base(repo.path)


def test_require_clean_base_rejects_unborn_checkout(tmp_path):
    path = tmp_path / "unborn"
    path.mkdir()
    run_git(path, "init", "--initial-branch=main")

    with pytest.raises(PublishGitError, match="attached branch"):
        require_clean_base(path)


def test_require_clean_base_rejects_merge_in_progress(repo):
    merge_head = Path(git_text(repo.path, "rev-parse", "--git-path", "MERGE_HEAD"))
    if not merge_head.is_absolute():
        merge_head = repo.path / merge_head
    merge_head.write_text(repo.head + "\n")

    with pytest.raises(PublishGitError, match="operation"):
        require_clean_base(repo.path)


def test_inspect_checkout_rejects_an_in_progress_operation(repo):
    merge_head = Path(git_text(repo.path, "rev-parse", "--git-path", "MERGE_HEAD"))
    if not merge_head.is_absolute():
        merge_head = repo.path / merge_head
    merge_head.write_text(repo.head + "\n")

    with pytest.raises(PublishGitError, match="operation"):
        inspect_checkout(repo.path)


def test_require_clean_base_rejects_dirty_submodule(repo, tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    run_git(child, "init", "--initial-branch=main")
    run_git(child, "config", "user.name", "Test User")
    run_git(child, "config", "user.email", "test@example.com")
    (child / "README.md").write_text("child\n")
    run_git(child, "add", "README.md")
    run_git(child, "commit", "-m", "initial")
    run_git(
        repo.path,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(child),
        "vendor/child",
    )
    run_git(repo.path, "commit", "-m", "add submodule")
    (repo.path / "vendor" / "child" / "README.md").write_text("dirty\n")

    with pytest.raises(PublishGitError, match="not clean"):
        require_clean_base(repo.path)


def test_validate_prepared_changes_rejects_a_staged_documentation_gitlink(repo, receipt, tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    run_git(child, "init", "--initial-branch=main")
    run_git(child, "config", "user.name", "Test User")
    run_git(child, "config", "user.email", "test@example.com")
    (child / "README.md").write_text("initial\n")
    run_git(child, "add", "README.md")
    run_git(child, "commit", "-m", "initial")
    run_git(
        repo.path,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(child),
        "docs/submodule",
    )
    run_git(repo.path, "commit", "-m", "add submodule")
    receipt = replace(receipt, base_commit=git_text(repo.path, "rev-parse", "HEAD"))
    submodule = repo.path / "docs" / "submodule"
    run_git(submodule, "config", "user.name", "Test User")
    run_git(submodule, "config", "user.email", "test@example.com")
    (submodule / "README.md").write_text("updated\n")
    run_git(submodule, "add", "README.md")
    run_git(submodule, "commit", "-m", "update")
    prepared = _prepared_receipt(repo, receipt, ("docs/submodule",))

    with pytest.raises(PublishGitError, match="submodule"):
        validate_prepared_changes(prepared, ("docs/submodule",), prepared.expected_tree)


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("", "empty or duplicate"),
        ("README.md\nREADME.md\n", "empty or duplicate"),
        ("/README.md\n", "unsafe path"),
        ("docs/../README.md\n", "unsafe path"),
        ("docs/./guide.md\n", "unsafe path"),
        ("docs/guide\x00.md\n", "unsafe path"),
    ],
)
def test_read_report_paths_rejects_unsafe_rows(tmp_path, contents, message):
    report = tmp_path / "report"
    report.write_text(contents)

    with pytest.raises(PublishGitError, match=message):
        read_report_paths(report)


def test_read_report_paths_requires_a_file(tmp_path):
    with pytest.raises(PublishGitError, match="missing"):
        read_report_paths(tmp_path / "missing")


def test_status_paths_includes_both_names_for_renames_and_copies():
    status = b"R  docs/new.md\0docs/old.md\0C  README-copy.md\0README.md\0?? docs/new file.md\0"

    assert status_paths(status) == {
        "docs/new.md",
        "docs/old.md",
        "README-copy.md",
        "README.md",
        "docs/new file.md",
    }


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/guide.md", True),
        ("Documentation/guide.md", True),
        ("README [draft].md", True),
        ("support.txt", True),
        ("AGENTS.md", False),
        ("src/readme.md", True),
        ("notes.md", False),
    ],
)
def test_is_documentation_path_uses_the_limited_allowlist(path, expected):
    assert is_documentation_path(path) is expected


def _prepared_receipt(
    repo: Repo, receipt: PublishReceipt, paths: tuple[str, ...]
) -> PublishReceipt:
    stage_literal_paths(repo.path, paths)
    return replace(
        receipt,
        state="prepared",
        outcome="changed",
        expected_tree=git_text(repo.path, "write-tree"),
        changed_paths=paths,
    )


def test_validate_prepared_changes_accepts_exact_documentation_paths(repo, receipt, tmp_path):
    paths = ("README [draft].md", "docs/guide with spaces.md")
    for path in paths:
        target = repo.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path)
    report = tmp_path / "report"
    report.write_text("\n".join(paths) + "\n")
    report_paths = read_report_paths(report)
    prepared = _prepared_receipt(repo, receipt, report_paths)

    validate_prepared_changes(prepared, report_paths, prepared.expected_tree)


def test_validate_prepared_changes_stages_hostile_pathspecs_literally(repo, receipt):
    path = "docs/[literal]*?.md"
    target = repo.path / path
    target.parent.mkdir()
    target.write_text("literal\n")
    prepared = _prepared_receipt(repo, receipt, (path,))

    validate_prepared_changes(prepared, (path,), prepared.expected_tree)


def test_validate_prepared_changes_rejects_unexpected_changed_path(repo, receipt, tmp_path):
    (repo.path / "README.md").write_text("changed\n")
    (repo.path / "docs").mkdir()
    (repo.path / "docs" / "guide.md").write_text("guide\n")
    paths = ("README.md", "docs/guide.md")
    prepared = _prepared_receipt(repo, receipt, paths)
    report = tmp_path / "report"
    report.write_text("README.md\n")

    with pytest.raises(PublishGitError, match="does not match"):
        validate_prepared_changes(prepared, read_report_paths(report), prepared.expected_tree)


def test_validate_prepared_changes_rejects_non_documentation_path(repo, receipt):
    (repo.path / "ringleader.py").write_text("not docs\n")
    prepared = _prepared_receipt(repo, receipt, ("ringleader.py",))

    with pytest.raises(PublishGitError, match="non-documentation"):
        validate_prepared_changes(prepared, ("ringleader.py",), prepared.expected_tree)


def test_validate_prepared_changes_rejects_changed_branch(repo, receipt):
    run_git(repo.path, "checkout", "-b", "other")

    with pytest.raises(PublishGitError, match="changed branch or HEAD"):
        validate_prepared_changes(receipt, (), repo.head)


def test_validate_prepared_changes_rejects_wrong_planned_tree(repo, receipt):
    (repo.path / "README.md").write_text("changed\n")
    prepared = _prepared_receipt(repo, receipt, ("README.md",))

    with pytest.raises(PublishGitError, match="does not match"):
        validate_prepared_changes(prepared, ("README.md",), repo.head)


def test_validate_prepared_changes_rejects_hook_added_file(repo, receipt):
    (repo.path / "README.md").write_text("changed\n")
    prepared = _prepared_receipt(repo, receipt, ("README.md",))
    (repo.path / "docs").mkdir()
    (repo.path / "docs" / "hook.md").write_text("hook\n")
    stage_literal_paths(repo.path, ("docs/hook.md",))

    with pytest.raises(PublishGitError, match="does not match"):
        validate_prepared_changes(prepared, ("README.md",), prepared.expected_tree)


def test_verify_published_commit_requires_expected_parent_tree_and_subject(repo, receipt):
    (repo.path / "README.md").write_text("changed\n")
    prepared = _prepared_receipt(repo, receipt, ("README.md",))
    commit = commit_index(repo.path, "chore: update docs")

    verified = verify_published_commit(prepared, commit)

    assert verified == commit


def test_prepared_recovery_accepts_verified_commit_after_recorded_branch_advances(repo, receipt):
    (repo.path / "README.md").write_text("changed\n")
    prepared = _prepared_receipt(repo, receipt, ("README.md",))
    commit = commit_index(repo.path, "chore: update docs")
    run_git(repo.path, "commit", "--allow-empty", "-m", "later ordinary work")

    assert verify_published_commit(prepared, commit) == commit


@pytest.mark.parametrize("mutation", ("advanced", "other-branch", "dirty"))
def test_immediate_post_commit_check_requires_exact_head_branch_and_clean(repo, receipt, mutation):
    (repo.path / "README.md").write_text("changed\n")
    prepared = _prepared_receipt(repo, receipt, ("README.md",))
    commit = commit_index(repo.path, "chore: update docs")
    if mutation == "advanced":
        run_git(repo.path, "commit", "--allow-empty", "-m", "later ordinary work")
        message = "HEAD"
    elif mutation == "other-branch":
        run_git(repo.path, "checkout", "-b", "other")
        message = "branch"
    else:
        (repo.path / "ordinary-work.txt").write_text("later work\n")
        message = "clean"

    with pytest.raises(PublishGitError, match=message):
        require_clean_base(repo.path, branch=prepared.branch, head=commit)


def test_verify_published_commit_rejects_integrator_created_commit(repo, receipt):
    (repo.path / "README.md").write_text("changed\n")
    prepared = _prepared_receipt(repo, receipt, ("README.md",))
    commit_index(repo.path, "chore: update docs")
    run_git(repo.path, "commit", "--allow-empty", "-m", "extra")

    with pytest.raises(PublishGitError, match="direct child"):
        verify_published_commit(prepared, git_text(repo.path, "rev-parse", "HEAD"))


def test_verify_published_commit_rejects_a_raw_merge_commit(repo, receipt):
    (repo.path / "README.md").write_text("changed\n")
    prepared = _prepared_receipt(repo, receipt, ("README.md",))
    side = (
        run_git(
            repo.path,
            "commit-tree",
            f"{receipt.base_commit}^{{tree}}",
            "-p",
            receipt.base_commit,
            "-m",
            "side",
        )
        .stdout.decode()
        .strip()
    )
    merge = (
        run_git(
            repo.path,
            "commit-tree",
            prepared.expected_tree,
            "-p",
            receipt.base_commit,
            "-p",
            side,
            "-m",
            "chore: update docs",
        )
        .stdout.decode()
        .strip()
    )
    run_git(repo.path, "update-ref", receipt.branch, merge, receipt.base_commit)

    with pytest.raises(PublishGitError, match="exactly one parent"):
        verify_published_commit(prepared, merge)


def test_verify_published_commit_ignores_replacement_objects(repo, receipt):
    (repo.path / "README.md").write_text("changed\n")
    prepared = _prepared_receipt(repo, receipt, ("README.md",))
    wrong = (
        run_git(
            repo.path,
            "commit-tree",
            f"{receipt.base_commit}^{{tree}}",
            "-p",
            receipt.base_commit,
            "-m",
            "wrong raw commit",
        )
        .stdout.decode()
        .strip()
    )
    replacement = (
        run_git(
            repo.path,
            "commit-tree",
            prepared.expected_tree,
            "-p",
            receipt.base_commit,
            "-m",
            "chore: update docs",
        )
        .stdout.decode()
        .strip()
    )
    run_git(repo.path, "update-ref", receipt.branch, wrong, receipt.base_commit)
    run_git(repo.path, "replace", wrong, replacement)

    with pytest.raises(PublishGitError, match="wrong tree"):
        verify_published_commit(prepared, wrong)


def test_verify_published_commit_rejects_wrong_subject(repo, receipt):
    (repo.path / "README.md").write_text("changed\n")
    prepared = _prepared_receipt(repo, receipt, ("README.md",))
    commit = commit_index(repo.path, "docs: update")

    with pytest.raises(PublishGitError, match="wrong subject"):
        verify_published_commit(prepared, commit)


def test_verify_published_commit_rejects_wrong_tree(repo, receipt):
    (repo.path / "README.md").write_text("changed\n")
    prepared = _prepared_receipt(repo, receipt, ("README.md",))
    (repo.path / "docs").mkdir()
    (repo.path / "docs" / "extra.md").write_text("extra\n")
    stage_literal_paths(repo.path, ("docs/extra.md",))
    commit = commit_index(repo.path, "chore: update docs")

    with pytest.raises(PublishGitError, match="wrong tree"):
        verify_published_commit(prepared, commit)


def test_verify_published_commit_rejects_unreachable_commit(repo, receipt):
    run_git(repo.path, "branch", "recorded", receipt.base_commit)
    receipt = replace(receipt, branch="refs/heads/recorded")
    (repo.path / "README.md").write_text("changed\n")
    prepared = _prepared_receipt(repo, receipt, ("README.md",))
    commit = commit_index(repo.path, "chore: update docs")

    with pytest.raises(PublishGitError, match="reachable"):
        verify_published_commit(prepared, commit)


def test_can_abandon_requires_a_clean_begun_receipt(repo, receipt):
    assert can_abandon(receipt)

    assert not can_abandon(replace(receipt, state="prepared", outcome="no-change"))
    (repo.path / "README.md").write_text("changed\n")
    assert not can_abandon(receipt)
