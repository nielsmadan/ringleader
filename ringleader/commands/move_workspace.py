"""Move a workspace directory and take its sessions with it.

Three things have to change together, and they are owned by three different places:

  the directory      — moved on disk here
  session storage    — reached through the source seam, in one of two shapes:
                       an agent that keys storage by launch cwd has it relocated
                       (`WorkspaceRelocatingSource`), while one that files
                       transcripts flat and records the cwd in its own index has
                       that entry rewritten in place (`WorkspaceRebindingSource`);
                       without this the sessions stay unreachable from the new path
  the registry       — retargeted and pinned, or the next scan reverts it from the
                       transcript's stale baked-in cwd

`move` does all three. `retarget` does the last two, for when the directory was
already moved by hand — which is also what makes `move` re-runnable, since a failure
after the directory moved leaves exactly that state.

A fourth kind of state also breaks, and is deliberately NOT repaired here: build
artifacts inside the tree that bake in an absolute path (`.venv`, `node_modules`,
`target`). Those are reported, never rewritten. The dividing line is who else can
fix it — an editable `.pth` under `~/.local/share/uv/tools` lives outside the moved
tree and nothing but this command knows the move happened, whereas a virtualenv is
derived state whose own package manager regenerates it correctly. Rewriting the
paths inside build artifacts would mean tracking every one of those formats forever,
and a subtly wrong rewrite leaves something that half-works.
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
from ..sources.base import IndexUpdate, WorkspaceRebindingSource, WorkspaceRelocatingSource
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
    rebind: list = field(default_factory=list)  # (thread, source, new_project)
    unsupported: list = field(default_factory=list)  # threads whose storage can't be followed
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


@dataclass(frozen=True)
class _Sources:
    """The two source shapes a move drives, resolved once and carried together. They
    are always needed as a pair: a plan built with only one silently misclassifies
    every session belonging to the other."""

    relocating: dict
    rebinding: dict


def _resolve_sources() -> _Sources:
    return _Sources(
        relocating=scanner.sources_with("relocates_workspace", WorkspaceRelocatingSource),
        rebinding=scanner.sources_with("rebinds_workspace", WorkspaceRebindingSource),
    )


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


def build_plan(reg: dict, old: str, new: str, srcs: _Sources) -> _Plan:
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
        if not thread.get("file"):
            plan.unsupported.append(thread)
            continue
        # Checked before the slug-directory logic below: these sources move no
        # transcript, so a slug dir and its siblings say nothing about them.
        if source in srcs.rebinding:
            plan.rebind.append((thread, source, new_project))
            continue
        if source not in srcs.relocating:
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


def collisions(plan: _Plan, srcs: _Sources) -> list[str]:
    """Destinations that already hold session storage. Refused rather than merged:
    merging renames the colliding session and rewrites its sessionId, which is the
    registry's primary key."""
    out = []
    for source, _old_cwd, new_cwd in plan.whole:
        dst = srcs.relocating[source].storage_for(new_cwd)
        if dst.exists():
            out.append(str(dst))
    for thread, source, new_project in plan.per:
        dst = srcs.relocating[source].storage_for(new_project) / Path(thread["file"]).name
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


def editable_installs(old: str) -> list[Path]:
    """`.pth` files whose editable-install target is `old` or sits beneath it.

    A tool installed with `pip install -e` / `uv tool install -e` resolves its
    source through one of these every time it runs, so moving the checkout
    breaks the installed command — including `rl` itself, whose own source is a
    workspace directory a user may reasonably move.
    """
    roots = [
        Path.home() / ".local/share/uv/tools",
        Path.home() / ".local/share/pipx/venvs",
    ]
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for pth in root.glob("*/lib/*/site-packages/*.pth"):
            try:
                lines = pth.read_text().splitlines()
            except OSError:
                continue
            for line in lines:
                target = line.strip()
                if not target.startswith("/"):
                    continue
                if target == old or is_beneath(target, old):
                    found.append(pth)
                    break
    return found


