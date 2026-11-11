# Agent setup

Wiring ringleader into the agents you actually use. Everything here detects what's installed,
previews its change, and can be removed cleanly.

```sh
rl install            # skills, statusline, hooks
rl install skills     # or one at a time
rl update all         # re-sync after a repo change
rl uninstall skills   # remove only what this tool created
rl doctor             # what's linked, registered, and how retention is set
```

`install` and `uninstall` confirm before touching anything; `--yes` skips the prompt.

## Skills

`skills/` ships nine agent-neutral skills that drive `rl` instead of raw commands:

| Skill | Does |
|---|---|
| `ringleader-init` | onboard a machine: verify `rl`, check retention (`rl doctor`), run a bounded first pass (`--recent 7`) |
| `ringleader-overview` | the morning read: what's in flight, what's stale, what's snoozed |
| `ringleader-triage` | recurring cleanup of the last few days — decide, harvest, name, review |
| `ringleader-cleanup` | the same flow over the *entire* backlog; resumable across runs |
| `ringleader-finish` | finish the session you're in, harvesting from live context |
| `ringleader-usage` | explain where tokens went and how to spend fewer (read-only) |
| `ringleader-publish` | drain pending harvests into the living docs of matching repos |
| `ringleader-analyze` | find where recent sessions went wrong and save the findings as a report |
| `ringleader-recommend` | turn saved findings into proposed edits to instruction files, skills and config; proposes only |

The eight session-track skills share an agent-neutral `SKILL.md` plus a bundled
`references/playbook.md`, synced from the canonical `skills/PLAYBOOK.md`
(`just sync-playbook`). `ringleader-publish` owns its focused publishing protocol.

All nine read natively in **Claude Code, Codex, Droid, OpenCode, Pi and Cursor CLI** — no
per-agent plugin format. `rl install skills` symlinks them into `~/.agents/skills` (Codex,
OpenCode, Pi, Cursor CLI),
`~/.claude/skills` (Claude Code) and `~/.factory/skills` (Droid).

`memory-triage` is separate and lives in `loadout/skills/`.

> Skills are **symlinked**, so editing the repo changes what every wired agent runs, immediately.

## The session-end hook

```sh
rl install hooks
```

Registers a session-end hook running `rl session sweep --from-hook`, so a finished session is
archived within seconds of quitting instead of at the next roundup. Wired for Claude Code, Codex,
Droid, Pi and Cursor CLI (in `~/.cursor/hooks.json`); OpenCode has no session-end event, so the sweep there runs from `rl roundup` and
`rl session refresh` instead.

Droid's hooks file needs one caveat. Droid reads `~/.factory/hooks.json` *or* the legacy
`~/.factory/hooks/hooks.json`, never both, so `install` writes into whichever you already have
and `uninstall` **deletes** a `hooks.json` it emptied rather than leaving `{}` behind — an empty
one would keep shadowing the legacy file. If your `~/.factory/settings.json` declares its own
`SessionEnd` hook, `install` warns you: droid lets the hooks file win per event, so ours would
replace yours rather than run alongside it.

`rl doctor` reports per agent whether it's installed.

## The status-line capture

```sh
rl install statusline [--settings PATH] [--yes]
rl uninstall statusline
```

Wires a snippet into Claude Code's statusLine command so `rl usage windows` can report 5h and
weekly limits. Path resolution is `--settings PATH` → `RINGLEADER_CLAUDE_SETTINGS` →
`~/.claude/settings.json`.

`install` shows a diff and asks for confirmation, backs the file up to `settings.json.bak`, and
**only inserts the snippet into the existing statusLine command** — nothing else in the file is
touched. Running it again is a no-op ("already installed"); `uninstall` removes exactly what
`install` added, restoring the original command.

**Both statusLine shapes are supported.** If `statusLine.command` is an **inline command**, the
snippet goes into that command. If it's a **path to a script** (as the official docs and tools
like `claude-pace` recommend), `install` reads the script, splices the snippet in after the line
that captures stdin, and backs the script up first — same diff/confirm/insert-only/reversible
contract.

If the referenced file doesn't exist, or neither target has a recognizable stdin capture,
`install` **refuses rather than guessing** and prints the snippet for you to paste yourself,
right after the line that reads stdin (e.g. `input=$(cat)`).

`rl doctor` reports which target is in use, whether the snippet is installed, and whether a
current capture exists.

## The AI engine

`rl session enrich` and `rl session harvest` need a model. Ringleader **stores no secrets** — by
default it drives an already-installed agent CLI one-shot, reusing that CLI's own auth.

```sh
rl config show                     # engine, model, and the resolved active model
rl config set ai claude            # pin one: claude | codex | llm
rl config set model <name>
rl config set ai.command_prefix sops-exec   # launch claude/codex through a wrapper; none clears it
```

Auto-pick order is `claude`, then `codex`. The `llm` engine calls a provider API directly via
the [`llm`](https://llm.datasette.io/) library with your own key — it is **not** a dependency,
so `pip install llm` first; `rl config set ai llm` reports the install hint if it's missing.

`ai.command_prefix` is for an agent CLI whose credentials come from a shell function or alias
rather than its own login: `rl` starts the binary off `PATH`, so a wrapper that only your
interactive shell applies never runs. The prefix is the executable that does the same job. It
stays empty by default, and an engine whose prefix is not on `PATH` counts as unavailable.

With no engine available, `enrich` and `harvest` are clean no-ops and classifying by hand inside
the triage skill remains the way to do it.

## Uninstalling is safe

Removal is ownership-scoped. A skill symlink counts as ringleader's only if its name starts with
`ringleader-` **and** it points inside the bundled skills directory; a hook entry only if its
command path lies inside that directory. Your own `SessionEnd` hook is never touched.

## Trying the skills from a zero state

`rl` takes every input from the environment, so a throwaway world needs no support from the tool:

```sh
just sandbox              # link the skills and enter an isolated sandbox shell
just sandbox-reset        # restore the frozen fixture before a new scenario
```

The first run builds `.sandbox/` from a frozen fixture; later runs preserve whatever the test
changed. The command links the bundled skills, opens an interactive child shell, and puts the
checkout's `rl` on `PATH`. Start an agent there and invoke a skill — every `rl` call lands in
`.sandbox/`, and your real sessions, registry and settings are untouched. Exit the child shell
when finished.

The fixture holds 100 verbatim sessions from nine verified public repositories (70 Claude Code,
30 Codex; evenly split named/unnamed), scanned for identifiers and credential shapes and
reviewed semantically. Raw transcripts stay out of git under
`~/.local/share/ringleader/fixtures/init-v1`. The committed descriptor pins the manifest
fingerprint, source counts and byte count, so an incomplete extraction is rejected before
`.sandbox/` is touched.

See `skills/SMOKE.md` for the manual end-to-end checklist.
