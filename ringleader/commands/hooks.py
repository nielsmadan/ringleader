"""`rl install/update/uninstall/status hooks` — register ringleader's session-end hook
into each detected agent's own configuration.

Three formats: Claude Code and Codex both keep a JSON `hooks.SessionEnd` list of
groups (read/patch/write); Pi discovers extensions by symlink, same as `skills.py`
links skill directories; OpenCode has no session-end event at all and is only ever
reported on, never written to.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import safe_write
from ..paths import exists_or_report

HOOK_EVENT = "SessionEnd"
TIMEOUT_SECONDS = {"claude": 5, "codex": 3}


def bundled_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "hooks"


def _session_end_script() -> Path:
    return bundled_dir() / "session_end.sh"


def _pi_extension() -> Path:
    return bundled_dir() / "pi-ringleader-finish.ts"


def _json_agents(home: Path) -> list[tuple[str, Path]]:
    """(agent label, hooks-config path) for each detected JSON-hook agent."""
    out: list[tuple[str, Path]] = []
    if exists_or_report(home / ".claude"):
        out.append(("claude", home / ".claude" / "settings.json"))
    if exists_or_report(home / ".codex"):
        out.append(("codex", home / ".codex" / "hooks.json"))
    return out


def _pi_dir(home: Path) -> Path | None:
    if exists_or_report(home / ".pi"):
        return home / ".pi" / "agent" / "extensions"
    return None


def _opencode_detected(home: Path) -> bool:
    return exists_or_report(home / ".config" / "opencode")


def _is_ours(command: object) -> bool:
    """An entry is ours only if its command path lies inside `bundled_dir()` — this is
    what `uninstall` matches on, so a user's own SessionEnd hook is never touched."""
    if not isinstance(command, str) or not command:
        return False
    try:
        return Path(command).resolve().is_relative_to(bundled_dir())
    except OSError:
        return False


def _load_json_raw(path: Path) -> tuple[str, dict] | None:
    """The file's own text plus the JSON object it holds — `("", {})` if the file
    doesn't exist yet, or `None` if it exists but can't be read or parsed as a JSON
    object, the signal to leave it untouched rather than risk clobbering something we
    can't understand. The raw text is what the diff is shown against, so a re-indent
    of the whole file is visible rather than silent.

    Deliberately does not pre-check with `exists_or_report()`/`.exists()`: a sandbox can grant
    read and write on a path separately, so "cannot stat" and "does not exist" are not
    the same fact. Only `FileNotFoundError` from the actual read means "absent" — any
    other `OSError` (permission denied, etc.) must fall through to `None`, never `{}`,
    or a config we can't read would get silently replaced with a fresh one instead of
    left alone."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "", {}
    except OSError as e:
        print(f"{path}: unreadable ({e.strerror or e})", file=sys.stderr)
        return None
    if not raw.strip():
        return raw, {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"{path}: invalid JSON ({e}); leaving untouched", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"{path}: top-level JSON is not an object; leaving untouched", file=sys.stderr)
        return None
    return raw, data


def _load_json_or_none(path: Path) -> dict | None:
    loaded = _load_json_raw(path)
    return None if loaded is None else loaded[1]


def _has_our_entry(data: dict) -> bool:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    events = hooks.get(HOOK_EVENT)
    if not isinstance(events, list):
        return False
    for group in events:
        if not isinstance(group, dict):
            continue
        for h in group.get("hooks", []):
            if isinstance(h, dict) and _is_ours(h.get("command")):
                return True
    return False


def _with_our_entry(data: dict, timeout_seconds: int) -> dict:
    """`data`, with our SessionEnd group appended if not already present. Mutates and
    returns `data` in place; raises `ValueError` (caller skips the file) if `hooks` or
    `hooks.SessionEnd` already exist with the wrong shape."""
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("'hooks' is not an object")
    events = hooks.setdefault(HOOK_EVENT, [])
    if not isinstance(events, list):
        raise ValueError(f"'hooks.{HOOK_EVENT}' is not an array")
    found = False
    for group in events:
        if not isinstance(group, dict):
            continue
        for h in group.get("hooks", []):
            if isinstance(h, dict) and _is_ours(h.get("command")):
                h["timeout"] = timeout_seconds
                found = True
    if found:
        return data
    events.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": str(_session_end_script()),
                    "timeout": timeout_seconds,
                }
            ]
        }
    )
    return data


def _without_our_entries(data: dict) -> tuple[dict, int]:
    """A copy of `data` with only our SessionEnd entries removed; a foreign entry in
    the same or another group survives untouched. Returns (new_data, removed_count)."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return data, 0
    events = hooks.get(HOOK_EVENT)
    if not isinstance(events, list):
        return data, 0
    removed = 0
    new_events = []
    for group in events:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            new_events.append(group)
            continue
        kept = [
            h for h in group["hooks"] if not (isinstance(h, dict) and _is_ours(h.get("command")))
        ]
        removed += len(group["hooks"]) - len(kept)
        if kept:
            new_events.append({**group, "hooks": kept})
    new_hooks = dict(hooks)
    if new_events:
        new_hooks[HOOK_EVENT] = new_events
    else:
        new_hooks.pop(HOOK_EVENT, None)
    new_data = dict(data)
    if new_hooks:
        new_data["hooks"] = new_hooks
    else:
        new_data.pop("hooks", None)
    return new_data, removed


