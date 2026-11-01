from __future__ import annotations

import json
import subprocess
from dataclasses import replace

import pytest
from ringleader import scanner, worktrees


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo with spaces"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / "tracked.txt").write_text("committed\n")
    subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=QA",
            "-c",
            "user.email=qa@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    return root


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("claude-code", ("claude", "--resume", "session-id", "--fork-session", "--worktree")),
        ("codex", ("codex", "fork", "session-id", "--worktree")),
        ("droid", ("droid", "resume", "session-id", "--fork", "--worktree")),
    ],
)
def test_native_creation_is_owned_by_harness(repo, source, expected):
    value = worktrees.plan(
        scanner.source_by_name(source), "session-id", repo, None, name="rl-fixed"
    )
    before = worktrees.command(["git", "worktree", "list", "--porcelain"], repo)
    calls = []

    def run(argv, cwd, *, timeout=120):
        calls.append((tuple(argv), cwd))
        return "--worktree"

    assert worktrees.prepare(value, run=run) == worktrees.PreparedWorktree(expected, repo)
    assert len(calls) == 1
    assert calls[0][0][-1] == "--help"
    assert worktrees.command(["git", "worktree", "list", "--porcelain"], repo) == before


def test_pi_checkout_has_committed_content_and_source_stays_dirty(repo, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("history\n")
    (repo / "tracked.txt").write_text("local change\n")
    (repo / "untracked.txt").write_text("local only\n")
    value = worktrees.plan(
        scanner.source_by_name("pi"), "session-id", repo, str(transcript), name="rl-fixed"
    )
    assert value.destination.parent == repo.parent / f"{repo.name}.worktrees"

    def run(argv, cwd, *, timeout=120):
        return "--fork" if argv[0] == "pi" else worktrees.command(argv, cwd)

    prepared = worktrees.prepare(value, run=run)
    argv, cwd = prepared.argv, prepared.cwd
    assert argv == ("pi", "--fork", str(transcript.resolve()))
    assert (cwd / "tracked.txt").read_text() == "committed\n"
    assert sorted(p.name for p in cwd.iterdir()) == [".git", "tracked.txt"]
    assert (repo / "tracked.txt").read_text() == "local change\n"
    assert (repo / "untracked.txt").read_text() == "local only\n"
    assert transcript.read_text() == "history\n"
    assert worktrees.command(["git", "branch", "--show-current"], cwd) == value.branch
    with pytest.raises(worktrees.WorktreeError):
        worktrees.prepare(value, run=run)
    assert (cwd / "tracked.txt").read_text() == "committed\n"


def test_pi_fork_from_linked_worktree_stays_with_original_repository(repo, tmp_path):
    linked = tmp_path / "linked"
    worktrees.command(["git", "worktree", "add", "-b", "linked", str(linked)], repo)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("history")
    value = worktrees.plan(
        scanner.source_by_name("pi"), "session-id", linked, str(transcript), name="rl-fixed"
    )
    assert value.destination.parent == repo.parent / f"{repo.name}.worktrees"


def test_opencode_preserves_native_setup_and_moves_only_fork(repo, tmp_path):
    destination = tmp_path / "native-worktree"
    destination.mkdir()
    calls = []

    def run(argv, cwd, *, timeout=120):
        calls.append(tuple(argv))
        if "--help" in argv:
            return "--param --data"
        operation = argv[2]
        if operation == "session.get":
            if argv[-1] == "sessionID=forked":
                return json.dumps({"data": {"location": {"directory": str(destination)}}})
            assert argv[-1] == "sessionID=original"
            return json.dumps({"data": {"projectID": "project-id"}})
        body = json.loads(argv[-1])
        if operation == "worktree.create":
            assert body == {"projectID": "project-id", "from": str(repo)}
            return json.dumps({"directory": str(destination)})
        if operation == "session.fork":
            assert argv[4] == "sessionID=original"
            assert body == {"before": None}
            return json.dumps({"data": {"id": "forked"}})
        assert operation == "session.move"
        assert argv[4] == "sessionID=forked"
        assert body == {"directory": str(destination), "delivery": None}
        return ""

    value = worktrees.plan(
        scanner.source_by_name("opencode"), "original", repo, None, name="rl-fixed"
    )
    assert worktrees.prepare(value, run=run) == worktrees.PreparedWorktree(
        ("opencode", "--session", "forked"), destination, "forked"
    )
    assert [args[2] for args in calls[1:]] == [
        "session.get",
        "worktree.create",
        "session.fork",
        "session.move",
        "session.get",
    ]


def test_opencode_failure_reports_retained_resources(repo, tmp_path):
    destination = tmp_path / "native-worktree"
    destination.mkdir()

    def run(argv, cwd, *, timeout=120):
        if "--help" in argv:
            return "--param --data"
        match argv[2]:
            case "session.get":
                return '{"data":{"projectID":"project-id"}}'
            case "worktree.create":
                return json.dumps({"directory": str(destination)})
            case "session.fork":
                return '{"data":{"id":"forked"}}'
            case _:
                raise worktrees.WorktreeError("move failed")

    value = worktrees.plan(
        scanner.source_by_name("opencode"), "original", repo, None, name="rl-fixed"
    )
    with pytest.raises(worktrees.WorktreeError, match="fork retained as forked") as error:
        worktrees.prepare(value, run=run)
    assert str(destination) in str(error.value)


def test_pi_old_binary_refuses_before_git_mutation(repo, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("history")
    value = worktrees.plan(
        scanner.source_by_name("pi"), "session-id", repo, str(transcript), name="rl-fixed"
    )
    with pytest.raises(worktrees.WorktreeError, match="does not support --fork"):
        worktrees.prepare(value, run=lambda *_: "old help")
    assert list(value.destination.parent.glob("*")) == []


def test_incomplete_pi_plan_refuses(repo):
    value = worktrees.WorktreePlan(scanner.source_by_name("pi"), "id", repo, ("pi",), "ringleader")
    with pytest.raises(worktrees.WorktreeError, match="Incomplete"):
        worktrees.prepare(replace(value, commit="abc"), run=lambda *_: "--fork")


@pytest.mark.parametrize("layout", ["submodule", "separate-git-dir"])
def test_pi_destination_follows_checkout_not_git_storage(repo, tmp_path, layout):
    checkout = tmp_path / "checkout"
    if layout == "submodule":
        subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                str(repo),
                "sub",
            ],
            check=True,
            capture_output=True,
        )
        checkout = checkout / "sub"
    else:
        subprocess.run(
            [
                "git",
                "clone",
                "--separate-git-dir",
                str(tmp_path / "storage"),
                str(repo),
                str(checkout),
            ],
            check=True,
            capture_output=True,
        )
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("history")
    first = worktrees.plan(
        scanner.source_by_name("pi"), "id", checkout, str(transcript), name="rl-fixed"
    )
    second = worktrees.plan(
        scanner.source_by_name("pi"), "id", checkout, str(transcript), name="rl-fixed"
    )
    assert (
        first.destination
        == second.destination
        == checkout.parent / f"{checkout.name}.worktrees" / "rl-fixed"
    )
    assert first.branch == second.branch == "rl-fixed"


