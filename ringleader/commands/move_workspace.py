"""Move a workspace directory and take its sessions with it.

Three things have to change together, and they are owned by three different places:

  the directory      — moved on disk here
  session storage    — the agent keys it by launch cwd, so it is relocated through
                       the source seam (`WorkspaceRelocatingSource`); without this
                       the sessions stay unreachable from the new path
  the registry       — retargeted and pinned, or the next scan reverts it from the
                       transcript's stale baked-in cwd

`move` does all three. `retarget` does the last two, for when the directory was
already moved by hand — which is also what makes `move` re-runnable, since a failure
after the directory moved leaves exactly that state.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from .. import gitinfo, registry, scanner
from ..slug import is_beneath, normalize, remap, slugify
from ..sources.base import WorkspaceRelocatingSource
from ..stateio import publish_lock
from . import default_registry


@dataclass
class _Plan:
    old: str
    new: str
    threads: list = field(default_factory=list)  # (thread, new_project)
    arch: list = field(default_factory=list)  # (archived record, new_project)
    whole: list = field(default_factory=list)  # (source, old_cwd, new_cwd)
    per: list = field(default_factory=list)  # (thread, source, new_project)
    unsupported: list = field(default_factory=list)  # threads whose source can't relocate
    already_done: bool = False  # nothing beneath OLD, but NEW already has sessions


@cache
def _git_for(new_project: str) -> dict | None:
    gp = gitinfo.resolve_git_project(new_project)
    if gp is None:
        return None
    return {
        "key": gp.key,
        "label": gp.label,
        "root": gp.root,
        "remote": gp.remote,
        "clone_url": gp.clone_url,
    }


def _relocating_sources() -> dict:
    return {
        s.name: s
        for s in scanner.active_sources()
        if "relocates_workspace" in s.capabilities and isinstance(s, WorkspaceRelocatingSource)
    }


def _drifted(thread: dict) -> bool:
    """True when the session's recorded cwd is not the directory it was launched in
    — a worktree, or any session that cd'd. Its storage lives under the launch dir's
    slug, so it can never ride along with a rename keyed on the recorded cwd."""
    return Path(thread["file"]).parent.name != slugify(normalize(thread["project"]))


def _listing(slug_dir: Path) -> list[Path]:
    """Directory contents, read once per directory rather than once per thread."""
    key = str(slug_dir)
    if _LISTINGS.get(key) is None:
        try:
            _LISTINGS[key] = sorted(slug_dir.glob("*.jsonl"))
        except OSError:
            _LISTINGS[key] = []
    return _LISTINGS[key]


_LISTINGS: dict[str, list[Path]] = {}


def build_plan(reg: dict, old: str, new: str, sources: dict) -> _Plan:
    _LISTINGS.clear()
    plan = _Plan(old=old, new=new)
    plan.threads = [
        (t, remap(t["project"], old, new))
        for t in reg.get("threads", [])
        if is_beneath(t.get("project"), old)
    ]
    for record in reg.get("archived", []):
        embedded = record.get("thread") or {}
        if is_beneath(embedded.get("project"), old):
            plan.arch.append((record, remap(embedded["project"], old, new)))

    if not plan.threads and not plan.arch:
        plan.already_done = any(is_beneath(t.get("project"), new) for t in reg.get("threads", []))
    matched = {t.get("file") for t, _ in plan.threads}
    seen: set[str] = set()
    for thread, new_project in plan.threads:
        source = thread.get("source") or "claude-code"
        if source not in sources or not thread.get("file"):
            plan.unsupported.append(thread)
            continue
        slug_dir = Path(thread["file"]).parent
        siblings = _listing(slug_dir)
        # A whole directory may only be renamed when we own every session in it, and
        # only when none of them has drifted — a drifted sibling would otherwise be
        # scheduled for a second, per-session move out of a directory already gone.
        owned = bool(siblings) and all(str(p) in matched for p in siblings)
        movable = owned and not any(
            _drifted(t) for t, _ in plan.threads if t.get("file") in {str(x) for x in siblings}
        )
        if movable:
            if str(slug_dir) not in seen:
                seen.add(str(slug_dir))
                plan.whole.append((source, thread["project"], new_project))
        else:
            plan.per.append((thread, source, new_project))
    return plan


def collisions(plan: _Plan, sources: dict) -> list[str]:
    """Destinations that already hold session storage. Refused rather than merged:
    merging renames the colliding session and rewrites its sessionId, which is the
    registry's primary key."""
    out = []
    for source, _old_cwd, new_cwd in plan.whole:
        dst = sources[source].storage_for(new_cwd)
        if dst.exists():
            out.append(str(dst))
    for thread, source, new_project in plan.per:
        dst = sources[source].storage_for(new_project) / Path(thread["file"]).name
        if dst.exists():
            out.append(str(dst))
    return out


