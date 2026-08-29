from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

from ..paths import bundled_skills_dir, exists_or_report

PREFIX = "ringleader-"


def _bundled_skills() -> list[Path]:
    root = bundled_skills_dir()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith(PREFIX))


def _install_dirs(home: Path) -> list[tuple[str, Path]]:
    """(agent label, skills dir) for each detected agent."""
    out: list[tuple[str, Path]] = []
    if exists_or_report(home / ".claude"):
        out.append(("claude", home / ".claude" / "skills"))
    shared = home / ".agents" / "skills"
    for label, probe in (("codex", ".codex"), ("opencode", ".config/opencode"), ("pi", ".pi")):
        if exists_or_report(home / probe):
            out.append((label, shared))
    return out


def _unique_dirs(targets: list[tuple[str, Path]]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for _, d in targets:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _resolved(d: Path) -> str:
    """Show a target dir, plus its real path when it (or a parent) is a symlink — so
    installing through e.g. a symlinked ``~/.claude/skills`` is never a surprise."""
    try:
        real = d.resolve()
    except OSError:
        return f"{d} (unreadable)"
    return f"{d} -> {real}" if real != d else f"{d}"


def _link_state(link: Path, target: Path) -> str:
    """``linked`` / ``broken/wrong`` / ``missing`` / ``unreadable`` for one candidate
    skill symlink. A stat failure (e.g. a sandbox that grants the containing dir but
    denies this specific entry) must read as ``unreadable``, not silently as
    ``missing`` — those mean different things to someone debugging their setup."""
    try:
        mode = os.lstat(link).st_mode
    except FileNotFoundError:
        return "missing"
    except OSError as e:
        return f"unreadable ({e.strerror or e})"
    if not stat.S_ISLNK(mode):
        return "missing"
    try:
        return "linked" if link.resolve() == target.resolve() else "broken/wrong"
    except OSError as e:
        return f"unreadable ({e.strerror or e})"


def _owned(link: Path) -> bool:
    """A symlink this tool manages: name starts with ``ringleader-`` and its target
    (even if the target no longer exists) lies inside the bundled skills dir."""
    if not link.name.startswith(PREFIX):
        return False
    try:
        if not link.is_symlink():
            return False
        target = link.resolve()
    except (OSError, RuntimeError):  # e.g. a symlink loop, or the entry is unreadable
        return False
    return target.is_relative_to(bundled_skills_dir().resolve())


def _confirm(targets: list[tuple[str, Path]], skills: list[Path], yes: bool) -> bool:
    print("Will link these skills:")
    for label, d in targets:
        print(f"  {label}: {_resolved(d)}")
    for s in skills:
        print(f"  - {s.name}")
    if yes:
        return True
    return input("Proceed? [y/N] ").strip().lower() in ("y", "yes")


def cmd_install(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl install skills")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args(argv)
    home = Path.home()
    bundled = _bundled_skills()
    if not bundled:
        print("no bundled ringleader skills found", file=sys.stderr)
        return 1
    targets = _install_dirs(home)
    if not targets:
        print("no supported agents detected (~/.claude, ~/.codex, ~/.config/opencode, ~/.pi)")
        return 0
    if not _confirm(targets, bundled, args.yes):
        print("aborted")
        return 0
    created = 0
    failed = 0
    unreadable = 0
    for d in _unique_dirs(targets):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # the directory itself is inaccessible (e.g. a sandbox hiding part of
            # $HOME). Report it, but keep it out of `failed`: nonzero is reserved for
            # a link that should have been created and wasn't, so an unattended
            # `rl update all --yes` isn't failed by an environment condition.
            print(f"  {d}: unreadable ({e.strerror or e})", file=sys.stderr)
            unreadable += 1
            continue
        for s in bundled:
            link = d / s.name
            try:
                if link.is_symlink():
                    if link.resolve() == s.resolve():
                        continue
                    link.unlink()
                elif link.exists():
                    print(f"skip {link}: a non-symlink already exists there", file=sys.stderr)
                    continue
                link.symlink_to(s)
                created += 1
            except OSError as e:
                print(f"could not link {link}: {e}", file=sys.stderr)
                failed += 1
    print(
        f"linked {created} skill(s)"
        + (f", {failed} failed" if failed else "")
        + (f", {unreadable} dir(s) unreadable" if unreadable else "")
    )
    return 1 if failed else 0


def _managed_links(d: Path) -> list[Path] | None:
    """Owned symlinks inside ``d``, or ``None`` if ``d`` itself could not be read.

    A directory that denies access must be reported as unreadable, not silently
    treated as empty — an empty list there would read as "nothing installed", which is
    a different (and possibly false) claim. Stats explicitly for the reason given in
    `paths.exists_or_report`; ``iterdir()`` can fail separately, so both stay guarded."""
    try:
        if not stat.S_ISDIR(os.stat(d).st_mode):
            return []
        entries = sorted(d.iterdir())
    except FileNotFoundError:
        return []
    except OSError as e:
        print(f"  {d}: unreadable ({e.strerror or e})", file=sys.stderr)
        return None
    return [link for link in entries if _owned(link)]


def cmd_uninstall(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl uninstall skills")
    ap.add_argument("--yes", "-y", action="store_true")
    args = ap.parse_args(argv)
    home = Path.home()
    dirs = _unique_dirs(_install_dirs(home))
    per_dir = [_managed_links(d) for d in dirs]
    unreadable = any(r is None for r in per_dir)
    victims = [link for r in per_dir if r is not None for link in r]
    if not victims:
        # an unreadable directory is an environment condition, not a failure of the
        # remove the caller asked for — report it above, but don't fail the run over it
        print("nothing readable to remove" if unreadable else "nothing installed")
        return 0
    print("Will remove:")
    for v in victims:
        print(f"  {v}")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted")
        return 0
    removed = 0
    failed = 0
    for v in victims:
        try:
            v.unlink()
            removed += 1
        except OSError as e:
            print(f"could not remove {v}: {e}", file=sys.stderr)
            failed += 1
    print(f"removed {removed} link(s)" + (f", {failed} failed" if failed else ""))
    return 1 if failed else 0


def cmd_update(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl update skills")
    ap.add_argument("--yes", "-y", action="store_true")
    args = ap.parse_args(argv)
    home = Path.home()
    bundled = {s.name for s in _bundled_skills()}
    dirs = _unique_dirs(_install_dirs(home))
    # update syncs what's already wired up; it never opts an agent in for the first
    # time, so it's safe to run unattended (e.g. from an install recipe).
    per_dir = [_managed_links(d) for d in dirs]
    unreadable = any(r is None for r in per_dir)
    linked = [link for r in per_dir if r is not None for link in r]
    if not linked:
        # an unreadable directory is an environment condition, not a failure of the
        # sync the caller asked for — report it above, but don't fail the run over it
        if unreadable:
            print("some skills directories could not be read; nothing else to sync")
        else:
            print("no ringleader skills installed — run `rl install skills` to wire them up")
        return 0
    stale = [link for link in linked if link.name not in bundled]
    stale_failed = 0
    if stale:
        print("Will remove stale links:")
        for v in stale:
            print(f"  {v}")
        if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return 0
        for v in stale:
            try:
                v.unlink()
            except OSError as e:
                print(f"could not remove {v}: {e}", file=sys.stderr)
                stale_failed += 1
    # add/repair the current set (reuse install's linking with the same confirm)
    rc = cmd_install(["--yes"] if args.yes else [])
    return 1 if (stale_failed or rc) else 0


def cmd_status(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="rl doctor").parse_args(argv)
    home = Path.home()
    bundled = _bundled_skills()
    targets = _install_dirs(home)
    if not targets:
        print("no supported agents detected")
        return 0
    for d in _unique_dirs(targets):
        print(f"{_resolved(d)}:")
        for s in bundled:
            print(f"  {s.name}: {_link_state(d / s.name, s)}")
    return 0