def _planned_text(data: dict, timeout_seconds: int) -> str | None:
    """The full text to write once our entry is in `data`, or None if it is already
    there. Raises `ValueError` (caller skips the file) on a wrong-shaped `hooks` key."""
    before = json.dumps(data, indent=2)
    after = json.dumps(_with_our_entry(data, timeout_seconds), indent=2)
    return None if after == before else after + "\n"


def _write_json(path: Path, text: str) -> tuple[int, int]:
    """Write `text` to `path`, backed up and atomically. Returns (changed, failed)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # the directory itself is inaccessible (e.g. a sandbox hiding part of $HOME).
        # Report it, but keep it out of `failed` — same convention as `_sync_pi` and
        # `skills.py`: nonzero is reserved for a write that should have succeeded and
        # didn't, so an unattended `rl update all --yes` isn't failed by an
        # environment condition.
        print(f"  {path.parent}: unreadable ({e.strerror or e})", file=sys.stderr)
        return 0, 0
    try:
        backup = safe_write.backup_and_write(path, text)
    except OSError as e:
        print(f"could not write {path}: {e}", file=sys.stderr)
        return 0, 1
    print(f"  wrote {path}" + (f" (backup: {backup})" if backup else ""))
    return 1, 0


def _already_installed_pi(link: Path) -> bool:
    try:
        return link.is_symlink() and link.resolve() == _pi_extension().resolve()
    except OSError:
        return False


def _pi_link_state(link: Path) -> str:
    try:
        if link.is_symlink():
            return "linked" if link.resolve() == _pi_extension().resolve() else "broken/wrong"
        return "exists (not a symlink)" if link.exists() else "missing"
    except OSError as e:
        return f"unreadable ({e.strerror or e})"


def _sync_pi(pi_dir: Path) -> tuple[int, int]:
    """Symlink the Pi extension into `pi_dir`, exactly as `skills.py` links skill
    directories: a no-op if already linked to us, skipped (with a message) if a
    non-symlink already sits there. Returns (changed, failed)."""
    target = _pi_extension()
    try:
        pi_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"  {pi_dir}: unreadable ({e.strerror or e})", file=sys.stderr)
        return 0, 0
    link = pi_dir / target.name
    try:
        if link.is_symlink():
            if link.resolve() == target.resolve():
                return 0, 0
            link.unlink()
        elif link.exists():
            print(f"skip {link}: a non-symlink already exists there", file=sys.stderr)
            return 0, 0
        link.symlink_to(target)
        return 1, 0
    except OSError as e:
        print(f"could not link {link}: {e}", file=sys.stderr)
        return 0, 1


def _json_plan(
    candidates: list[tuple[str, Path]], *, opt_in: bool
) -> tuple[list[tuple[str, Path, str, str | None]], int]:
    """(label, path, current text, text to write or None if already installed) per
    writable target, plus a count of targets skipped as malformed."""
    plan: list[tuple[str, Path, str, str | None]] = []
    failed = 0
    for label, path in candidates:
        loaded = _load_json_raw(path)
        if loaded is None:
            continue
        raw, data = loaded
        if not (opt_in or _has_our_entry(data)):
            continue
        try:
            plan.append((label, path, raw, _planned_text(data, TIMEOUT_SECONDS[label])))
        except ValueError as e:
            print(f"{path}: {e}; leaving untouched", file=sys.stderr)
            failed += 1
    return plan, failed


def _sync(home: Path, yes: bool, *, opt_in: bool) -> int:
    """Shared body for `install` (opt_in=True: wires up every detected agent) and
    `update` (opt_in=False: re-syncs only agents that already carry our entry, and
    never opts a new one in — the contract `just install-local`'s unattended
    `rl update all --yes` depends on)."""
    candidates = _json_agents(home)
    pi_dir = _pi_dir(home)
    if not candidates and pi_dir is None:
        print("no supported agents detected (~/.claude, ~/.codex, ~/.pi)")
        return 0

    json_plan, failed = _json_plan(candidates, opt_in=opt_in)

    pi_target = None
    if pi_dir is not None:
        link = pi_dir / _pi_extension().name
        if opt_in or _already_installed_pi(link):
            pi_target = pi_dir

    if not json_plan and pi_target is None:
        print("nothing to do" if opt_in else "no agents currently carry the ringleader hook")
        return 1 if failed else 0

    print("Will register the session-end hook for:")
    for label, path, raw, text in json_plan:
        if text is None:
            print(f"  {label}: {path} (already installed)")
            continue
        print(f"  {label}: {path}")
        print(safe_write.diff(path, raw, text), end="")
    if pi_target is not None:
        print(f"  pi: {pi_target / _pi_extension().name}")
    if not yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted")
        return 1 if failed else 0

    changed = 0
    for _label, path, _raw, text in json_plan:
        if text is None:
            continue
        c, f = _write_json(path, text)
        changed += c
        failed += f
    if pi_target is not None:
        c, f = _sync_pi(pi_target)
        changed += c
        failed += f

    print(f"synced {changed} target(s)" + (f", {failed} failed" if failed else ""))
    return 1 if failed else 0


def cmd_install(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl install hooks")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args(argv)
    return _sync(Path.home(), args.yes, opt_in=True)


def cmd_update(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl update hooks")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args(argv)
    return _sync(Path.home(), args.yes, opt_in=False)


def cmd_uninstall(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl uninstall hooks")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args(argv)
    home = Path.home()

    json_victims: list[tuple[str, Path, str, str]] = []
    for label, path in _json_agents(home):
        loaded = _load_json_raw(path)
        if loaded is None or not _has_our_entry(loaded[1]):
            continue
        raw, data = loaded
        new_data, removed = _without_our_entries(data)
        if removed:
            json_victims.append((label, path, raw, json.dumps(new_data, indent=2) + "\n"))

    pi_dir = _pi_dir(home)
    pi_link = None
    if pi_dir is not None:
        candidate = pi_dir / _pi_extension().name
        if _already_installed_pi(candidate):
            pi_link = candidate

    if not json_victims and pi_link is None:
        print("nothing installed")
        return 0

    print("Will remove:")
    for label, path, raw, text in json_victims:
        print(f"  {label}: {path}")
        print(safe_write.diff(path, raw, text), end="")
    if pi_link is not None:
        print(f"  pi: {pi_link}")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted")
        return 0

    failed = 0
    for _label, path, _raw, text in json_victims:
        _c, f = _write_json(path, text)
        failed += f

    if pi_link is not None:
        try:
            pi_link.unlink()
        except OSError as e:
            print(f"could not remove {pi_link}: {e}", file=sys.stderr)
            failed += 1

    print("removed" + (f", {failed} failed" if failed else ""))
    return 1 if failed else 0


def cmd_status(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="rl doctor").parse_args(argv)
    home = Path.home()
    any_agent = False

    for label, path in _json_agents(home):
        any_agent = True
        print(f"{label}: {path}")
        data = _load_json_or_none(path)
        if data is None:
            status = "could not read/parse (see above)"
        elif _has_our_entry(data):
            status = "installed"
        else:
            status = "not installed"
        print(f"  status: {status}")

    pi_dir = _pi_dir(home)
    if pi_dir is not None:
        any_agent = True
        link = pi_dir / _pi_extension().name
        print(f"pi: {link}")
        print(f"  status: {_pi_link_state(link)}")

    if _opencode_detected(home):
        any_agent = True
        print("opencode: no session-end event — archiving happens at the next roundup")

    if not any_agent:
        print("no supported agents detected")
    return 0