def _refuse_empty(
    plan: _Plan, old: str, verb: str, *, move_files: bool, allow_empty: bool
) -> int | None:
    """Nothing matched. For `retarget` that is simply an error; for `move` it is a
    near-certain mistake — matching is lexical and case-sensitive while the filesystem
    is neither, so a mistyped, case-differing or symlinked OLD would move the directory
    and orphan every session under it without any error."""
    if plan.threads or plan.arch:
        return None
    if not move_files:
        if plan.already_done:
            print(f"nothing to retarget — already recorded beneath {plan.new}")
            return 0
        print(f"rl-workspace-{verb}: no sessions recorded beneath {old}", file=sys.stderr)
        return 1
    if allow_empty:
        return None
    print(
        f"rl-workspace-{verb}: {old} has no recorded sessions — check the path's case and "
        "that it is not reached through a symlink, or pass --allow-empty to move anyway",
        file=sys.stderr,
    )
    return 1


def _preflight(old: str, new: str, *, move_files: bool) -> str | None:
    if is_beneath(new, old):
        return f"{new} is inside {old}"
    if not move_files:
        return None if Path(new).is_dir() else f"{new} is not a directory"
    if not Path(old).is_dir():
        return f"{old} is not a directory"
    if Path(new).exists():
        return f"{new} already exists"
    if not Path(new).parent.is_dir():
        return f"{Path(new).parent} does not exist"
    return None


def _describe(plan: _Plan, *, move_files: bool) -> None:
    if move_files:
        print(f"move {plan.old} -> {plan.new}")
    for _source, old_cwd, new_cwd in plan.whole:
        print(f"  storage  {slugify(normalize(old_cwd))} -> {slugify(normalize(new_cwd))}")
    for thread, _source, _new_project in plan.per:
        why = "cwd drifted from its launch dir" if _drifted(thread) else "shared directory"
        print(f"  session  {thread['sessionId'][:8]}  moved on its own ({why})")
    for thread in plan.unsupported:
        print(f"  session  {thread['sessionId'][:8]}  registry only ({thread.get('source')})")
    print(f"{len(plan.threads)} session(s), {len(plan.arch)} archived")


def _relocate_storage(plan: _Plan, sources: dict, verb: str) -> tuple[dict, set[str]]:
    """Returns the old->new transcript paths, and the session ids whose storage did
    NOT move. A failed session must not be pinned: pinning rewrites its `project`, and
    `retarget` finds threads by the old path — so pinning a stranded session puts it
    beyond the reach of the one command that could repair it."""
    moved: dict[str, str] = {}
    stranded: set[str] = set()
    for source, old_cwd, new_cwd in plan.whole:
        try:
            moved.update(sources[source].relocate_storage(old_cwd, new_cwd))
        except (RuntimeError, OSError) as e:
            print(f"rl-workspace-{verb}: {old_cwd}: {e}", file=sys.stderr)
            for thread, _ in plan.threads:
                if thread.get("project") == old_cwd:
                    stranded.add(thread["sessionId"])
    for thread, source, new_project in plan.per:
        try:
            moved[thread["file"]] = sources[source].relocate_session(thread["file"], new_project)
        except (RuntimeError, OSError) as e:
            print(f"rl-workspace-{verb}: {thread['sessionId'][:8]}: {e}", file=sys.stderr)
            stranded.add(thread["sessionId"])
    return moved, stranded


