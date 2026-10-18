from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass
class GitProject:
    root: str | None
    remote: str | None
    key: str  # stable grouping key
    label: str  # human-friendly group label
    clone_url: str | None = None


@dataclass(frozen=True)
class GitRun:
    returncode: int | None
    stdout: str = ""
    stderr: str = ""


type GitRunner = Callable[[list[str]], GitRun]
type GitResolver = Callable[[str], GitProject | None]


class GitLookupFailedError(Exception):
    """Git gave no definite answer for a cwd: it timed out, could not run, or failed
    for a reason other than the directory not being a repository."""


def normalize_remote(url: str) -> str:
    u = url.strip()
    u = u.removesuffix(".git")
    if u.startswith("git@"):  # git@host:org/repo
        u = u[4:].replace(":", "/", 1)
    else:
        for pre in ("https://", "http://", "ssh://", "git://"):
            if u.startswith(pre):
                u = u[len(pre) :]
                break
        head, _, tail = u.partition("/")  # strip user@host
        if "@" in head:
            head = head.split("@", 1)[1]
        if ":" in head:  # strip :port from host
            head = head.rsplit(":", 1)[0]
        u = head + ("/" + tail if tail else "")
    return u


def _default_runner(cmd: list[str]) -> GitRun:
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except Exception:  # noqa: BLE001 -- git absent, hung or undecodable: no answer
        return GitRun(None)
    return GitRun(out.returncode, out.stdout, out.stderr)


_NOT_A_REPO = "not a git repository"
_CONFIG_KEY_MISSING = 1


