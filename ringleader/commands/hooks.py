"""`rl install/update/uninstall/status hooks` — register ringleader's session-end hook
into each detected agent's own configuration.

Three formats: Claude Code, Codex, Droid and Cursor keep JSON hook files
(read/patch/write) — a list of `SessionEnd` matcher groups under `hooks`, except that
Droid's standalone `hooks.json` is keyed by event name directly and Cursor lists bare
entries under `hooks.sessionEnd`; Pi discovers extensions by symlink, same as `skills.py`
links skill directories; OpenCode has no session-end event at all and is only ever
reported on, never written to.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NamedTuple

from .. import safe_write
from ..paths import (
    cursor_cli_installed,
    exists_or_report,
    factory_dir,
    factory_label,
)

HOOK_EVENT = "SessionEnd"


class JsonAgent(NamedTuple):
    label: str
    path: Path
    flat: bool
    timeout: int
    prune_when_empty: bool = False
    event: str = HOOK_EVENT
    grouped: bool = True
    version: int | None = None


def bundled_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "hooks"


def _session_end_script() -> Path:
    return bundled_dir() / "session_end.sh"


def _pi_extension() -> Path:
    return bundled_dir() / "pi-ringleader-finish.ts"


def _droid_agent(factory: Path) -> JsonAgent:
    """Droid reads `hooks.json` *or* the legacy `hooks/hooks.json`, never both, so
    creating the former would silently disable everything in the latter — and an empty
    one shadows it just as well. `prune_when_empty` therefore only applies where there
    is no legacy file to un-shadow: deleting an emptied `hooks.json` beside one would
    re-activate hooks the user had shadowed, whoever created the file."""
    standalone = factory / "hooks.json"
    legacy = factory / "hooks" / "hooks.json"
    if not exists_or_report(standalone) and exists_or_report(legacy):
        return JsonAgent("droid", legacy, flat=True, timeout=5)
    return JsonAgent(
        "droid", standalone, flat=True, timeout=5, prune_when_empty=not exists_or_report(legacy)
    )


def _json_agents(home: Path) -> list[JsonAgent]:
    """One entry per detected JSON-hook agent: where its hooks file lives and how that
    file is shaped."""
    out: list[JsonAgent] = []
    if exists_or_report(home / ".claude"):
        out.append(JsonAgent("claude", home / ".claude" / "settings.json", flat=False, timeout=5))
    if exists_or_report(home / ".codex"):
        out.append(JsonAgent("codex", home / ".codex" / "hooks.json", flat=False, timeout=3))
    factory = factory_dir()
    if exists_or_report(factory):
        out.append(_droid_agent(factory))
    if cursor_cli_installed(home):
        # Cursor reads hooks from $HOME/.cursor, never from CURSOR_CONFIG_DIR
        hooks = home / ".cursor" / "hooks.json"
        out.append(
            JsonAgent(
                "cursor",
                hooks,
                flat=False,
                timeout=5,
                event="sessionEnd",
                grouped=False,
                version=1,
            )
        )
    return out


def _pi_dir(home: Path) -> Path | None:
    if exists_or_report(home / ".pi"):
        return home / ".pi" / "agent" / "extensions"
    return None


def _opencode_detected(home: Path) -> bool:
    return exists_or_report(home / ".config" / "opencode")


def _is_ours(command: object) -> bool:
    """An entry is ours only if its command path lies inside `bundled_dir()` — this is
    what `uninstall` matches on, so a user's own session-end hook is never touched."""
    if not isinstance(command, str) or not command:
        return False
    try:
        return Path(command).resolve().is_relative_to(bundled_dir())
    except OSError:
        return False


def _load_json_raw(path: Path) -> tuple[str, dict[str, Any]] | None:
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


def _load_json_or_none(path: Path) -> dict[str, Any] | None:
    loaded = _load_json_raw(path)
    return None if loaded is None else loaded[1]