# (directory, sibling file identifying the ecosystem, command that regenerates it).
# Ordered: the first entry whose directory matches and whose sibling exists wins, so
# each name ends with a `None` fallback for an unrecognised toolchain.
DERIVED_STATE = (
    (".venv", "uv.lock", "uv sync --reinstall"),
    (".venv", "pyproject.toml", "uv sync --reinstall"),
    (".venv", None, "recreate the virtualenv"),
    ("node_modules", "pnpm-lock.yaml", "pnpm install --force"),
    ("node_modules", "yarn.lock", "yarn install --force"),
    ("node_modules", None, "npm ci"),
    ("target", "Cargo.toml", "cargo clean"),
    (".build", "Package.swift", "swift build"),
)

# never descended into: the artifacts themselves are large, and .git is irrelevant here
_NO_DESCENT = {".git", ".venv", "node_modules", "target", ".build"}


def _search_roots(root: Path, max_depth: int):
    """`root` and its subdirectories down to `max_depth`, skipping the artifact
    directories themselves — a monorepo keeps its package builds one or two levels
    down, and walking the whole tree to find them is not worth the stat calls."""
    level = [root]
    for _ in range(max_depth + 1):
        yield from level
        nxt = []
        for d in level:
            try:
                entries = list(d.iterdir())
            except OSError:
                continue
            nxt.extend(
                c
                for c in entries
                if c.is_dir() and not c.is_symlink() and c.name not in _NO_DESCENT
            )
        level = nxt


def derived_state(root: str, *, max_depth: int = 2) -> list[tuple[Path, str]]:
    """Build artifacts under `root` that bake in an absolute path, each with the
    command that regenerates it. Reported to the user, never rewritten — see the
    module docstring for why this side of the line is not ours to repair."""
    found = []
    for parent in _search_roots(Path(root), max_depth):
        matched: set[str] = set()
        for name, sibling, command in DERIVED_STATE:
            path = parent / name
            if name in matched or not path.is_dir():
                continue
            if sibling is not None and not (parent / sibling).exists():
                continue
            matched.add(name)
            found.append((path, command))
    return found


def repoint_editables(pths: list[Path], old: str, new: str) -> list[Path]:
    """Rewrite each `.pth` onto the new location. Returns the ones that failed,
    which the caller must report: a silently unrepaired `.pth` leaves a working
    directory and a broken command, and the breakage surfaces later somewhere
    unrelated."""
    failed = []
    for pth in pths:
        try:
            lines = pth.read_text().splitlines()
            out = [
                new + line.strip()[len(old) :]
                if line.strip() == old or is_beneath(line.strip(), old)
                else line
                for line in lines
            ]
            pth.write_text("\n".join(out) + "\n")
        except OSError:
            failed.append(pth)
    return failed


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
    for thread, _source, _new_project in plan.rebind:
        print(f"  session  {thread['sessionId'][:8]}  rebound in place ({thread.get('source')})")
    for thread in plan.unsupported:
        print(f"  session  {thread['sessionId'][:8]}  registry only ({thread.get('source')})")
    print(f"{len(plan.threads)} session(s), {len(plan.arch)} archived")
    if move_files:
        for pth in editable_installs(plan.old):
            print(f"  editable  {pth.name} will be repointed at the new location")
    tree = plan.old if move_files else plan.new
    for path, command in derived_state(tree):
        rel = path.relative_to(tree)
        print(f"  derived   {rel} keeps the old path — regenerate it with `{command}`")


