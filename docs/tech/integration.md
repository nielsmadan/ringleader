# Agent integration

`commands/skills.py`, `commands/hooks.py`, `commands/statusline.py`, `session_env.py`,
`safe_write.py`: everything that writes into an agent's own configuration.

## Purpose

Ringleader wires itself into four agents — installing skills, a session-end hook, and a Claude
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
| OpenCode | `~/.agents/skills` | detected via `~/.config/opencode` |
| Pi | `~/.agents/skills` | `~/.pi/agent/extensions` (a TypeScript extension) |

`paths.exists_or_report()` is used for every probe: it reports a stat failure instead of
silently treating it as absent, so a permissions problem does not look like "agent not
installed".

### Skills

`rl install skills` **symlinks** each bundled `ringleader-*` directory into every detected skills
dir. `_unique_dirs()` collapses targets that resolve to the same real path (common when
`~/.claude/skills` is itself a symlink), and `_resolved()` displays the real path so installing
through a symlink is never a surprise.

`_owned()` defines what this tool may remove: a symlink whose name starts with `ringleader-`
**and** whose target lies inside the bundled skills dir. Install refuses to replace a
non-symlink; uninstall touches nothing it does not own.

`paths.bundled_skills_dir()` resolves to the wheel-bundled copy when installed, and to the
repo's `.agents/skills/` for a source run — so an editable install edits live skills.

### Hooks

`rl install hooks` registers a session-end hook that runs `rl session sweep --from-hook`, giving
the seconds-after-exit archive path ([0008](../decisions/0008-sweep-archives-only-quiet-transcripts.md)).
JSON-configured agents get an entry in their hooks file; Pi gets the bundled
`pi-ringleader-finish.ts` extension.

`_is_ours()` is the removal predicate: an entry counts as ours **only if its command path lies
inside `bundled_dir()`**. A user's own `SessionEnd` hook is never touched.

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

### Which session am I in?

`session_env.py` answers that from the **live process**, not from disk: `PROBES` maps a source
name to the environment variable that agent exports (`codex` → `CODEX_THREAD_ID`,
`pi` → `PI_SESSION_ID`, `opencode` → `OPENCODE_SESSION_ID`, `claude-code` →
`CLAUDE_CODE_SESSION_ID`). `current_session()` returns `(source, session_id)` or `None`, and
backs every `--current` flag plus `rl session whoami`. Codex also checks
`CODEX_SESSION_ID` as a fallback (THREAD_ID names the child subagent, SESSION_ID
the parent). `PROBES` is ordered so an inner agent's var wins if a shell
inherits an outer agent's env (Codex/Pi/OpenCode before Claude Code). All four
agents have a `Source`. Pi exports its id, so `--current` works end to end;
OpenCode does not yet export the probed variable, and its database-backed Source
deliberately declines filesystem lifecycle operations.

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
confirm before writing, and define ownership narrowly enough that uninstall can never remove
something the user wrote. The tool edits four other programs' configuration files; being
removable without residue is a correctness property, not a nicety.

## Related

- [0007: agent-neutral skill packaging](../decisions/0007-agent-neutral-skill-packaging.md)
- [features/agent-integration.md](../features/agent-integration.md)