def lookup_git_project(cwd: str, runner: GitRunner = _default_runner) -> GitProject | None:
    """The cwd's git project, or None when git says it is not in a repository (or
    the cwd does not exist). Raises `GitLookupFailedError` when git gave no definite
    answer, so a caller holding an earlier identity can keep it."""
    try:
        if not Path(cwd).exists():
            return None
    except OSError as error:
        raise GitLookupFailedError(f"{cwd}: {error}") from error
    top = runner(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
    if top.returncode not in (0, None) and _NOT_A_REPO in top.stderr:
        return None
    root = top.stdout.strip()
    if top.returncode != 0 or not root:
        raise GitLookupFailedError(f"{cwd}: git rev-parse failed ({top.returncode})")
    origin = runner(["git", "-C", cwd, "config", "--get", "remote.origin.url"])
    if origin.returncode not in (0, _CONFIG_KEY_MISSING):
        raise GitLookupFailedError(f"{cwd}: git config failed ({origin.returncode})")
    remote_raw = origin.stdout.strip() if origin.returncode == 0 else ""
    remote = normalize_remote(remote_raw) if remote_raw else None
    if remote:
        return GitProject(
            root=root,
            remote=remote,
            key=remote,
            label=remote.rsplit("/", 1)[-1],
            clone_url=remote_raw,
        )
    return GitProject(root=root, remote=None, key=root, label=Path(root).name)


def resolve_git_project(cwd: str | None, runner: GitRunner | None = None) -> GitProject | None:
    if not cwd:
        return None
    try:
        return lookup_git_project(cwd, runner or _default_runner)
    except GitLookupFailedError:
        return None


RESOLVE_WORKERS = 8


def resolve_many(cwds: Iterable[str], resolver: GitResolver) -> dict[str, GitProject | None]:
    """`resolver` once per distinct cwd, concurrently. A cwd whose lookup raised
    `GitLookupFailedError` is left out of the result."""
    unique = list(dict.fromkeys(cwds))

    def attempt(cwd: str) -> tuple[bool, GitProject | None]:
        try:
            return True, resolver(cwd)
        except GitLookupFailedError:
            return False, None

    if len(unique) <= 1:
        outcomes = [attempt(cwd) for cwd in unique]
    else:
        with ThreadPoolExecutor(max_workers=min(RESOLVE_WORKERS, len(unique))) as pool:
            outcomes = list(pool.map(attempt, unique))
    return {
        cwd: project for cwd, (answered, project) in zip(unique, outcomes, strict=True) if answered
    }


GIT_RECHECK_AFTER = timedelta(hours=24)


@dataclass(frozen=True)
class GitIdentity:
    project: GitProject | None
    replaces_recorded: bool
    checked_at: str | None


_UNKNOWN = GitIdentity(None, replaces_recorded=False, checked_at=None)


def _checked_at(thread: dict[str, Any], now: datetime) -> datetime | None:
    raw = thread.get("gitCheckedAt")
    if not isinstance(raw, str):
        return None
    try:
        checked = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if checked.tzinfo is None or not now - GIT_RECHECK_AFTER < checked <= now:
        return None
    return checked


def _recent_checks(
    threads: Iterable[dict[str, Any]], now: datetime
) -> dict[str, tuple[datetime, dict[str, Any]]]:
    """Per cwd, the recorded thread whose git identity was checked most recently,
    within `GIT_RECHECK_AFTER`. A pinned thread's git fields can describe the
    directory it was moved from, so it never vouches for its cwd."""
    stamped = sorted(
        (
            (checked, thread)
            for thread in threads
            if isinstance(thread.get("project"), str)
            and not thread.get("locationPinned")
            and (checked := _checked_at(thread, now)) is not None
        ),
        key=lambda pair: pair[0],
    )
    return {thread["project"]: (checked, thread) for checked, thread in stamped}


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _unchanged_since(git_dir: Path, checked: datetime) -> bool:
    try:
        return git_dir.stat().st_ctime <= checked.timestamp()
    except OSError:
        return False


def _recorded_identity(thread: dict[str, Any]) -> GitIdentity:
    key = thread.get("gitProjectKey")
    project = (
        GitProject(
            root=thread.get("gitRoot"),
            remote=thread.get("gitRemote"),
            key=key,
            label=thread.get("gitProjectLabel") or key,
            clone_url=thread.get("gitCloneUrl"),
        )
        if isinstance(key, str) and key
        else None
    )
    return GitIdentity(project, replaces_recorded=False, checked_at=thread.get("gitCheckedAt"))


def _still_current(identity: GitIdentity, cwd: str, checked: datetime) -> bool:
    """A recorded check still describes the cwd: the repository it found has not
    been touched since, or, if it found none, no repository has appeared."""
    if not _is_dir(Path(cwd)):
        return False
    if identity.project is None:
        return not any((d / ".git").exists() for d in (Path(cwd), *Path(cwd).parents))
    root = identity.project.root
    return root is not None and _unchanged_since(Path(root) / ".git", checked)


def _looked_up(cwd: str, project: GitProject | None, stamp: str) -> GitIdentity:
    if project is None and not _is_dir(Path(cwd)):
        return _UNKNOWN
    return GitIdentity(project, replaces_recorded=True, checked_at=stamp)


def git_identities(
    cwds: Iterable[str],
    recorded_threads: Iterable[dict[str, Any]],
    resolver: GitResolver,
    now: datetime,
) -> dict[str, GitIdentity]:
    """Each cwd's git identity: a recorded check that is recent and still current is
    reused, everything else is looked up. A lookup that fails, or a cwd that no
    longer exists, yields an identity that changes nothing recorded."""
    unique = list(dict.fromkeys(cwds))
    recent = _recent_checks(recorded_threads, now)
    reused = {
        cwd: identity
        for cwd in unique
        if cwd in recent
        and _still_current(identity := _recorded_identity(recent[cwd][1]), cwd, recent[cwd][0])
    }
    looked_up = resolve_many([cwd for cwd in unique if cwd not in reused], resolver)
    stamp = now.isoformat()
    fresh = {cwd: _looked_up(cwd, project, stamp) for cwd, project in looked_up.items()}
    return {cwd: reused.get(cwd) or fresh.get(cwd, _UNKNOWN) for cwd in unique}
