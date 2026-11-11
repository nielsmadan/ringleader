# ringleader

Curates the **dormant** agent sessions sitting on disk — naming them, classifying what state
they're in, surfacing what's still in-flight vs. abandoned, harvesting what they taught you, and
pruning the rest before the GC clock silently deletes it.

Reads Claude Code, Codex, Factory Droid, OpenCode, and Pi sessions; reports token usage across
all five. OpenCode session curation is read-only: views, triage, detail, export, and harvest
work, while file lifecycle verbs decline its database-backed sessions.

## Install

```sh
just install        # migrates any legacy repo-root state, then reinstalls `rl`
```

or directly `uv tool install --reinstall .` — the `--reinstall` matters, because the version
never changes, so a plain `uv tool install .` audits the existing install and keeps the old
code. Either way this puts `rl` on `PATH` as a real console command — run it as `rl <command>`
from anywhere, not `./rl`. From a source checkout without installing, use `uv run rl <command>`.

**Working on ringleader itself?** Use `just install-editable` — a normal install copies the
source, so the global `rl` silently falls behind your checkout until you reinstall.

Re-run `just install` to refresh the snapshot, including when the version is unchanged.
`just uninstall` removes the installed CLI and preserves your registry, archives, and configuration.

State (registry, archive, pending harvest queue) lives under `~/.local/share/ringleader/`
(or `$XDG_DATA_HOME/ringleader`) — not in this repo.

→ [Getting started](docs/user/getting-started.md)

### From a release

Each GitHub release carries `ringleader-X.Y.Z-py3-none-any.whl` (with the presence binaries
and the skills inside) and a `SHA256SUMS`. Check the wheel against it, then:

```sh
uv tool install --force ./ringleader-X.Y.Z-py3-none-any.whl
rl --version                        # rl X.Y.Z
rl install skills --yes             # the ringleader-* skills, for agent handoffs
rl install hooks --yes              # the session-end hook that archives finished sessions
rl data presence install --yes      # live-session presence (see below)
```

## Quick start

