# Agent integration

`commands/skills.py`, `commands/hooks.py`, `commands/statusline.py`, `session_env.py`,
`safe_write.py`: everything that writes into an agent's own configuration.

## Purpose

Ringleader wires itself into six agents — installing skills, a session-end hook, and a Claude
Code status-line capture. Every one of those edits a file the agent owns, so this layer's whole
concern is **editing someone else's config without breaking it and without becoming
un-removable**.

## How it works

### Detection

Each installer probes for an agent's home directory and skips what is absent, so a machine with
only Claude Code installs only Claude Code's wiring.

| Agent | Skills dir | Hook config |
|---|---|---|
| Claude Code | `~/.claude/skills` | `~/.claude/settings.json` |
| Codex | `~/.agents/skills` | `~/.codex/hooks.json` |
| Droid | `~/.factory/skills` | `~/.factory/hooks.json`, or the legacy `~/.factory/hooks/hooks.json` |
| OpenCode | `~/.agents/skills` | detected via `~/.config/opencode` |
| Pi | `~/.agents/skills` | `~/.pi/agent/extensions` (a TypeScript extension) |
| Cursor CLI | `~/.agents/skills` | `~/.cursor/hooks.json`, detected via its `cli-config.json` |

`paths.exists_or_report()` is used for every probe: it reports a stat failure instead of
silently treating it as absent, so a permissions problem does not look like "agent not
installed".

Droid's home is `paths.factory_dir()`, not `home / ".factory"`, because `FACTORY_HOME_OVERRIDE`
replaces **HOME** rather than the `.factory` name; `paths.factory_label()` is what messages print,
so an overridden root is named in full instead of as a misleading `~/.factory`. Droid also gets
its own skills directory rather than the shared `~/.agents/skills`, because `.factory/skills` is
its documented native personal skills location — the same reason Claude Code gets `~/.claude/skills`.

Cursor CLI is detected by `paths.cursor_cli_installed()`: `cli-config.json` in its config dir
(`CURSOR_CONFIG_DIR`, else `$XDG_CONFIG_HOME/cursor`, else `~/.cursor`). The Cursor IDE creates
`~/.cursor` as well, so the directory alone would wire up an IDE-only machine. Cursor reads
`~/.agents/skills` as one of its own skills dirs, so it shares that target rather than getting
`~/.cursor/skills`.

### Skills

`rl install skills` **symlinks** each bundled `ringleader-*` directory into every detected skills
dir. `_unique_dirs()` collapses targets that resolve to the same real path (common when
`~/.claude/skills` is itself a symlink), and `_resolved()` displays the real path so installing
through a symlink is never a surprise.

`_owned()` defines what this tool may remove: a symlink whose name starts with `ringleader-`
**and** whose target lies inside the bundled skills dir. Install refuses to replace a
non-symlink; uninstall touches nothing it does not own.

`paths.bundled_skills_dir()` resolves to the wheel-bundled copy when installed, and to the
repo's `skills/` for a source run — so an editable install edits live skills.

### Hooks

`rl install hooks` registers a session-end hook that runs `rl session sweep --from-hook`, giving
the seconds-after-exit archive path ([0008](../decisions/0008-sweep-archives-only-quiet-transcripts.md)).
JSON-configured agents get an entry in their hooks file; Pi gets the bundled
`pi-ringleader-finish.ts` extension.

`_is_ours()` is the removal predicate: an entry counts as ours **only if its command path lies
inside `bundled_dir()`**. A user's own `SessionEnd` hook is never touched.

#### Droid's hooks file

`JsonAgent.flat` exists for droid. A standalone `hooks.json` is keyed by event name
**directly** — `{"SessionEnd": [...]}` — while the `{"hooks": {"SessionEnd": [...]}}` nesting
Claude Code and Codex use is only valid inside `settings.json`. `_event_container()` is the one
place that branch lives; everything downstream patches the same shape. Droid's event vocabulary
is otherwise exactly Claude Code's, and `timeout` is per command, in seconds, defaulting to 60 —
droid's `JsonAgent` writes 5.

`_droid_agent()` handles the loader being **either/or**: once `hooks.json` exists, droid never
reads the legacy `.factory/hooks/hooks.json`. Creating the former would therefore silently
disable everything in the latter, so ringleader writes into the legacy file when that is the one
present. The same fact drives `prune_when_empty`: uninstall **deletes** a `hooks.json` it
emptied, because an empty one shadows the legacy file just as effectively as a full one — but
only where there is no legacy file beneath it. Where there is, the emptied file stays, whoever
created it: deleting it would hand `hooks/hooks.json` back to droid and re-arm hooks the user
shadowed on purpose.

`_warn_if_droid_settings_shadowed()` covers the last edge. Droid merges its hooks file over
`settings.json` per event with the hooks file winning, so installing ours replaces a `SessionEnd`
declared in `settings.json` rather than joining it. The installer says so before writing rather
than letting the user's hook quietly stop firing.

#### Cursor's hooks file

Cursor reads user hooks from `$HOME/.cursor/hooks.json` whatever `CURSOR_CONFIG_DIR` says. Its
shape is a third one: `{"version": 1, "hooks": {"sessionEnd": [entry, ...]}}`, with camelCase
event names and **bare** entries rather than `{"hooks": [...]}` matcher groups. `JsonAgent` says so
with `event="sessionEnd"`, `grouped=False` and `version=1`, which install writes only into a file
that has no `version` yet. `_hook_entries()` is where the grouped and bare shapes meet. Cursor
also loads Claude Code's `~/.claude/settings.json` hooks, so on a machine with both the sweep may
run twice at session end; it is idempotent.