def _event_container(data: dict[str, Any], agent: JsonAgent) -> dict[str, Any] | None:
    """The object that holds the event lists: the file's root when it is keyed by event
    name, else its `hooks` value."""
    if agent.flat:
        return data
    hooks = data.get("hooks")
    return hooks if isinstance(hooks, dict) else None


def _hook_entries(events: list[Any], agent: JsonAgent) -> list[dict[str, Any]]:
    """Every hook entry under one event: inside its matcher groups, or the event's
    list itself for an agent that keeps no groups."""
    if not agent.grouped:
        return [h for h in events if isinstance(h, dict)]
    return [
        h
        for group in events
        if isinstance(group, dict) and isinstance(group.get("hooks"), list)
        for h in group["hooks"]
        if isinstance(h, dict)
    ]


def _has_our_entry(data: dict[str, Any], agent: JsonAgent) -> bool:
    container = _event_container(data, agent)
    if container is None:
        return False
    events = container.get(agent.event)
    if not isinstance(events, list):
        return False
    return any(_is_ours(h.get("command")) for h in _hook_entries(events, agent))


def _with_our_entry(data: dict[str, Any], timeout_seconds: int, agent: JsonAgent) -> dict[str, Any]:
    """`data`, with our session-end entry appended (and the agent's `version` key set if
    missing), or its timeout refreshed if already present. Mutates and returns `data`
    in place; raises `ValueError` (caller skips the file) if the event
    key or its container already exist with the wrong shape."""
    if agent.flat:
        container = data
    else:
        container = data.setdefault("hooks", {})
        if not isinstance(container, dict):
            raise ValueError("'hooks' is not an object")
    events = container.setdefault(agent.event, [])
    if not isinstance(events, list):
        key = agent.event if agent.flat else f"hooks.{agent.event}"
        raise ValueError(f"'{key}' is not an array")
    ours = [h for h in _hook_entries(events, agent) if _is_ours(h.get("command"))]
    for h in ours:
        h["timeout"] = timeout_seconds
    if ours:
        return data
    if agent.version is not None:
        data.setdefault("version", agent.version)
    entry = {
        "type": "command",
        "command": str(_session_end_script()),
        "timeout": timeout_seconds,
    }
    events.append({"hooks": [entry]} if agent.grouped else entry)
    return data


def _kept_events(events: list[Any], agent: JsonAgent) -> tuple[list[Any], int]:
    def ours(h: object) -> bool:
        return isinstance(h, dict) and _is_ours(h.get("command"))

    if not agent.grouped:
        kept = [h for h in events if not ours(h)]
        return kept, len(events) - len(kept)
    removed = 0
    new_events = []
    for group in events:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            new_events.append(group)
            continue
        hooks = [h for h in group["hooks"] if not ours(h)]
        removed += len(group["hooks"]) - len(hooks)
        if hooks:
            new_events.append({**group, "hooks": hooks})
    return new_events, removed


def _without_our_entries(data: dict[str, Any], agent: JsonAgent) -> tuple[dict[str, Any], int]:
    """A copy of `data` with only our session-end entries removed; a foreign entry in
    the same or another group survives untouched. Returns (new_data, removed_count)."""
    container = _event_container(data, agent)
    if container is None:
        return data, 0
    events = container.get(agent.event)
    if not isinstance(events, list):
        return data, 0
    new_events, removed = _kept_events(events, agent)
    new_container = dict(container)
    if new_events:
        new_container[agent.event] = new_events
    else:
        new_container.pop(agent.event, None)
    if agent.flat:
        return new_container, removed
    new_data = dict(data)
    if new_container:
        new_data["hooks"] = new_container
    else:
        new_data.pop("hooks", None)
    return new_data, removed


