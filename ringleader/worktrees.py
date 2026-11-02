from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Protocol

from .sources.base import SessionLaunchingSource


class Command(Protocol):
    def __call__(self, argv: Sequence[str], cwd: Path, *, timeout: float = 120) -> str: ...


class WorktreeError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorktreePlan:
    source: SessionLaunchingSource
    session_id: str | None
    cwd: Path
    argv: tuple[str, ...]
    owner: str
    destination: Path | None = None
    branch: str | None = None
    commit: str | None = None


@dataclass(frozen=True)
class PreparedWorktree:
    argv: tuple[str, ...]
    cwd: Path
    session_id: str | None = None


def command(argv: Sequence[str], cwd: Path, *, timeout: float = 120) -> str:
    try:
        result = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorktreeError(f"{argv[0]} failed: {exc}") from exc
    if result.returncode:
        raise WorktreeError(f"{argv[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def _git(cwd: Path, run: Command, *args: str) -> str:
    return run(("git", "-C", str(cwd), *args), cwd)


def _main_checkout(root: Path, run: Command) -> Path:
    common = Path(
        _git(root, run, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve()
    current = Path(_git(root, run, "rev-parse", "--absolute-git-dir")).resolve()
    if common == current:
        return root
    first = _git(root, run, "worktree", "list", "--porcelain", "-z").split("\0", 1)[0]
    if not first.startswith("worktree "):
        raise WorktreeError("Git did not identify the main checkout")
    candidate = Path(first.removeprefix("worktree ")).resolve()
    if not (candidate / ".git").exists():
        return root
    candidate_common = Path(
        _git(candidate, run, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve()
    return candidate if candidate_common == common else root


def plan(
    source: SessionLaunchingSource,
    session_id: str | None,
    cwd: Path,
    transcript: str | None,
    *,
    name: str,
    run: Command = command,
) -> WorktreePlan:
    if source.name not in {"claude-code", "codex", "droid", "opencode", "pi"}:
        raise WorktreeError(f"{source.name} does not support worktrees")
    if session_id is None:
        argv = source.launch_argv()
    elif source.name == "opencode":
        argv = source.resume_argv(session_id)
    else:
        argv = source.fork_argv(session_id)
    if source.name not in {"pi", "opencode"}:
        argv = (*argv, "--worktree")
    root = Path(_git(cwd, run, "rev-parse", "--show-toplevel")).resolve()
    commit = _git(root, run, "rev-parse", "--verify", "HEAD^{commit}")
    if source.name != "pi":
        return WorktreePlan(source, session_id, cwd, argv, source.name)
    if session_id is not None:
        if transcript is None or not Path(transcript).is_file():
            raise WorktreeError("Pi needs an existing transcript file to fork into a worktree")
        argv = source.fork_argv(str(Path(transcript).resolve()))
    main = _main_checkout(root, run)
    destination = main.parent / f"{main.name}.worktrees" / name
    return WorktreePlan(
        source,
        session_id,
        cwd,
        argv,
        "ringleader (persistent Git checkout)",
        destination,
        name,
        commit,
    )


def check_support(value: WorktreePlan, *, run: Command = command) -> None:
    argv = value.argv
    if value.source.name == "opencode":
        help_text = run(("opencode", "api", "--help"), value.cwd)
        if "--param" not in help_text or "--data" not in help_text:
            raise WorktreeError("OpenCode worktrees require the v2 API CLI")
    elif value.source.name == "pi":
        if value.session_id is not None and "--fork" not in run(("pi", "--help"), value.cwd):
            raise WorktreeError("This Pi version does not support --fork; update Pi first")
    else:
        help_argv = (
            (argv[0], argv[1], "--help")
            if value.source.name != "claude-code" and value.session_id is not None
            else (argv[0], "--help")
        )
        if "--worktree" not in run(help_argv, value.cwd):
            raise WorktreeError(
                f"This {argv[0]} version does not support --worktree; update it first"
            )


def _api(
    run: Command,
    cwd: Path,
    operation: str,
    body: dict[str, Any],
    session_id: str | None = None,
) -> str:
    params = ("--param", f"sessionID={session_id}") if session_id else ()
    return run(("opencode", "api", operation, *params, "--data", json.dumps(body)), cwd)


def _field(text: str, *keys: str) -> str:
    try:
        value = json.loads(text)
        for key in keys:
            value = value[key]
        if not isinstance(value, str) or not value:
            raise ValueError("missing field")
        return value
    except (ValueError, KeyError, TypeError) as exc:
        raise WorktreeError(f"Unexpected OpenCode API response for {'.'.join(keys)}") from exc


def _project_id(value: WorktreePlan, run: Command) -> str:
    if value.session_id is not None:
        return _field(
            run(
                ("opencode", "api", "session.get", "--param", f"sessionID={value.session_id}"),
                value.cwd,
            ),
            "data",
            "projectID",
        )
    return _field(
        run(
            ("opencode", "api", "location.get", "--param", f"location[directory]={value.cwd}"),
            value.cwd,
        ),
        "project",
        "id",
    )


def _wait_for_move(
    run: Command,
    cwd: Path,
    session_id: str,
    destination: Path,
    *,
    clock: Callable[[], float] = monotonic,
    pause: Callable[[float], None] = sleep,
) -> None:
    deadline = clock() + 30
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise WorktreeError("Timed out waiting for the fork to enter its worktree")
        text = run(
            ("opencode", "api", "session.get", "--param", f"sessionID={session_id}"),
            cwd,
            timeout=remaining,
        )
        if clock() >= deadline:
            raise WorktreeError("Timed out waiting for the fork to enter its worktree")
        try:
            location = json.loads(text)["data"]["location"]["directory"]
            if not isinstance(location, str):
                raise TypeError("invalid directory")
        except (ValueError, KeyError, TypeError) as exc:
            raise WorktreeError("Cannot verify the fork's directory after session.move") from exc
        if Path(location).resolve() == destination.resolve():
            return
        pause(min(0.1, max(0, deadline - clock())))


def prepare(value: WorktreePlan, *, run: Command = command) -> PreparedWorktree:
    check_support(value, run=run)
    if value.source.name == "pi":
        if value.destination is None or value.branch is None or value.commit is None:
            raise WorktreeError("Incomplete Pi worktree plan")
        try:
            _git(
                value.cwd,
                run,
                "worktree",
                "add",
                "-b",
                value.branch,
                str(value.destination),
                value.commit,
            )
        except WorktreeError as exc:
            raise WorktreeError(
                f"Pi worktree preparation failed at planned destination {value.destination}: {exc}; "
                "any created checkout is retained"
            ) from exc
        return PreparedWorktree(value.argv, value.destination)
    if value.source.name != "opencode":
        return PreparedWorktree(value.argv, value.cwd)
    destination: str | None = None
    fork: str | None = None
    try:
        project = _project_id(value, run)
        destination = _field(
            _api(run, value.cwd, "worktree.create", {"projectID": project, "from": str(value.cwd)}),
            "directory",
        )
        if not Path(destination).is_absolute() or not Path(destination).is_dir():
            raise WorktreeError("OpenCode returned an unusable worktree directory")
        if value.session_id is None:
            return PreparedWorktree(value.argv, Path(destination))
        fork = _field(
            _api(run, value.cwd, "session.fork", {"before": None}, value.session_id),
            "data",
            "id",
        )
        if fork == value.session_id:
            raise WorktreeError("OpenCode returned the source identity instead of a new fork")
        _api(run, value.cwd, "session.move", {"directory": destination, "delivery": None}, fork)
        _wait_for_move(run, value.cwd, fork, Path(destination))
        return PreparedWorktree(value.source.resume_argv(fork), Path(destination), fork)
    except (WorktreeError, KeyboardInterrupt) as exc:
        retained = f"; worktree retained at {destination}" if destination else ""
        retained += f"; fork retained as {fork}" if fork else ""
        if isinstance(exc, KeyboardInterrupt):
            raise KeyboardInterrupt(f"OpenCode worktree preparation cancelled{retained}") from exc
        raise WorktreeError(f"OpenCode worktree preparation failed: {exc}{retained}") from exc