def _relocate_storage(plan: _Plan, srcs: _Sources, verb: str) -> tuple[dict, set[str]]:
    """Returns the old->new transcript paths, and the session ids whose storage did
    NOT move. A failed session must not be pinned: pinning rewrites its `project`, and
    `retarget` finds threads by the old path — so pinning a stranded session puts it
    beyond the reach of the one command that could repair it."""
    moved: dict[str, str] = {}
    stranded: set[str] = set()
    for source, old_cwd, new_cwd in plan.whole:
        try:
            moved.update(srcs.relocating[source].relocate_storage(old_cwd, new_cwd))
        except (RuntimeError, OSError) as e:
            print(f"rl-workspace-{verb}: {old_cwd}: {e}", file=sys.stderr)
            for thread, _ in plan.threads:
                if thread.get("project") == old_cwd:
                    stranded.add(thread["sessionId"])
    for thread, source, new_project in plan.per:
        try:
            moved[thread["file"]] = srcs.relocating[source].relocate_session(
                thread["file"], new_project
            )
        except (RuntimeError, OSError) as e:
            print(f"rl-workspace-{verb}: {thread['sessionId'][:8]}: {e}", file=sys.stderr)
            stranded.add(thread["sessionId"])
    for thread, source, new_project in plan.rebind:
        sid = thread["sessionId"]
        try:
            outcome = srcs.rebinding[source].rebind_workspace(sid, thread["file"], new_project)
        except (RuntimeError, OSError) as e:
            print(f"rl-workspace-{verb}: {sid[:8]}: {e}", file=sys.stderr)
            stranded.add(sid)
            continue
        if outcome is IndexUpdate.NO_INDEX:
            # Strand rather than pin. The index being absent is usually temporary — a
            # Codex home mid-migration, a mis-set CODEX_SESSIONS_DIR — and a pinned
            # thread is one `retarget` can never find again.
            print(
                f"rl-workspace-{verb}: {sid[:8]}: {source} has no session index here",
                file=sys.stderr,
            )
            stranded.add(sid)
        elif outcome is IndexUpdate.NO_ENTRY:
            print(
                f"rl-workspace-{verb}: {sid[:8]}: no {source} index entry for "
                f"{thread['file']} — it stays resumable by id",
                file=sys.stderr,
            )
    _rebind_archived(plan, srcs, verb)
    return moved, stranded


def _rebind_archived(plan: _Plan, srcs: _Sources, verb: str) -> None:
    """An archived session still has an index entry, still keyed to the path the
    transcript had before archiving — so it needs the same rebind, or unarchiving it
    after a move restores a session the picker still filters to the old workspace."""
    for record, new_project in plan.arch:
        source = (record.get("thread") or {}).get("source")
        original = record.get("originalPath")
        if not original or source not in srcs.rebinding:
            continue
        sid = record["sessionId"]
        try:
            srcs.rebinding[source].rebind_workspace(sid, original, new_project)
        except (RuntimeError, OSError) as e:
            print(f"rl-workspace-{verb}: {sid[:8]} (archived): {e}", file=sys.stderr)


def remap_cached_git_root(thread: dict, old: str, new: str) -> None:
    """Carry a cached `gitRoot` across with the tree.

    `pin_thread_location` keeps the cached git fields when the destination has no
    resolvable git identity — a deleted worktree, say. That is right for the
    identity itself, but the cached root is a path, and left alone it names a
    location that no longer exists. Remapping keeps the identity and makes the
    path true.
    """
    root = thread.get("gitRoot")
    if isinstance(root, str) and (root == old or is_beneath(root, old)):
        thread["gitRoot"] = new + root[len(old) :]


def _retarget_registry(
    registry_path: str,
    old: str,
    new: str,
    moved: dict,
    srcs: _Sources,
    stranded: set[str],
) -> _Plan:
    with publish_lock():
        reg = registry.load_registry(registry_path)
        plan = build_plan(reg, old, new, srcs)
        plan.threads = [t for t in plan.threads if t[0]["sessionId"] not in stranded]
        for thread, new_project in plan.threads:
            registry.pin_thread_location(thread, new_project, _git_for(new_project))
            remap_cached_git_root(thread, old, new)
            if thread.get("file") in moved:
                thread["file"] = moved[thread["file"]]
        for record, new_project in plan.arch:
            embedded = record["thread"]
            was = embedded["project"]
            registry.pin_thread_location(embedded, new_project, _git_for(new_project))
            remap_cached_git_root(embedded, old, new)
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

    srcs = _resolve_sources()
    plan = build_plan(registry.load_registry(args.registry), old, new, srcs)
    empty = _refuse_empty(plan, old, verb, move_files=move_files, allow_empty=args.allow_empty)
    if empty is not None:
        return empty

    occupied = collisions(plan, srcs)
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

    if move_files:
        failed = repoint_editables(editable_installs(old), old, new)
        for pth in failed:
            print(
                f"rl-workspace-{verb}: could not repoint {pth} — the command it "
                f"installs will fail until you run:\n"
                f'  echo "{new}" > {pth}',
                file=sys.stderr,
            )

    moved, stranded = _relocate_storage(plan, srcs, verb)
    applied = _retarget_registry(args.registry, old, new, moved, srcs, stranded)
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
