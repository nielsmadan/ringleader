import os
import subprocess
import sys
from pathlib import Path

import pytest
from ringleader import cli, refresh, worktrees
from ringleader.commands.start import cmd_start


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo with spaces"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
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
            "--allow-empty",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.chdir(root)
    return root


@pytest.mark.parametrize(
    "agent,binary",
    [
        ("claude", "claude"),
        ("claude-code", "claude"),
        ("codex", "codex"),
        ("droid", "droid"),
        ("opencode", "opencode"),
        ("pi", "pi"),
    ],
)
def test_plain_start_launches_without_history_or_git(tmp_path, monkeypatch, agent, binary):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        refresh, "auto_refresh", lambda *_a, **_k: pytest.fail("start must not scan")
    )
    calls = []
    assert (
        cmd_start(
            [agent],
            runner=lambda argv, cwd: calls.append((argv, cwd)) or 0,
            which=lambda _: "/bin/agent",
        )
        == 0
    )
    assert calls == [((binary,), str(tmp_path))]


@pytest.mark.parametrize("agent", ["claude", "codex", "droid", "pi", "opencode"])
def test_worktree_dry_run_does_not_prepare_or_launch(repo, monkeypatch, capsys, agent):
    inventory = subprocess.check_output(["git", "worktree", "list", "--porcelain"], cwd=repo)
    monkeypatch.setattr(worktrees, "prepare", lambda _: pytest.fail("dry-run must not prepare"))
    assert (
        cmd_start(
            [agent, "--worktree", "--dry-run"],
            runner=lambda *_: pytest.fail("must not launch"),
            which=lambda _: None,
        )
        == 0
    )
    assert "no worktree or session created" in capsys.readouterr().out
    assert (
        subprocess.check_output(["git", "worktree", "list", "--porcelain"], cwd=repo) == inventory
    )
    assert sorted(p.name for p in repo.parent.iterdir()) == [repo.name]


@pytest.mark.parametrize("agent", ["claude", "codex", "droid"])
def test_worktree_start_uses_fresh_native_argv(repo, monkeypatch, agent):
    probes, launches = [], []
    prepare = worktrees.prepare

    def run(argv, cwd, *, timeout=120):
        probes.append(tuple(argv))
        return "--worktree"

    monkeypatch.setattr(worktrees, "prepare", lambda value: prepare(value, run=run))
    assert (
        cmd_start(
            [agent, "--worktree"],
            runner=lambda argv, cwd: launches.append((argv, cwd)) or 0,
            which=lambda _: "/bin/agent",
        )
        == 0
    )
    assert probes == [(agent, "--help")]
    assert launches == [((agent, "--worktree"), str(repo))]


@pytest.mark.parametrize("agent", ["claude", "codex", "droid", "opencode"])
def test_unsupported_worktree_capability_refuses_before_launch(repo, monkeypatch, capsys, agent):
    prepare = worktrees.prepare
    probes = []

    def old_help(argv, cwd, *, timeout=120):
        probes.append(tuple(argv))
        return "old CLI help"

    monkeypatch.setattr(worktrees, "prepare", lambda value: prepare(value, run=old_help))
    assert (
        cmd_start(
            [agent, "--worktree"],
            runner=lambda *_: pytest.fail("unsupported CLI must not launch"),
            which=lambda _: "/bin/agent",
        )
        == 1
    )
    expected = ("opencode", "api", "--help") if agent == "opencode" else (agent, "--help")
    assert probes == [expected]
    assert (
        "require the v2 API CLI" if agent == "opencode" else "update it first"
    ) in capsys.readouterr().err


def test_opencode_command_launches_fresh_in_returned_directory(repo, monkeypatch):
    destination = repo.parent / "native"
    destination.mkdir()
    launches = []

    def prepare(value):
        assert value.source.name == "opencode"
        assert value.session_id is None
        return worktrees.PreparedWorktree(("opencode",), destination)

    monkeypatch.setattr(worktrees, "prepare", prepare)
    assert (
        cmd_start(
            ["opencode", "--worktree"],
            runner=lambda argv, cwd: launches.append((argv, cwd)) or 0,
            which=lambda _: "/bin/opencode",
        )
        == 0
    )
    assert launches == [(("opencode",), str(destination))]


