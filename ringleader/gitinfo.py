from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GitProject:
    root: str | None
    remote: str | None
    key: str        # stable grouping key
    label: str      # human-friendly group label


def normalize_remote(url: str) -> str:
    u = url.strip()
    if u.endswith(".git"):
        u = u[:-4]
    if u.startswith("git@"):                       # git@host:org/repo
        u = u[4:].replace(":", "/", 1)
    else:
        for pre in ("https://", "http://", "ssh://", "git://"):
            if u.startswith(pre):
                u = u[len(pre):]
                break
        head, _, tail = u.partition("/")           # strip user@host
        if "@" in head:
            head = head.split("@", 1)[1]
        if ":" in head:                            # strip :port from host
            head = head.rsplit(":", 1)[0]
        u = head + ("/" + tail if tail else "")
    return u


def _default_runner(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _git(cwd: str, args: list[str], runner) -> str | None:
    try:
        out = runner(["git", "-C", cwd, *args])
    except Exception:
        return None
    return out.strip() if out else None


def resolve_git_project(cwd: str | None, runner=None) -> GitProject | None:
    if not cwd or not Path(cwd).exists():
        return None
    if runner is None:
        runner = _default_runner
    root = _git(cwd, ["rev-parse", "--show-toplevel"], runner)
    if not root:
        return None
    remote_raw = _git(cwd, ["config", "--get", "remote.origin.url"], runner)
    remote = normalize_remote(remote_raw) if remote_raw else None
    if remote:
        key = remote
        label = remote.rsplit("/", 1)[-1]
    else:
        key = root
        label = Path(root).name
    return GitProject(root=root, remote=remote, key=key, label=label)
