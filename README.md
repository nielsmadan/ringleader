# ringleader

Curates the **dormant** agent sessions sitting on disk — naming them, classifying what state
they're in, surfacing what's still in-flight vs. abandoned, harvesting what they taught you, and
pruning the rest before the GC clock silently deletes it.

Reads Claude Code, Codex, OpenCode, and Pi sessions; reports token usage across all four. OpenCode
session curation is read-only: views, triage, detail, export, and harvest work, while file
lifecycle verbs decline its database-backed sessions.

## Install

```sh
just install-local        # migrates any legacy repo-root state, then reinstalls `rl`
```

or directly `uv tool install --reinstall .` — the `--reinstall` matters, because the version
never changes, so a plain `uv tool install .` audits the existing install and keeps the old
code. Either way this puts `rl` on `PATH` as a real console command — run it as `rl <command>`
from anywhere, not `./rl`. From a source checkout without installing, use `uv run rl <command>`.

**Working on ringleader itself?** Use `just install-editable` — a normal install copies the
source, so the global `rl` silently falls behind your checkout until you reinstall.

State (registry, archive, pending harvest queue) lives under `~/.local/share/ringleader/`
(or `$XDG_DATA_HOME/ringleader`) — not in this repo.

→ [Getting started](docs/user/getting-started.md)

## Quick start

```sh
rl overview            # where is my work spread — a rollup by git project
rl triage              # what awaits a decision
rl inbox               # work the queue in a TUI
rl doctor              # what's wired, and how aggressive your GC clock is
```

The default overview's `total` is the number of sessions still awaiting a triage decision.
Use `--status all` when you want every matching live session instead.

Then wire the skills into your agent so it can drive all of this for you:

```sh
rl install             # skills, session-end hook, statusline capture
```

→ [Agent setup](docs/user/agent-setup.md)

## What it does

- **Triage** — every dormant session gets one of five decisions: finish it, keep going, snooze,
  keep long-term, or delete. `rl overview` groups by git project, collapsing worktrees and
  clones of the same repo. → [The triage workflow](docs/user/triage-workflow.md)
- **Harvest** — distill what a session taught into durable markdown before it disappears, then
  publish it into the repo's living docs. `rl session finish` refuses a session that was never
  harvested. → [Finishing a session](docs/user/finishing-a-session.md)
- **Archive, don't delete** — for file-backed sources, archiving *moves* the transcript out of the agent's store, so
  `claude --resume` can't see it, and `rl session unarchive` puts it back.
  → [Archives and retention](docs/user/archives-and-retention.md)
- **Prune** — remove the tiny, stale dead-ends. Previews by default; only `--delete` deletes.
- **Usage** — where your tokens went, across four harnesses, plus 5h/weekly subscription
  windows. → [Usage analytics](docs/user/usage-analytics.md)

## Skills

`.agents/skills/` ships seven agent-neutral skills that drive `rl` instead of raw commands, and
read natively in Claude Code, Codex, OpenCode and Pi:

| Skill | Does |
|---|---|
| `ringleader-init` | onboard a machine, then run a bounded first triage pass |
| `ringleader-overview` | the morning read: what's in flight, stale, or snoozed |
| `ringleader-triage` | recurring cleanup of the last few days |
| `ringleader-cleanup` | the same over the entire backlog; resumable across runs |
| `ringleader-finish` | finish the session you're *in*, harvesting from live context — or nothing, when there was nothing to keep |
| `ringleader-usage` | explain where tokens went and how to spend fewer |
| `ringleader-publish` | drain pending harvests into the living docs of matching repos |

`memory-triage` is separate and lives in `.claude/skills/`.

```sh
rl install skills      # symlink into ~/.agents/skills and ~/.claude/skills
rl update all          # re-sync after a repo change
rl uninstall skills    # remove only what this tool created
```

→ [Agent setup](docs/user/agent-setup.md) · `.agents/skills/SMOKE.md` for the manual checklist

## Commands

```sh
rl inbox                            # TUI to blitz dormant sessions inbox-zero style
rl roundup [--yes]                  # refresh + prune (deletes) + enrich in one shot
rl overview [<selector>] [--json]   # the view; project rollup by default
rl triage [--json]                  # sessions awaiting a decision
rl prune [--delete]                 # delete tiny + stale sessions
rl usage [--breakdown] [--by X]     # token usage across claude/codex/opencode/pi
rl harvests [--json]                # inbox of harvested knowledge
rl session search <query>           # find a session by name, id prefix or cached blurb
rl doctor                           # what's linked, registered, and how retention is set
rl session <sub>                    # session plumbing: refresh, harvest, finish, archive, ...
rl mem <sub>                        # memory plumbing
rl data <sub>                       # export, tokens, migrate, presence
rl data presence status [--json]    # is live-session presence wired up
rl workspace move <old> <new>       # move a checkout without stranding its sessions
rl config show | set <key> <value>  # the AI engine and retention (stores no secrets)
```

