# Wire ringleader into the agents you use

> **Problem:** ringleader curates sessions from four different agents, so it has to install
> itself into four different configuration systems — and be removable from all of them without
> leaving residue.

## Overview

```sh
rl install              # everything: skills, statusline, hooks
rl install skills       # or one noun at a time
rl update all           # re-sync what's already wired, after a repo change
rl uninstall skills     # remove only what this tool created
rl doctor               # what's linked, registered, and how retention is set
```

Nouns: `skills`, `statusline`, `hooks`, `all`. Bare `rl install` does `all`. Each takes `--yes`;
`statusline` also takes `--settings PATH`. `rl update` has no `statusline` noun.

These installers **detect** the agents present, **preview** their edits, and skip what is absent.

## Live-session presence

`rl data presence install` registers Ringleader as a consumer of the shared Hooklinesinker
binary and installs its status hooks. `status` reports the wiring and running-session count;
`uninstall` removes only Ringleader's registration. Hooks remain while another consumer needs them.

Presence supports Claude Code, Codex, OpenCode, Pi, Factory Droid, Qwen Code, and Kimi Code.
Installation names the detected agents and confirms before changing their configuration;
repeat `--agent` to choose explicitly, or use `--yes` for unattended setup.
`just install` and `just install-editable` run that setup too.
Dormant-session views do not yet use presence to exclude running sessions.

## Skills

Seven bundled skills at `.agents/skills/ringleader-*`, each an agent-neutral `SKILL.md`. Six
also bundle a `references/playbook.md` synced from the canonical `PLAYBOOK.md`
(`just sync-playbook`); `ringleader-publish` ships `references/protocol.md` instead:

| Skill | Does |
|---|---|
| `ringleader-init` | onboard a machine: verify the CLI, check retention, first bounded triage |
| `ringleader-overview` | the morning read across dormant sessions |
| `ringleader-triage` | recurring cleanup of the last few days |
| `ringleader-cleanup` | the full backlog, in resumable chunks |
| `ringleader-finish` | finish the session you are *in*, harvesting from live context |
| `ringleader-usage` | explain where tokens went |
| `ringleader-publish` | drain pending harvests into repository docs |

`rl install skills` **symlinks** them into `~/.agents/skills` (Codex, OpenCode, Pi) and
`~/.claude/skills` (Claude Code). `memory-triage` is separate and still lives in
`.claude/skills/`.

## Hooks

`rl install hooks` registers a session-end hook running `rl session sweep --from-hook`, so a
finished session is archived seconds after the agent exits instead of on the next roundup.
JSON-configured agents get a hooks-file entry; Pi gets a bundled TypeScript extension.

## Status line

`rl install statusline` adds a capture block to Claude Code's `statusLine.command` so
`rl usage windows` can report 5h/weekly limits. It **appends** rather than replaces, and refuses
rather than guesses when the settings file is ambiguous — printing the snippet for manual
placement instead.

## Removable by design

Uninstall is ownership-scoped. A skill symlink counts as ours only if its name starts with
`ringleader-` **and** it points inside the bundled skills dir; a hook entry only if its command
path lies inside the bundled dir. Your own `SessionEnd` hook is never touched.

## Key entry points

- `commands/skills.py`, `commands/hooks.py`, `commands/statusline.py`, `commands/setup.py`,
  `commands/doctor.py`.
- `paths.bundled_skills_dir()` — the wheel copy when installed, the repo's `.agents/skills/`
  for a source run.

## Gotchas

- **Skills are symlinked**, so a repo edit is immediately live in every wired agent.
- **`rl doctor` is the source of truth** for what is actually wired.
- **The status-line installer can decline** on an ambiguous settings file, by design.

## Related

- [0007: agent-neutral skill packaging](../decisions/0007-agent-neutral-skill-packaging.md)
- [tech/integration.md](../tech/integration.md) · [user/agent-setup.md](../user/agent-setup.md)