def _retarget_registry(
    registry_path: str, old: str, new: str, moved: dict, sources: dict, stranded: set[str]
) -> _Plan:
    with publish_lock():
        reg = registry.load_registry(registry_path)
        plan = build_plan(reg, old, new, sources)
        plan.threads = [t for t in plan.threads if t[0]["sessionId"] not in stranded]
        for thread, new_project in plan.threads:
            registry.pin_thread_location(thread, new_project, _git_for(new_project))
            if thread.get("file") in moved:
                thread["file"] = moved[thread["file"]]
        for record, new_project in plan.arch:
            embedded = record["thread"]
            was = embedded["project"]
            registry.pin_thread_location(embedded, new_project, _git_for(new_project))
            # the tombstone's own `project` is often a git label, not a path
            if record.get("project") == was:
                record["project"] = new_project
        registry.save_registry(registry_path, reg)
        return plan


def _run(argv: list[str], *, move_files: bool) -> int:
    verb = "move" if move_files else "retarget"
    ap = argparse.ArgumentParser(
        prog=f"rl workspace {verb}",
        description=(
            "Move a workspace directory, relocate its session storage, and retarget the registry."
            if move_files
            else "Retarget sessions after a workspace directory was moved by hand."
        ),
    )
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    if move_files:
        ap.add_argument("--allow-empty", action="store_true", help="move even if no sessions match")
    args = ap.parse_args(argv)
    if not move_files:
        args.allow_empty = False

    old, new = normalize(args.old), normalize(args.new)
    problem = _preflight(old, new, move_files=move_files)
    if problem is not None:
        print(f"rl-workspace-{verb}: {problem}", file=sys.stderr)
        return 1

    sources = _relocating_sources()
    plan = build_plan(registry.load_registry(args.registry), old, new, sources)
    empty = _refuse_empty(plan, old, verb, move_files=move_files, allow_empty=args.allow_empty)
    if empty is not None:
        return empty

    occupied = collisions(plan, sources)
    if occupied:
        print(f"rl-workspace-{verb}: session storage already exists at:", file=sys.stderr)
        for path in occupied:
            print(f"  {path}", file=sys.stderr)
        print("refusing to merge — resolve it by hand, then re-run", file=sys.stderr)
        return 1

    _describe(plan, move_files=move_files)
    if args.dry_run:
        return 0
    if not args.yes and not _confirmed():
        return 0

    if move_files:
        # re-checked here, not just in preflight: the confirmation above blocks for an
        # unbounded time, and shutil.move does not raise on an existing directory — it
        # moves OLD *inside* it and reports success
        if Path(new).exists():
            print(f"rl-workspace-{verb}: {new} appeared while waiting — aborting", file=sys.stderr)
            return 1
        try:
            shutil.move(old, new)
        except OSError as e:
            print(f"rl-workspace-{verb}: {e}", file=sys.stderr)
            return 1

    moved, stranded = _relocate_storage(plan, sources, verb)
    applied = _retarget_registry(args.registry, old, new, moved, sources, stranded)
    if move_files:
        print(f"moved {old} -> {new}")
    print(f"retargeted {len(applied.threads)} session(s), {len(applied.arch)} archived")
    if stranded:
        print(
            f"{len(stranded)} session(s) left in place and NOT retargeted — "
            f"re-run `rl workspace retarget {old} {new}` once the cause is cleared",
            file=sys.stderr,
        )
    return 1 if stranded else 0


def _confirmed() -> bool:
    try:
        if input("Proceed? [y/N] ").strip().lower() in ("y", "yes"):
            return True
    except EOFError:
        pass
    print("aborted")
    return False


def cmd_move(argv: list[str]) -> int:
    return _run(argv, move_files=True)


def cmd_retarget(argv: list[str]) -> int:
    return _run(argv, move_files=False)
