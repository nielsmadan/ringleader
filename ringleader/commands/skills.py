from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..paths import bundled_skills_dir

PREFIX = "ringleader-"


def _bundled_skills() -> list[Path]:
    root = bundled_skills_dir()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith(PREFIX))


def _install_dirs(home: Path) -> list[tuple[str, Path]]:
    """(agent label, skills dir) for each detected agent."""
    out: list[tuple[str, Path]] = []
    if (home / ".claude").exists():
        out.append(("claude", home / ".claude" / "skills"))
    shared = home / ".agents" / "skills"
    for label, probe in (("codex", ".codex"), ("opencode", ".config/opencode"), ("pi", ".pi")):
        if (home / probe).exists():
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
    real = d.resolve()
    return f"{d} -> {real}" if real != d else f"{d}"


def _owned(link: Path) -> bool:
    """A symlink this tool manages: name starts with ``ringleader-`` and its target
    (even if the target no longer exists) lies inside the bundled skills dir."""
    if not link.name.startswith(PREFIX) or not link.is_symlink():
        return False
    try:
        target = link.resolve()
    except (OSError, RuntimeError):  # e.g. a symlink loop
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
    ap = argparse.ArgumentParser(prog="rl skills install")
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
    for d in _unique_dirs(targets):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"skip {d}: {e}", file=sys.stderr)
            failed += 1
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
    print(f"linked {created} skill(s)" + (f", {failed} failed" if failed else ""))
    return 1 if failed else 0


def _managed_links(d: Path):
    if not d.is_dir():
        return
    for link in sorted(d.iterdir()):
        if _owned(link):
            yield link


def cmd_uninstall(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl skills uninstall")
    ap.add_argument("--yes", "-y", action="store_true")
    args = ap.parse_args(argv)
    home = Path.home()
    dirs = _unique_dirs(_install_dirs(home))
    victims = [link for d in dirs for link in _managed_links(d)]
    if not victims:
        print("nothing installed")
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
    ap = argparse.ArgumentParser(prog="rl skills update")
    ap.add_argument("--yes", "-y", action="store_true")
    args = ap.parse_args(argv)
    home = Path.home()
    bundled = {s.name for s in _bundled_skills()}
    dirs = _unique_dirs(_install_dirs(home))
    stale = [link for d in dirs for link in _managed_links(d) if link.name not in bundled]
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
    argparse.ArgumentParser(prog="rl skills status").parse_args(argv)
    home = Path.home()
    bundled = _bundled_skills()
    targets = _install_dirs(home)
    if not targets:
        print("no supported agents detected")
        return 0
    for d in _unique_dirs(targets):
        print(f"{_resolved(d)}:")
        for s in bundled:
            link = d / s.name
            if link.is_symlink() and link.resolve() == s.resolve():
                state = "linked"
            elif link.is_symlink():
                state = "broken/wrong"
            else:
                state = "missing"
            print(f"  {s.name}: {state}")
    return 0