def _planned_text(data: dict[str, Any], timeout_seconds: int, agent: JsonAgent) -> str | None:
    """The full text to write once our entry is in `data`, or None if it is already
    there. Raises `ValueError` (caller skips the file) on a wrong-shaped event key."""
    before = json.dumps(data, indent=2)
    after = json.dumps(_with_our_entry(data, timeout_seconds, agent), indent=2)
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


def _remove_json(path: Path) -> tuple[int, int]:
    """Delete a hooks file that held nothing but our entry, backing up nothing: the
    diff above showed all of it. Returns (changed, failed)."""
    try:
        path.unlink()
    except OSError as e:
        print(f"could not remove {path}: {e}", file=sys.stderr)
        return 0, 1
    print(f"  removed {path}")
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
    candidates: list[JsonAgent], *, opt_in: bool
) -> tuple[list[tuple[str, Path, str, str | None]], int]:
    """(label, path, current text, text to write or None if already installed) per
    writable target, plus a count of targets skipped as malformed."""
    plan: list[tuple[str, Path, str, str | None]] = []
    failed = 0
    for agent in candidates:
        loaded = _load_json_raw(agent.path)
        if loaded is None:
            continue
        raw, data = loaded
        if not (opt_in or _has_our_entry(data, agent)):
            continue
        try:
            text = _planned_text(data, agent.timeout, agent)
        except ValueError as e:
            print(f"{agent.path}: {e}; leaving untouched", file=sys.stderr)
            failed += 1
            continue
        plan.append((agent.label, agent.path, raw, text))
    return plan, failed


def _warn_if_droid_settings_shadowed(plan: list[tuple[str, Path, str, str | None]]) -> None:
    """Droid merges its hooks file over `settings.json` per event, hooks file winning,
    so our SessionEnd replaces one declared there rather than joining it."""
    if not any(label == "droid" and text is not None for label, _p, _r, text in plan):
        return
    settings = factory_dir() / "settings.json"
    loaded = _load_json_raw(settings)
    if loaded is None:
        return
    declared = loaded[1].get("hooks")
    if isinstance(declared, dict) and declared.get(HOOK_EVENT):
        print(
            f"warning: {settings} declares a {HOOK_EVENT} hook that droid will ignore "
            "while ours is installed",
            file=sys.stderr,
        )


def _sync(home: Path, yes: bool, *, opt_in: bool) -> int:
    """Shared body for `install` (opt_in=True: wires up every detected agent) and
    `update` (opt_in=False: re-syncs only agents that already carry our entry, and
    never opts a new one in — the contract `just install`'s unattended
    `rl update all --yes` depends on)."""
    candidates = _json_agents(home)
    pi_dir = _pi_dir(home)
    if not candidates and pi_dir is None:
        print(
            f"no supported agents detected (~/.claude, ~/.codex, {factory_label()}, ~/.pi,"
            " ~/.cursor)"
        )
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
    _warn_if_droid_settings_shadowed(json_plan)
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
    for agent in _json_agents(home):
        loaded = _load_json_raw(agent.path)
        if loaded is None or not _has_our_entry(loaded[1], agent):
            continue
        raw, data = loaded
        new_data, removed = _without_our_entries(data, agent)
        if not removed:
            continue
        gone = agent.prune_when_empty and not new_data
        text = "" if gone else json.dumps(new_data, indent=2) + "\n"
        json_victims.append((agent.label, agent.path, raw, text))

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
        print(f"  {label}: {path}" + ("" if text else " (deleting the file)"))
        print(safe_write.diff(path, raw, text), end="")
    if pi_link is not None:
        print(f"  pi: {pi_link}")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted")
        return 0

    failed = 0
    for _label, path, _raw, text in json_victims:
        _c, f = _write_json(path, text) if text else _remove_json(path)
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

    for agent in _json_agents(home):
        any_agent = True
        print(f"{agent.label}: {agent.path}")
        data = _load_json_or_none(agent.path)
        if data is None:
            status = "could not read/parse (see above)"
        elif _has_our_entry(data, agent):
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
