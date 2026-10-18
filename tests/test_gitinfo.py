import subprocess
import threading
from pathlib import Path

import pytest
from ringleader.gitinfo import (
    GitLookupFailedError,
    GitProject,
    GitRun,
    lookup_git_project,
    normalize_remote,
    resolve_git_project,
    resolve_many,
)


def test_normalize_remote_variants():
    assert normalize_remote("git@github.com:org/repo.git") == "github.com/org/repo"
    assert normalize_remote("https://github.com/org/repo.git") == "github.com/org/repo"
    assert normalize_remote("ssh://git@gitlab.com/org/repo") == "gitlab.com/org/repo"
    assert normalize_remote("https://github.com/org/repo") == "github.com/org/repo"
    # Fix 3: port in ssh:// URL must not leak into the host segment
    assert normalize_remote("ssh://git@github.com:2222/org/repo") == "github.com/org/repo"


_TOP = ("rev-parse", "--show-toplevel")
_ORIGIN = ("config", "--get", "remote.origin.url")
_TIMED_OUT = GitRun(None)
_NO_ORIGIN = GitRun(1)
_NOT_A_REPO = GitRun(128, stderr="fatal: not a git repository (or any of the parent directories)")


def _runner(mapping):
    # mapping: tuple(args after `git -C <cwd>`) -> stdout string, or a GitRun as is
    def run(cmd):
        answer = mapping[tuple(cmd[3:])]
        return answer if isinstance(answer, GitRun) else GitRun(0, answer)

    return run


def test_resolve_prefers_remote_slug(tmp_path):
    cwd = str(tmp_path)  # must exist
    runner = _runner(
        {
            ("rev-parse", "--show-toplevel"): f"{cwd}\n",
            ("config", "--get", "remote.origin.url"): "git@github.com:me/cool-repo.git\n",
        }
    )
    gp = resolve_git_project(cwd, runner=runner)
    assert isinstance(gp, GitProject)
    assert gp.key == "github.com/me/cool-repo"
    assert gp.label == "cool-repo"
    assert gp.root == cwd
    assert gp.remote == "github.com/me/cool-repo"


def test_resolve_keeps_raw_clone_url(tmp_path):
    raw = "git@github.com:me/cool-repo.git"
    gp = resolve_git_project(
        str(tmp_path),
        runner=_runner(
            {
                ("rev-parse", "--show-toplevel"): f"{tmp_path}\n",
                ("config", "--get", "remote.origin.url"): f"{raw}\n",
            }
        ),
    )
    assert gp.remote == "github.com/me/cool-repo"
    assert gp.clone_url == raw


def test_resolve_falls_back_to_toplevel_without_remote(tmp_path):
    cwd = str(tmp_path)
    runner = _runner(
        {
            ("rev-parse", "--show-toplevel"): f"{cwd}\n",
            ("config", "--get", "remote.origin.url"): _NO_ORIGIN,
        }
    )
    gp = resolve_git_project(cwd, runner=runner)
    assert gp.remote is None
    assert gp.key == cwd
    assert gp.label == tmp_path.name


def test_resolve_returns_none_when_not_a_repo(tmp_path):
    cwd = str(tmp_path)
    runner = _runner({("rev-parse", "--show-toplevel"): _NOT_A_REPO})
    assert lookup_git_project(cwd, runner=runner) is None


def test_resolve_returns_none_when_cwd_missing(tmp_path):
    missing = str(tmp_path / "gone")

    # runner should never be consulted; pass one that would explode if called
    def boom(cmd):
        raise AssertionError("runner must not run for a missing cwd")

    assert resolve_git_project(missing, runner=boom) is None


def test_an_unreadable_cwd_resolves_to_none_instead_of_raising(monkeypatch):
    # a directory the process cannot stat must not abort the caller's whole scan
    def boom(self, *a, **k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "stat", boom)
    assert resolve_git_project("/somewhere/unreadable") is None


def test_resolve_many_runs_the_resolver_concurrently_once_per_distinct_cwd():
    barrier = threading.Barrier(3, timeout=5)
    calls = []

    def resolver(cwd):
        calls.append(cwd)
        barrier.wait()
        return GitProject(root=cwd, remote=None, key=cwd, label=cwd)

    resolved = resolve_many(["/a", "/b", "/a", "/c"], resolver)

    assert sorted(calls) == ["/a", "/b", "/c"]
    assert list(resolved) == ["/a", "/b", "/c"]
    assert resolved["/b"].key == "/b"


def test_a_timed_out_remote_lookup_is_a_failure_not_a_path_keyed_project(tmp_path):
    runner = _runner({_TOP: f"{tmp_path}\n", _ORIGIN: _TIMED_OUT})

    with pytest.raises(GitLookupFailedError):
        lookup_git_project(str(tmp_path), runner=runner)


def test_a_timed_out_toplevel_lookup_is_a_failure_not_a_missing_repo(tmp_path):
    with pytest.raises(GitLookupFailedError):
        lookup_git_project(str(tmp_path), runner=_runner({_TOP: _TIMED_OUT}))


def test_a_git_error_other_than_not_a_repo_is_a_failure(tmp_path):
    dubious = GitRun(128, stderr="fatal: detected dubious ownership in repository")

    with pytest.raises(GitLookupFailedError):
        lookup_git_project(str(tmp_path), runner=_runner({_TOP: dubious}))


def test_a_broken_git_config_is_a_failure(tmp_path):
    runner = _runner({_TOP: f"{tmp_path}\n", _ORIGIN: GitRun(3, stderr="bad config line")})

    with pytest.raises(GitLookupFailedError):
        lookup_git_project(str(tmp_path), runner=runner)


def test_resolve_git_project_answers_none_for_a_failed_lookup(tmp_path):
    runner = _runner({_TOP: f"{tmp_path}\n", _ORIGIN: _TIMED_OUT})

    assert resolve_git_project(str(tmp_path), runner=runner) is None


def test_resolve_many_leaves_out_a_cwd_whose_lookup_failed():
    def resolver(cwd):
        if cwd == "/flaky":
            raise GitLookupFailedError(cwd)
        return GitProject(root=cwd, remote=None, key=cwd, label=cwd)

    resolved = resolve_many(["/a", "/flaky", "/b"], resolver)

    assert list(resolved) == ["/a", "/b"]


def test_the_default_runner_reads_a_real_repository_and_a_real_non_repository(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:me/real.git"],
        check=True,
    )

    project = lookup_git_project(str(repo))

    assert project is not None
    assert (project.key, Path(project.root or "").resolve()) == (
        "github.com/me/real",
        repo.resolve(),
    )
    assert lookup_git_project(str(plain)) is None