### Status line

`rl install statusline` is the most invasive edit, because Claude Code's `statusLine.command` is
a shell command the user may already have customized. The installer **appends** a capture block
rather than replacing the command, and refuses rather than guessing when the file is ambiguous:

- more than one top-level `statusLine` key → refuse, "refusing to guess which is live";
- a non-string `statusLine.command` → refuse.

`rl install statusline --help` also prints the snippet for manual placement, for a status line
the installer will not touch.

### Safe writes

`safe_write.backup_and_write()` backs the original bytes up to `<name>.bak` before replacing a
file, preserves the existing mode (the status-line script must stay executable), and writes
atomically. `diff()` renders the change for the confirmation prompt — every installer previews
before it writes.

### Shared presence

`presence.PresenceClient` invokes Hooklinesinker's protocol-1 CLI with a two-second timeout.
It never reads the helper's state files. Running IDs are qualified by agent; unavailable or
malformed responses stay distinguishable from an empty set of running sessions.

`commands/presence.py` exposes install, status, and uninstall under `rl data presence`.
Installation verifies the bundled binary's checksum, compares it with the active version,
registers the `ringleader` consumer, and delegates native hook edits to the helper.
Each successful hook install is verified with `hooks status --json`; unsupported configurations
are reported as failures. OpenCode, Pi, Qwen, and Kimi detection follows the helper's config
directory overrides. Uninstall removes the shared binary and hooks only for the last consumer;
it is a no-op when ringleader is not registered, and keeps any warning hooklinesinker prints
about hooks it left in place. Status prints the setup command when a hook is `drifted` (written
by an older hooklinesinker), and install reminds you to approve Codex hooks in Codex's `/hooks`.

`scripts/hooklinesinker.json` pins the bundled release and the sha256 of each of its three
binaries. `scripts/hooklinesinker.py vendor` downloads it into
`build/hooklinesinker/downloads/<version>/` (reused while its checksums, the pinned hashes and
the binary's `version --json` still match the pin), and `scripts/vendor-hooklinesinker.sh`
stages its three binaries and checksum manifest under `ringleader/_vendor/`. The pinned hashes
are what make a replaced release asset fail: the release's own `SHA256SUMS` would be replaced
with it. `update` moves the pin to a published stable release carrying every artifact, records
the new hashes, and refuses downgrades.
Hatchling includes those ignored artifacts in the wheel.
Source installs without artifacts still work; presence installation reports the missing bundle.

### Which session am I in?

`session_env.py` answers that from the **live process**, not from disk: `PROBES` maps a source
name to the environment variable that agent exports (`codex` → `CODEX_THREAD_ID`,
`pi` → `PI_SESSION_ID`, `opencode` → `OPENCODE_SESSION_ID`, `claude-code` →
`CLAUDE_CODE_SESSION_ID`). `current_session()` returns `(source, session_id)` or `None`, and
backs every `--current` flag plus `rl session whoami`. Codex also checks
`CODEX_SESSION_ID` as a fallback (THREAD_ID names the child subagent, SESSION_ID
the parent). `PROBES` is ordered so an inner agent's var wins if a shell
inherits an outer agent's env (Codex/Pi/OpenCode before Claude Code). Every probed
agent has a `Source`. Pi exports its id, so `--current` works end to end;
OpenCode does not yet export the probed variable, and its database-backed Source
deliberately declines filesystem lifecycle operations. **Droid has no `PROBES` entry** because it
exports no session-id variable to probe — only `DROID_PARENT_SESSION_ID` — so `--current` and
`rl session whoami` do not resolve inside a droid session.

## Key entry points

- `commands/skills.py` — `_install_dirs()`, `_owned()`, `_unique_dirs()`.
- `commands/hooks.py` — `_json_agents()`, `_is_ours()`.
- `commands/statusline.py` — `_validated_command()`, `_installed_block()`.
- `commands/doctor.py` — reports what is wired.
- `session_env.PROBES` / `current_session()`.
- `safe_write.backup_and_write()`.

## Gotchas

- **OpenCode's `--current` probe is forward-looking.** Its Source supplies registry entries,
  but OpenCode does not yet export `OPENCODE_SESSION_ID`, and lifecycle operations deliberately
  decline its database-backed sessions.
- **Droid is not probeable and has no status line to capture.** `rl install statusline` is
  Claude-Code-only, and droid's status-line payload carries no rate-limit data, so there is
  nothing for it to capture — that is a deliberate omission, not a missing case.
- **Symlinked skills mean repo edits are live** in every wired agent immediately. Convenient
  while iterating, surprising if you forget.
- **Uninstall is ownership-scoped**, by symlink target and by command path. A hand-written hook
  or an unrelated `ringleader-`-prefixed directory that is not a symlink is left alone.
- **The status-line installer can refuse** on a settings file it considers ambiguous, and that is
  the designed outcome — the snippet is printed for manual placement instead.
- **`rl doctor` is the source of truth** for what is actually wired, rather than assuming an
  install succeeded everywhere.

## Why

Every installer here follows the same three rules: detect rather than assume, preview and
confirm before writing, and define ownership narrowly enough that uninstall always leaves the
user's own entries behind. The one file uninstall removes outright is a droid `hooks.json` with
nothing left in it, and only where no legacy `hooks/hooks.json` sits beneath it to be un-shadowed.
The tool edits five other programs' configuration files; being removable without residue is a
correctness property, not a nicety.

## Related

- [0007: agent-neutral skill packaging](../decisions/0007-agent-neutral-skill-packaging.md)
- [features/agent-integration.md](../features/agent-integration.md)
