from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shlex
import shutil
import sys
from datetime import UTC, datetime
from json.decoder import JSONDecoder
from pathlib import Path

from ..paths import ensure_state_dir
from ..usage.claude_limits import default_capture_path, read_windows

MARKER = "# ringleader-capture"

SNIPPET = (
    "printf '%s' \"$input\" | jq -c '{captured_at: (now|floor), rate_limits}' "
    '> "$D/claude-limits.json.tmp" 2>/dev/null && '
    'mv -f "$D/claude-limits.json.tmp" "$D/claude-limits.json";'
)

_STDIN_CAPTURE_RE = re.compile(r'\binput\s*=\s*"?\$\(\s*cat\s*\)"?')
_WS_RE = re.compile(r"[ \t\n\r]*")


def _default_settings_path() -> str:
    return os.environ.get("RINGLEADER_CLAUDE_SETTINGS") or str(
        Path.home() / ".claude" / "settings.json"
    )


def _add_settings_arg(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--settings", default=_default_settings_path())


def _read(path: Path) -> tuple[str, dict]:
    raw = path.read_text(encoding="utf-8")
    return raw, json.loads(raw)


def _command_of(data: dict) -> object:
    status_line = data.get("statusLine")
    if not isinstance(status_line, dict):
        return None
    return status_line.get("command")


def _script_target(command: str) -> Path | None:
    """The script file ``command`` invokes, if it names one that exists locally.

    Takes the first shell word (the program itself, ignoring any arguments after it),
    expands ``~``, and checks it against the filesystem. Not a script for a bare
    ``echo hello``-style inline command, since ``echo`` is a builtin, not a local file."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None
    candidate = Path(os.path.expanduser(tokens[0]))
    return candidate if candidate.is_file() else None


def _validated_command(path: Path, raw: str, data: dict) -> tuple[str | None, int | None]:
    """The top-level ``statusLine.command``, refusing rather than guessing when the file
    is ambiguous (a duplicate top-level ``statusLine`` key) or malformed (a non-string
    ``command``). Returns ``(command, None)`` on success — ``command`` may be falsy when
    no statusLine is configured — or ``(None, exit_code)`` to propagate straight up."""
    if _top_level_key_count(raw, "statusLine") > 1:
        print(
            f'duplicate top-level "statusLine" key in {path}; refusing to guess which is live',
            file=sys.stderr,
        )
        return None, 1
    command = _command_of(data)
    if command is not None and not isinstance(command, str):
        print(f"statusLine.command in {path} is not a string", file=sys.stderr)
        return None, 1
    return command, None


def _installed_block(state_dir: Path) -> str:
    resolved = SNIPPET.replace("$D", str(state_dir))
    return f"\n{MARKER}\n{resolved}\n{MARKER}"


def _print_snippet_help() -> None:
    print("Add this to your statusLine command, immediately after it captures stdin")
    print(f"(e.g. `input=$(cat)`), wrapped in `{MARKER}` markers, with $D set to your state dir:")
    print()
    print(SNIPPET)


def _string_end(text: str, quote_start: int) -> int:
    """Index just past the closing quote of the JSON string starting at ``quote_start``.

    A 2-char skip on every backslash is enough to find the terminator correctly for any
    valid JSON escape, ``\\uXXXX`` included: the four hex digits that follow ``\\u`` are
    plain characters, never a quote or a backslash themselves. Only used to find a key
    string's span here — value spans come from the real decoder, see ``_object_key_span``.
    """
    i = quote_start + 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    raise ValueError("unterminated JSON string")


def _ws_end(text: str, pos: int) -> int:
    """Index just past the whitespace run starting at ``pos``.

    ``_WS_RE`` matches zero-or-more chars, so ``.match`` always succeeds for any ``pos``
    in ``[0, len(text)]`` -- every call site here passes such a position, so this is
    never ``None`` in practice."""
    m = _WS_RE.match(text, pos)
    assert m is not None
    return m.end()


def _object_key_span(text: str, key: str) -> tuple[int, int] | None:
    """Span ``(start, end)`` of ``key``'s value in the JSON object opening at the start of
    ``text``, or ``None`` if ``text`` doesn't open with an object or has no such key at
    that level.

    Walks sibling key/value pairs structurally: each value's extent comes from the real
    stdlib decoder (``JSONDecoder.raw_decode``), not a text search, so a same-named key
    nested inside a sibling's value — an array entry, a hook's own ``command`` field, a
    decoy object — is skipped wholesale along with the rest of that sibling and can never
    be mistaken for this level's key. This is what keeps the splice target in agreement
    with ``_command_of``'s ``data.get("statusLine")``, which is unambiguous by construction
    (plain dict access, no notion of "the wrong nested one").
    """
    decoder = JSONDecoder()
    i = _ws_end(text, 0)
    if i >= len(text) or text[i] != "{":
        return None
    i += 1
    while True:
        i = _ws_end(text, i)
        if i >= len(text) or text[i] == "}":
            return None
        if text[i] != '"':
            return None
        key_end = _string_end(text, i)
        try:
            this_key = json.loads(text[i:key_end])
        except json.JSONDecodeError:
            return None
        i = _ws_end(text, key_end)
        if i >= len(text) or text[i] != ":":
            return None
        i = _ws_end(text, i + 1)
        try:
            _, value_end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            return None
        if this_key == key:
            return i, value_end
        i = _ws_end(text, value_end)
        if i < len(text) and text[i] == ",":
            i += 1
            continue
        return None


def _top_level_key_count(text: str, key: str) -> int:
    """How many times ``key`` appears as a top-level key of the JSON object opening
    ``text``. ``json.loads`` silently keeps the last of several duplicate top-level keys,
    while ``_object_key_span`` (structural, first-match) stops at the first — so a
    duplicate ``statusLine`` key would let a splice land on the shadowed object while
    ``data.get("statusLine")`` reports the other one as live. Callers refuse rather than
    guess which occurrence is real."""
    decoder = JSONDecoder()
    i = _ws_end(text, 0)
    if i >= len(text) or text[i] != "{":
        return 0
    i += 1
    count = 0
    while True:
        i = _ws_end(text, i)
        if i >= len(text) or text[i] == "}":
            return count
        if text[i] != '"':
            return count
        key_end = _string_end(text, i)
        try:
            this_key = json.loads(text[i:key_end])
        except json.JSONDecodeError:
            return count
        if this_key == key:
            count += 1
        i = _ws_end(text, key_end)
        if i >= len(text) or text[i] != ":":
            return count
        i = _ws_end(text, i + 1)
        try:
            _, value_end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            return count
        i = _ws_end(text, value_end)
        if i < len(text) and text[i] == ",":
            i += 1
            continue
        return count


def _splice_command(raw: str, new_command: str) -> str:
    """Rewrite only the top-level ``statusLine.command`` string literal in ``raw``,
    byte-for-byte identical everywhere else — no full-document re-dump, so no risk of
    reordering keys, reformatting whitespace, or mangling unrelated content (e.g.
    ``ensure_ascii`` escaping), and no risk of targeting a same-named key nested inside
    something else (e.g. a hook's own ``command`` field, or a decoy ``statusLine``)."""
    status_span = _object_key_span(raw, "statusLine")
    if status_span is None:
        raise ValueError("could not locate the top-level statusLine object in the raw file")
    obj_start, obj_end = status_span
    sub = raw[obj_start:obj_end]

    cmd_span = _object_key_span(sub, "command")
    if cmd_span is None:
        raise ValueError("could not locate statusLine.command in the raw file")
    quote_start, quote_end = cmd_span
    if sub[quote_start] != '"':
        raise ValueError("statusLine.command is not a JSON string in the raw file")

    new_literal = json.dumps(new_command, ensure_ascii=False)
    new_sub = sub[:quote_start] + new_literal + sub[quote_end:]
    return raw[:obj_start] + new_sub + raw[obj_end:]


_SCRIPT_BLOCK_RE = re.compile(re.escape(MARKER) + r"\n.*?\n" + re.escape(MARKER) + r"\n", re.DOTALL)


def _splice_script(raw: str, state_dir: Path) -> str:
    """Insert the capture snippet immediately after the line that captures stdin,
    leaving every other line byte-identical. A real script makes this unambiguous —
    unlike the inline-JSON case, there's no risk of a same-named key elsewhere."""
    lines = raw.splitlines(keepends=True)
    idx = next((i for i, line in enumerate(lines) if _STDIN_CAPTURE_RE.search(line)), None)
    if idx is None:
        raise ValueError("no recognizable stdin capture in the script")
    resolved = SNIPPET.replace("$D", str(state_dir))
    sep = "" if lines[idx].endswith(("\n", "\r")) else "\n"
    block = f"{sep}{MARKER}\n{resolved}\n{MARKER}\n"
    return "".join([*lines[: idx + 1], block, *lines[idx + 1 :]])


def _install_script(script: Path, state_dir: Path, yes: bool) -> int:
    try:
        raw = script.read_text(encoding="utf-8")
    except OSError as e:
        print(f"could not read {script}: {e}", file=sys.stderr)
        return 1

    if MARKER in raw:
        print("already installed")
        return 0

    try:
        new_raw = _splice_script(raw, state_dir)
    except ValueError as e:
        print(f"could not safely edit {script}: {e}", file=sys.stderr)
        _print_snippet_help()
        return 1

    return _confirm_and_write(script, raw, new_raw, yes, "installed capture snippet")


def _uninstall_script(script: Path, yes: bool) -> int:
    try:
        raw = script.read_text(encoding="utf-8")
    except OSError as e:
        print(f"could not read {script}: {e}", file=sys.stderr)
        return 1

    if MARKER not in raw:
        print("not installed")
        return 0

    new_raw = _SCRIPT_BLOCK_RE.sub("", raw, count=1)
    if new_raw == raw:
        print(
            f"could not safely edit {script}: found {MARKER!r} but not a matching block",
            file=sys.stderr,
        )
        return 1

    return _confirm_and_write(script, raw, new_raw, yes, "removed capture snippet")


def _confirm_and_write(path: Path, raw_before: str, raw_after: str, yes: bool, verb: str) -> int:
    diff = "".join(
        difflib.unified_diff(
            raw_before.splitlines(keepends=True),
            raw_after.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )
    print(diff, end="")
    if not yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted")
        return 0
    backup = path.with_name(path.name + ".bak")
    shutil.copy2(path, backup)
    path.write_text(raw_after, encoding="utf-8")
    print(f"{verb} -> {path} (backup: {backup})")
    return 0


def cmd_install(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl statusline install")
    _add_settings_arg(ap)
    ap.add_argument("--yes", "-y", action="store_true")
    args = ap.parse_args(argv)
    path = Path(args.settings)

    # unconditional: a prior install whose state dir was later removed by some other
    # means must not stay silently broken just because we short-circuit below.
    state_dir = ensure_state_dir()

    try:
        raw, data = _read(path)
    except (OSError, json.JSONDecodeError) as e:
        print(f"could not read {path}: {e}", file=sys.stderr)
        return 1

    command, err = _validated_command(path, raw, data)
    if err is not None:
        return err
    if not command:
        print(f"no statusLine command configured in {path}", file=sys.stderr)
        _print_snippet_help()
        return 1

    script = _script_target(command)
    if script is not None:
        return _install_script(script, state_dir, args.yes)

    if MARKER in command:
        print("already installed")
        return 0

    if not _STDIN_CAPTURE_RE.search(command):
        print("statusLine command does not capture stdin in a recognizable way", file=sys.stderr)
        _print_snippet_help()
        return 1

    new_command = command + _installed_block(state_dir)
    try:
        new_raw = _splice_command(raw, new_command)
    except ValueError as e:
        print(f"could not safely edit {path}: {e}", file=sys.stderr)
        _print_snippet_help()
        return 1
    return _confirm_and_write(path, raw, new_raw, args.yes, "installed capture snippet")


def cmd_uninstall(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl statusline uninstall")
    _add_settings_arg(ap)
    ap.add_argument("--yes", "-y", action="store_true")
    args = ap.parse_args(argv)
    path = Path(args.settings)

    try:
        raw, data = _read(path)
    except (OSError, json.JSONDecodeError) as e:
        print(f"could not read {path}: {e}", file=sys.stderr)
        return 1

    command, err = _validated_command(path, raw, data)
    if err is not None:
        return err

    script = _script_target(command) if command else None
    if script is not None:
        return _uninstall_script(script, args.yes)

    if not command or MARKER not in command:
        print("not installed")
        return 0

    idx = command.find(f"\n{MARKER}")
    new_command = command[:idx] if idx != -1 else command
    try:
        new_raw = _splice_command(raw, new_command)
    except ValueError as e:
        print(f"could not safely edit {path}: {e}", file=sys.stderr)
        return 1
    return _confirm_and_write(path, raw, new_raw, args.yes, "removed capture snippet")


def cmd_status(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl statusline status")
    _add_settings_arg(ap)
    args = ap.parse_args(argv)
    path = Path(args.settings)

    try:
        raw, data = _read(path)
    except (OSError, json.JSONDecodeError) as e:
        print(f"could not read {path}: {e}", file=sys.stderr)
        return 1

    command, err = _validated_command(path, raw, data)
    if err is not None:
        return err

    script = _script_target(command) if command else None
    if script is not None:
        print(f"target: script ({script})")
        try:
            script_raw = script.read_text(encoding="utf-8")
        except OSError as e:
            print(f"could not read {script}: {e}", file=sys.stderr)
            return 1
        installed = MARKER in script_raw
    else:
        print("target: inline command" if command else "target: none")
        installed = command is not None and MARKER in command
    print(f"snippet: {'installed' if installed else 'not installed'}")

    capture = default_capture_path()
    if not capture.exists():
        print(f"capture file: none ({capture})")
        return 0

    now = datetime.now(UTC)
    age = now.timestamp() - capture.stat().st_mtime
    windows = read_windows(capture, now)
    print(f"capture file: {capture} (age {int(age)}s)")
    print(f"windows current: {'yes' if windows else 'no'}")
    return 0