A `<selector>` is a git-project label (`ringleader`) or a cwd path — **not** a session id.

→ [Full CLI reference](docs/user/cli.md)

## Live session presence

Ringleader's presence commands report which agent sessions are live using
[hooklinesinker](https://github.com/nielsmadan/hooklinesinker), a small
shared binary that owns the status hooks for Claude Code, Codex, OpenCode, Pi, Factory Droid,
Qwen Code and Kimi Code. **You do not install a separate Brew dependency**: the release artifacts
for all four supported architectures ride along inside ringleader's own wheel.

```sh
rl data presence install [--agent claude]... [--yes]   # register + install hooks
rl data presence status [--json]                       # what is wired up
rl data presence uninstall [--yes]                     # remove only ringleader's consumer
```

A plain `uv tool install .` leaves presence setup to you; `just install-local`,
`just install-editable`, and `just refresh-local` also activate presence.
`ringleader-init` runs `status` and asks before running `install`. Installing registers
ringleader as a *consumer* — several tools can share one binary and one set of hooks, and
uninstalling removes only ringleader's registration, leaving the hooks in place for whoever
else still uses them. A command that needs live sessions never installs silently; it fails and
names `rl data presence install`.

**The privacy boundary:** presence reads use Hooklinesinker's CLI JSON commands with a
2-second timeout. The status ledger under `$XDG_STATE_HOME/hooklinesinker` stays private
to that binary. `rl doctor` ends with a `presence` section, and
`hooklinesinker doctor` reports the same checks first-hand.

Working from a source checkout? `just install-local` stages the artifacts first (see
`scripts/vendor-hooklinesinker.sh`, which verifies a release build's `SHA256SUMS` before
copying anything into `ringleader/_vendor/`). Without a staged artifact everything else still
installs and works — only `rl data presence` reports what is missing.

For a local release build outside the default `../hooklinesinker/dist` location:

```sh
HOOKLINESINKER_DIST=/path/to/hooklinesinker/dist just install-local
```

The directory must contain all three release binaries and `SHA256SUMS`.

## The AI engine

`rl session enrich` and `rl session harvest` need a model. Ringleader **stores no secrets** — it
drives an already-installed agent CLI one-shot (`claude`, then `codex`), reusing that CLI's own
auth. `rl config set ai <claude|codex|llm>` pins a choice; the `llm` engine calls a provider API
directly with your own key and is not a dependency. With nothing available, both verbs are clean
no-ops and classifying stays a manual step in the triage skill.

## Documentation

Guides for **using** ringleader live in [`docs/user/`](docs/user/index.md):

- [Getting started](docs/user/getting-started.md) — install, first scan, first triage pass.
- [Agent setup](docs/user/agent-setup.md) — skills, hooks, statusline, the AI engine, sandbox.
- [The triage workflow](docs/user/triage-workflow.md) — the recurring loop.
- [Finishing a session](docs/user/finishing-a-session.md) — harvesting, and why finish ≠ archive.
- [Archives and retention](docs/user/archives-and-retention.md) — including a manual restore.
- [Usage analytics](docs/user/usage-analytics.md) — tokens, windows, multi-machine merge.
- [CLI reference](docs/user/cli.md) — every command at a glance.

Docs for **building** ringleader are in [`docs/overview.md`](docs/overview.md):
[features/](docs/features/overview.md) (what it does), [tech/](docs/tech/overview.md) (how it's
built), [decisions/](docs/decisions/overview.md) (ADRs), and
[reference/](docs/reference/agent-session-management.md) (how the external agent CLIs behave).

## Development

```sh
just test              # pytest
just check             # everything CI runs: ruff check + format --check + mypy + tests
just setup             # install dependencies and hooks, then verify the checkout
just doctor            # verify tools and hook installation
just install-editable  # track the working tree
just sandbox           # isolated throwaway world for exercising the skills
just sync-playbook     # re-sync the bundled skill playbooks
```

Hooks are defined in `lefthook.yml`: pre-commit lints and format-checks staged `*.py`;
pre-push runs `ruff check`, `mypy`, and the suite.

See `AGENTS.md` for contributor and agent guidance.
