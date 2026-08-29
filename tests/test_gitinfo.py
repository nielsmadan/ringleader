from pathlib import Path

from ringleader.gitinfo import GitProject, normalize_remote, resolve_git_project


def test_normalize_remote_variants():
    assert normalize_remote("git@github.com:org/repo.git") == "github.com/org/repo"
    assert normalize_remote("https://github.com/org/repo.git") == "github.com/org/repo"
    assert normalize_remote("ssh://git@gitlab.com/org/repo") == "gitlab.com/org/repo"
    assert normalize_remote("https://github.com/org/repo") == "github.com/org/repo"
    # Fix 3: port in ssh:// URL must not leak into the host segment
    assert normalize_remote("ssh://git@github.com:2222/org/repo") == "github.com/org/repo"


def _runner(mapping):
    # mapping: tuple(args after `git -C <cwd>`) -> stdout string, or None to fail
    def run(cmd):
        # cmd = ["git", "-C", cwd, *args]
        key = tuple(cmd[3:])
        return mapping.get(key)

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
            ("config", "--get", "remote.origin.url"): None,  # no remote
        }
    )
    gp = resolve_git_project(cwd, runner=runner)
    assert gp.remote is None
    assert gp.key == cwd
    assert gp.label == tmp_path.name


def test_resolve_returns_none_when_not_a_repo(tmp_path):
    cwd = str(tmp_path)
    runner = _runner({("rev-parse", "--show-toplevel"): None})
    assert resolve_git_project(cwd, runner=runner) is None


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