def test_opencode_cancel_after_preparation_reports_created_destination(repo, monkeypatch, capsys):
    destination = repo.parent / "native"
    destination.mkdir()
    monkeypatch.setattr(
        worktrees, "prepare", lambda _: worktrees.PreparedWorktree(("opencode",), destination)
    )

    def cancel(*_):
        raise KeyboardInterrupt

    assert (
        cmd_start(["opencode", "--worktree"], runner=cancel, which=lambda _: "/bin/opencode") == 130
    )
    assert f"any checkout at {destination} is retained" in capsys.readouterr().err


def test_pi_start_creates_persistent_checkout_and_retains_it_after_launch_error(repo, capsys):
    (repo / "untracked.txt").write_text("local only")
    launches = []

    def fail_launch(argv, cwd):
        launches.append((argv, cwd))
        raise OSError("exec failed")

    assert cmd_start(["pi", "--worktree"], runner=fail_launch, which=lambda _: "/bin/pi") == 1
    argv, cwd = launches[0]
    destination = Path(cwd)
    assert argv == ("pi",)
    assert destination.parent == repo.parent / f"{repo.name}.worktrees"
    assert sorted(p.name for p in destination.iterdir()) == [".git"]
    assert (repo / "untracked.txt").read_text() == "local only"
    error = capsys.readouterr().err
    assert str(destination) in error
    assert "retained" in error


def test_missing_binary_refuses_before_worktree_preparation(repo, monkeypatch, capsys):
    monkeypatch.setattr(worktrees, "plan", lambda *_a, **_k: pytest.fail("missing executable"))
    assert cmd_start(["pi", "--worktree"], which=lambda _: None) == 1
    assert "pi is not installed" in capsys.readouterr().err


def test_worktree_requires_git_but_plain_dry_run_does_not(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cmd_start(["pi", "--worktree", "--dry-run"]) == 1
    assert "not a git repository" in capsys.readouterr().err
    assert cmd_start(["pi", "--dry-run"]) == 0
    assert "argv: pi" in capsys.readouterr().out


def test_start_help_and_dispatch(capsys):
    assert cli.main(["start"]) == 0
    assert "rl start" in capsys.readouterr().out
    assert cli.main(["--help"]) == 0
    assert "start" in capsys.readouterr().out


@pytest.mark.parametrize(
    "args", [["unknown"], ["pi", "--to", "codex"], ["pi", "--worktree", "name"]]
)
def test_invalid_arguments_do_not_launch(args):
    with pytest.raises(SystemExit) as error:
        cmd_start(args, runner=lambda *_: pytest.fail("invalid input"))
    assert error.value.code == 2


@pytest.mark.parametrize("in_worktree", [False, True])
def test_cli_exec_replaces_process_in_launch_directory(tmp_path, repo, in_worktree):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "pi"
    binary.write_text("#!/bin/sh\npwd\nprintf 'argc=%s\\n' \"$#\"\nexit 7\n")
    binary.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(root)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ringleader",
            "start",
            "pi",
            *(["--worktree"] if in_worktree else []),
        ],
        cwd=repo if in_worktree else tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 7
    launched, argc = result.stdout.splitlines()
    assert argc == "argc=0"
    if in_worktree:
        assert Path(launched).parent == repo.parent / f"{repo.name}.worktrees"
        assert (Path(launched) / ".git").is_file()
    else:
        assert launched == str(tmp_path)


def test_exec_preserves_buffered_output_before_replacing_process(tmp_path):
    destination = tmp_path / "destination"
    destination.mkdir()
    script = (
        "import sys; from ringleader.launch import exec_agent; "
        "print('before exec'); print('before stderr', file=sys.stderr, end=''); "
        "exec_agent(['/bin/pwd'], sys.argv[1])"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["before exec", str(destination)]
    assert result.stderr == "before stderr"


@pytest.mark.parametrize("in_worktree", [False, True])
def test_cancel_reports_only_relevant_retained_resources(repo, capsys, in_worktree):
    def cancel(*_):
        raise KeyboardInterrupt

    assert (
        cmd_start(
            ["pi", *(["--worktree"] if in_worktree else [])],
            runner=cancel,
            which=lambda _: "/bin/pi",
        )
        == 130
    )
    message = capsys.readouterr().err
    if in_worktree:
        assert f"any checkout at {repo.parent}/{repo.name}.worktrees/rl-" in message
        assert "is retained" in message
    else:
        assert message == "rl-start: cancelled\n"