```sh
rl overview            # where is my work spread — a rollup by git project
rl triage              # what awaits a decision
rl                     # the home TUI (rl inbox): what's waiting, what's next, triage it
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
- **Inbox** — bare `rl` opens a home screen: counts, the next thing to do, and one-key triage
  (archive, keep going, snooze, keep, delete with undo), plus resume, roundup, prune, harvests
  and usage. Agent handoffs start the right skill in the right directory.
  → [The triage workflow](docs/user/triage-workflow.md)
- **Harvest** — distill what a session taught into durable markdown before it disappears, then
  publish it into the repo's living docs. `rl session finish` refuses a session that was never
  harvested. → [Finishing a session](docs/user/finishing-a-session.md)
- **Archive, don't delete** — for file-backed sources, archiving *moves* the transcript out of the agent's store, so
  `claude --resume` can't see it, and `rl session unarchive` puts it back.
  → [Archives and retention](docs/user/archives-and-retention.md)
- **Start** — `rl start <agent>` opens a fresh conversation in the current directory.
  Add `--worktree` for a separate checkout: native agent worktrees where supported,
  or a persistent Git checkout for Pi.
  → [Starting a fresh session](docs/user/cli.md#starting-a-fresh-session)
- **Resume** — `rl resume` is one picker over every agent's sessions, filtered as you type.
  Pick one to resume it or fork a new session with that agent, in the right directory,
  including a fork into a separate worktree with `--worktree`,
  or hand it to a *different* agent via a native conversion (falling back to a Markdown
  transcript of the old session only if that's refused, or if you pass `--markdown`).
  → [Resuming sessions](docs/features/resuming-sessions.md)
- **Prune** — remove the tiny, stale dead-ends. Previews by default; only `--delete` deletes.
- **Usage** — where your tokens went, across five harnesses, plus 5h/weekly subscription
  windows. → [Usage analytics](docs/user/usage-analytics.md)
- **Analyze** — where sessions went wrong: retry loops, failure streaks, rewinds, cache rebuilds,
  recurring errors, scored per session and grouped by project, tool, skill and model. Reads from a
  local SQLite index; a model is used only for optional labels. → [Analysis](docs/features/analysis.md)

## Skills

`skills/` ships nine agent-neutral skills that drive `rl` instead of raw commands, and
read natively in Claude Code, Codex, Droid, OpenCode and Pi:

| Skill | Does |
|---|---|
| `ringleader-init` | onboard a machine, then run a bounded first triage pass |
| `ringleader-overview` | the morning read: what's in flight, stale, or snoozed |
| `ringleader-triage` | recurring cleanup of the last few days |
| `ringleader-cleanup` | the same over the entire backlog; resumable across runs |
| `ringleader-finish` | finish the session you're *in*, harvesting from live context — or nothing, when there was nothing to keep |
| `ringleader-usage` | explain where tokens went and how to spend fewer |
| `ringleader-publish` | drain pending harvests into the living docs of matching repos |
| `ringleader-analyze` | find where recent sessions went wrong and save the findings as a report |
| `ringleader-recommend` | turn saved findings into proposed edits to your agent setup; proposes only |

`memory-triage` is separate and lives in `loadout/skills/`.

```sh
rl install skills      # symlink into ~/.agents/skills, ~/.claude/skills and ~/.factory/skills
rl update all          # re-sync after a repo change
rl uninstall skills    # remove only what this tool created
```

→ [Agent setup](docs/user/agent-setup.md) · `skills/SMOKE.md` for the manual checklist

## Commands

```sh
rl start <agent>                    # fresh conversation here: claude, codex, droid, opencode, pi
rl start <agent> --worktree         # fresh conversation in a separate checkout
rl resume [<query>]                 # TUI to re-enter a session, in its agent or another
rl resume --last                    # re-enter the most recent session here, no picker
rl resume --last --worktree         # fork it into a separate checkout with its own agent
rl inbox [--read-only]              # the daily home TUI; bare `rl` in a terminal opens it
rl roundup [--enrich all|recent|none] [--preview] [--yes] # refresh + sweep + prune (deletes) + enrich, plan first
rl overview [<selector>] [--json]   # the view; project rollup by default
rl triage [--json]                  # sessions awaiting a decision
rl prune [--delete]                 # delete tiny + stale sessions
rl usage [--breakdown] [--by X]     # token usage across claude/codex/droid/opencode/pi
rl analyze [<selector>] [--by X]    # where sessions went wrong and where tokens went, from a SQLite index
rl analyze session <id>             # one session in detail
rl analyze issues                   # error signatures that recur across sessions
rl analyze reports [save ...]       # saved findings and recommendations
rl harvests [--json]                # inbox of harvested knowledge
rl session search <query>           # find a session by name, id prefix or cached blurb
rl handoff triage --project K --agent A # an agent triages one project; --dry-run previews
rl doctor                           # what's linked, registered, and how retention is set
rl session <sub>                    # session plumbing: refresh, harvest, finish, archive, ...
rl mem <sub>                        # memory plumbing
rl data <sub>                       # export, tokens, migrate, presence
rl data presence status [--json]    # is live-session presence wired up
rl workspace move <old> <new>       # move a checkout without stranding its sessions
rl config show | set <key> <value>  # the AI engine, retention, label model (stores no secrets)
```

A `<selector>` is a git-project label (`ringleader`) or a cwd path — **not** a session id.

→ [Full CLI reference](docs/user/cli.md)

## Continue a session in another agent

```sh
rl session refresh
rl session convert pi:<id> --to claude --dry-run
rl session convert pi:<id> --to claude
rl session fork <id> --compact
rl session handoff <id> --to codex
rl session lineage <id>
```

Conversion preserves the source and creates a native target in the source's
recorded workspace. A repeat reuses that result; `fork` always creates a new one.
`handoff` forks and resumes. `--compact` deterministically replaces complete tool
exchanges with explicit omission markers while keeping instructions and prose.
`--open` selects visible open work; `--running` selects verified live sessions.
Selection reads the registry without implicitly refreshing it. `rl resume --to`
uses this same conversion, so the same support and limits below apply there too.

Targets are Claude Code, Codex, OpenCode, Pi, and Droid. Each converts when its
installed version is either pinned from upstream research or self-verified on
this machine — `rl doctor --verify-conversion [AGENT]`, a one-time offline check
that `rl resume --to` also runs itself, interactively, the first time it needs an
unverified target. Codex/OpenCode require initialized compatible databases;
OpenCode also requires supported explicit model configuration, and its native
runtime request construction remains unverified (CLI-acceptance tier only).
Ordinary sessions with unsupported context, compaction, tools, or attachments
fail explicitly. Preview first; `rl doctor` checks compatibility for its current
workspace and native roots.

After an interruption, use `rl data transaction list` and `resume <transaction-id>`.
Rollback removes only private staging; published targets are recovered forward.
Archived/deleted conversion results are reported rather than recreated. Workspace
relocation remains `rl workspace move`/`retarget`.

→ [Conversion workflow, limitations, and recovery](docs/user/session-portability.md)

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

A plain `uv tool install .` leaves presence setup to you; `just install` and
`just install-editable` also activate presence.
`ringleader-init` runs `status` and asks before running `install`. Installing registers
ringleader as a *consumer* — several tools can share one binary and one set of hooks, and
uninstalling removes only ringleader's registration, leaving the hooks in place for whoever
else still uses them. A command that needs live sessions never installs silently; it fails and
names `rl data presence install`.

The shared binary is never downgraded, so whichever consumer bundles a newer hooklinesinker
first activates it for all of them. Hooks written by an older version then show as `drifted`
in `rl data presence status` until `rl data presence install` rewrites them, and Codex asks
you to approve new or changed hooks in its `/hooks` menu before they run.

**The privacy boundary:** presence reads use Hooklinesinker's CLI JSON commands with a
2-second timeout. The status ledger under `$XDG_STATE_HOME/hooklinesinker` stays private
to that binary. `rl doctor` ends with a `presence` section, and
`hooklinesinker doctor` reports the same checks first-hand.

Working from a source checkout? `just install` stages the artifacts first: it downloads the
hooklinesinker release pinned in `scripts/hooklinesinker.json`, verifies each binary against the
sha256 recorded in the pin and the release's `SHA256SUMS`, checks that the binary reports the
pinned version, and copies it into `ringleader/_vendor/`. Without
network access everything else still installs and works — only `rl data presence` reports
what is missing.

```sh
just check-hooklinesinker          # is a newer release published?
just update-hooklinesinker         # pin the latest release (or pass a version)
just install                       # stage the new pin and reinstall
```

To stage a local release build instead of the pinned download:

```sh
HOOKLINESINKER_DIST=/path/to/hooklinesinker/dist just install
```

That directory must contain all three release binaries and `SHA256SUMS`.

## The AI engine

`rl session enrich`, `rl session harvest` and `rl analyze` labels need a model. Ringleader **stores no secrets** — it
drives an already-installed agent CLI one-shot (`claude`, then `codex`), reusing that CLI's own
auth. `rl config set ai <claude|codex|llm>` pins a choice; the `llm` engine calls a provider API
directly with your own key and is not a dependency. With nothing available, both verbs are clean
no-ops and classifying stays a manual step in the triage skill. Enrich defaults to `haiku` on the
`claude` engine; change it with `rl config set enrich.model <name>` (`default` restores `haiku`).
`rl analyze` labels default to `haiku` the same way; `rl config set analyze.model <name>` changes
them. If that CLI gets its
credentials from a shell wrapper, `rl config set ai.command_prefix sops-exec` launches it through one.

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

**Releasing.** Install [curtaincall](https://github.com/nielsmadan/curtaincall), so `curtains` is
on `PATH`, then from a clean, pushed `main`:

```sh
just release --dry-run   # the proposed version and the commits it covers
just release             # check, prepare, confirm, tag and push
just release minor       # or patch, major, or an explicit X.Y.Z
```

`scripts/release.json` is the policy. The first release is `0.2.0`; later ones are proposed from
the commit types since the last tag. `just release` runs `just check`, sets `pyproject.toml`'s
`version` (the only version), relocks, regenerates `CHANGELOG.md` with git-cliff, commits
`chore: release X.Y.Z`, then pushes `main` and an annotated `vX.Y.Z` tag together.

The tag starts `.github/workflows/release.yml`. It validates the tag, refuses one that does not
match the version, vendors the pinned hooklinesinker (failing, unlike `just vendor`, when the
download does), runs `just check` and builds the wheel in a read-only job. A separate job, holding
the only write token, publishes a GitHub release with `ringleader-X.Y.Z-py3-none-any.whl`,
`contract-golden.tar.gz` (the `tests/golden/contract-*.json` documents, flat) and `SHA256SUMS`,
assembled and checked by `scripts/release.py`. If publishing fails after the tag is pushed, fix
`main` if needed and rerun it with `gh workflow run release.yml --ref main -f tag=vX.Y.Z`.

See `AGENTS.md` for contributor and agent guidance.