def test_move_polling_waits_for_destination_with_remaining_budget(tmp_path):
    ticks = [0.0]
    budgets = []

    def pause(seconds):
        ticks[0] += seconds

    def run(argv, cwd, *, timeout=120):
        budgets.append(timeout)
        ticks[0] += 1
        directory = tmp_path if len(budgets) == 3 else tmp_path / "old"
        return json.dumps({"data": {"location": {"directory": str(directory)}}})

    worktrees._wait_for_move(run, tmp_path, "fork", tmp_path, clock=lambda: ticks[0], pause=pause)
    assert budgets == pytest.approx([30, 28.9, 27.8])


@pytest.mark.parametrize("matches", [False, True])
def test_move_polling_rejects_late_response(tmp_path, matches):
    ticks = [0.0]
    calls = []

    def run(argv, cwd, *, timeout=120):
        calls.append(timeout)
        ticks[0] = 31
        directory = tmp_path if matches else tmp_path / "old"
        return json.dumps({"data": {"location": {"directory": str(directory)}}})

    with pytest.raises(worktrees.WorktreeError, match="Timed out"):
        worktrees._wait_for_move(
            run, tmp_path, "fork", tmp_path, clock=lambda: ticks[0], pause=lambda _: None
        )
    assert calls == [30]


def test_malformed_move_response_reports_retained_worktree_and_fork(repo, tmp_path):
    destination = tmp_path / "destination"
    destination.mkdir()

    def run(argv, cwd, *, timeout=120):
        if "--help" in argv:
            return "--param --data"
        if argv[2] == "session.get":
            return (
                '{"data":{"projectID":"project"}}'
                if argv[-1] == "sessionID=original"
                else '{"data":{"location":null}}'
            )
        if argv[2] == "worktree.create":
            return json.dumps({"directory": str(destination)})
        if argv[2] == "session.fork":
            return '{"data":{"id":"forked"}}'
        return ""

    value = worktrees.plan(
        scanner.source_by_name("opencode"), "original", repo, None, name="rl-fixed"
    )
    with pytest.raises(worktrees.WorktreeError, match="Cannot verify") as error:
        worktrees.prepare(value, run=run)
    assert "fork retained as forked" in str(error.value)
    assert str(destination) in str(error.value)
