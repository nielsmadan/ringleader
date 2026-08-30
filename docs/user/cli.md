# CLI reference

Every `rl` command at a glance. `rl <command> --help` is authoritative for flags.

From a source checkout without installing, prefix each call with `uv run`.

```
rl
├── inbox                       TUI: blitz dormant sessions inbox-zero style
├── roundup                     refresh + prune + enrich in one shot
├── overview [<selector>]       the view: project rollup, or a deep-dive
├── triage                      sessions awaiting a decision
├── prune                       delete tiny + stale sessions (preview by default)
├── usage                       token usage across claude/codex/opencode/pi
│   ├── session <id>            where one session's tokens went
│   ├── windows                 5h/weekly subscription limits
│   └── export                  dump usage for merging across machines
├── harvests                    inbox of knowledge harvested out of sessions
├── doctor                      what's linked, registered, how retention is set
├── install | update | uninstall  <skills|statusline|hooks|all>
├── config                      show | set <key> <value>
├── session <sub>               session plumbing (see below)
├── mem <sub>                   memory plumbing (see below)
├── data <sub>                  export | tokens | migrate
└── workspace <sub>             move | retarget
```

`rl <group>` with no subcommand lists that group's verbs.

## Everyday

| Command | Does | Key flags |
|---|---|---|
| `rl inbox` | Textual TUI over the dormant-session queue, in three tabs. `j`/`k` move, `h` harvest, `d` delete, `u` undo, `s` snooze, `K` keep, `a` archive, `enter` select, `;` detail, `q` quit. Offers a `roundup` when state is stale. | `--registry` |
| `rl roundup` | Refresh, prune (**deletes**), and enrich in one pass. | `--yes` |
| `rl overview [<selector>]` | Project rollup by default; `<selector>` deep-dives one repo. | `-s/--sessions`, `--status`, `--archived`, `--snoozed`, `--kept`, `--checkouts`, `--cwd`, `--source`, `--recent`, `--no-scan`, `--json` |
| `rl triage` | Sessions not yet reviewed/harvested/parked. | `-u/--unclassified`, `--project`, `--source`, `--recent`, `--no-scan`, `--json` |
| `rl prune` | Delete tiny + stale sessions. **Previews unless `--delete`.** | `--delete`, `--older-than` (7), `--max-turns` (12), `--project` |
| `rl usage` | Token usage from local transcripts. | `--by day\|model\|project\|session\|source`, `--breakdown`, `--source`, `--days`/`--since`/`--until`, `--all`, `--archived`, `--limit`, `--merge`, `--json` |
| `rl usage session <id>` | One session's spend: turns, split, cache rate, top turns. | `--json` |
| `rl usage windows` | 5h/weekly windows. Codex from disk; Claude needs `rl install statusline`. | |
| `rl usage export` | Write usage records out, to `--merge` elsewhere. | `--out` |
| `rl harvests` | The pending harvest queue. | `--json` |
| `rl doctor` | Per-agent report: skills, hooks, statusline, retention. | `--settings` |

`--status` accepts `active`, `unfinished`, `blocked`, `finished`, `abandoned`, `harvested`,
`unclassified`, or `all`; it is repeatable (OR). `-a`/`--all` is a legacy alias for
`--status all`.

In the default project rollup, `total` matches the `rl triage` count for each project. An
explicit status view counts every matching session instead.

## Setup

`rl install <noun>`, `rl update <noun>`, `rl uninstall <noun>`. Bare `rl install` does `all`.
Each noun takes `--yes`.

| Noun | Wires up | install | update | uninstall |
|---|---|---|---|---|
| `skills` | the bundled `ringleader-*` skills, symlinked into each agent's skills dir | ✓ | ✓ | ✓ |
| `statusline` | the Claude Code `statusLine` snippet that captures rate-limit windows | ✓ | — | ✓ |
| `hooks` | the session-end hook that archives finished sessions | ✓ | ✓ | ✓ |
| `all` | every noun above | ✓ | ✓ | ✓ |

`rl install statusline` also takes `--settings PATH`.

## `rl session`

Mostly called by the skills, not by hand.

**Scan and classify**

| Command | Does |
|---|---|
| `rl session refresh` | Reconcile the registry against disk; emit sessions to classify. `--tail`, `--project` |
| `rl session set` | Classify one session by hand. `--session`, `--status`, `--what`, `--name`, `--confidence` |
| `rl session enrich` | Classify pending sessions via the configured model. No-op with no model. `--project`, `--limit` |
| `rl session name` | Apply/refresh session names. `--project`, `--limit`, `--active-window`, `--dry-run` |

**Finish and file away**

| Command | Does |
|---|---|
| `rl session harvest [<id>\|--current]` | Distill a session into markdown and mark it harvested. `--from-file <path\|->` stores agent-authored markdown as-is; `--dry-run` prints instead of writing. |
| `rl session finish [<id>\|--current]` | Mark `finishOnExit` — leaves triage now, archives when `sweep` finds the transcript quiet. Refuses an unharvested session unless `--force`. **Archives nothing itself.** |
| `rl session sweep` | Archive every `finishOnExit` thread whose transcript stopped changing. `--session <id>`, `--from-hook`, `--current`, `--grace` |
| `rl session complete <id>` | Harvest, then archive. No archive if the harvest was a no-op. |
| `rl session archive <id>` | Move the transcript to `archive/`, out of the resume picker. `--summary-file`, `--archive-root` |
| `rl session unarchive <id>` | Undo an archive; the session is resumable again. |
| `rl session delete <id>` | Delete one session's files, leaving a `deleted` record. |

**Park and pin**

| Command | Does |
|---|---|
| `rl session snooze <id>` | Hide from the default view but keep it live. `--until <iso>`, `--for <days>` |
| `rl session wake <id>` | Un-snooze. |
| `rl session keep <id>` | Keep long-term: stays live, leaves triage for good. `--yes` |
| `rl session unkeep <id>` | Undo a keep; back to the triage inbox. |

**Bookkeeping**

| Command | Does |
|---|---|
| `rl session mark harvested <id>` | Record a session as harvested. `--paths a,b` records the docs. |
| `rl session mark reviewed <id>` | Skip an open session until it changes. |
| `rl session mark cleaned <selector>` | Record a project as just-triaged. `--at <iso>` |
| `rl session relocate <id> <path>` | Re-point one record. **Registry only** — storage stays put, so the session is not resumable from the new path. |
| `rl session whoami` | Print the source and session id `rl` resolves for the session it's running inside. Backs `--current`. |

## `rl mem`

Backs the `memory-triage` skill.

| Command | Does |
|---|---|
| `rl mem refresh` | Reconcile + emit memory entries to review. `--project` |
| `rl mem prune` | Delete obsolete-candidate entries. **Previews unless `--delete`.** `--project` |
| `rl mem triage` | The memory-review inbox. `--project`, `--no-scan` |
| `rl mem overview [<selector>]` | Memory rollup, or a `<selector>` deep-dive. `--project`, `--no-scan` |
| `rl mem mark cleaned <selector>` | Record a project's memory as just-triaged. |
| `rl mem mark reviewed <path>` | Mark one entry reviewed. |

## `rl data`

| Command | Does |
|---|---|
| `rl data export <id>` | Render a session to markdown. `--out` |
| `rl data tokens` | `rl usage` pinned to `--source claude`; same flags otherwise. |
| `rl data migrate [<old_dir>]` | One-time move of legacy repo-root state into `~/.local/share/ringleader/`. Safe to re-run. |

## `rl workspace`

| Command | Does |
|---|---|
| `rl workspace move <old> <new>` | Move the directory, its session storage, and the registry. Confirms first; **refuses rather than merges** into a destination that already holds session storage. `--dry-run`, `--yes`, `--allow-empty` |
| `rl workspace retarget <old> <new>` | The last two halves, for a directory you already moved by hand. Makes `move` re-runnable after a partial failure. `--dry-run`, `--yes` |

## `rl config`

`rl config show` prints the current values and the resolved active model.
`rl config set <key> <value>` accepts:

| Key | Values |
|---|---|
| `ai` | `claude` \| `codex` \| `llm` — engine for `enrich`/`harvest` |
| `model` | model name for the chosen engine |
| `llm.provider` | provider for the `llm` engine |
| `archive.retention_days` | non-negative int; `0` = keep archives forever |

`roundup_stale_hours` (default 8) is read from the same file but **cannot be set with
`rl config set`** — edit `config.json` under the state dir to change it.

Stores no secrets — the `claude`/`codex` engines reuse that CLI's own auth.

## Conventions

- A **`<selector>`** is a git-project label (`ringleader`) or a cwd path — never a session id.
- **`--project` is repeatable** wherever it appears, and aliases the positional selector on
  `overview`.
- **`overview` and `triage` scan the disk first** unless you pass `--no-scan`.
- **`prune` and `mem prune` preview by default**; only `--delete` removes anything. `rl roundup`
  runs prune *with* deletion.
- State lives in `~/.local/share/ringleader/` (`$XDG_DATA_HOME/ringleader` if set), not the repo.

## Known rough edges

- `rl config --help` and `rl session mark --help` print `unknown subcommand` instead of help;
  both hand-roll their dispatch. Use `rl config` / `rl session mark` bare to list subcommands.
