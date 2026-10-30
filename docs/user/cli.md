# CLI reference

Every `rl` command at a glance. `rl <command> --help` is authoritative for flags.

From a source checkout without installing, prefix each call with `uv run`.

Bare `rl` in an interactive terminal opens `rl inbox`. In a pipe, with `TERM=dumb`, or inside an
agent session it prints usage.

```
rl
├── inbox                       TUI home: triage, resume, roundup, prune, harvests, usage
├── roundup                     refresh + sweep + prune + enrich, plan first
├── overview [<selector>]       the view: project rollup, or a deep-dive
├── triage                      sessions awaiting a decision
├── prune                       delete tiny + stale sessions (preview by default)
├── usage                       token usage across claude/codex/droid/opencode/pi
│   ├── session <id>            where one session's tokens went
│   ├── windows                 5h/weekly subscription limits
│   └── export                  dump usage for merging across machines
├── analyze [<selector>]        where sessions went wrong and where tokens went
│   ├── session <id>            one session in detail
│   ├── issues                  recurring error signatures across sessions
│   └── reports [save]          saved findings and recommendations
├── harvests                    inbox of knowledge harvested out of sessions
├── doctor                      what's linked, registered, how retention is set
├── install | update | uninstall  <skills|statusline|hooks|all>
├── config                      show | set <key> <value>
├── session <sub>               session plumbing (see below)
├── mem <sub>                   memory plumbing (see below)
├── data <sub>                  export | tokens | migrate | presence
├── gui <sub>                   machine interface for the circus app (see below)
└── workspace <sub>             move | retarget
```

`rl <group>` with no subcommand lists that group's verbs.

## Everyday

| Command | Does | Key flags |
|---|---|---|
| `rl resume [<query>]` | Textual picker over every agent's sessions, most recent first, filtered as you type: an exact name first, then a name prefix, a name substring, a fuzzy name/project-label/session-id-prefix match, and last a session whose summary or prompt contains every typed word. `ctrl+j`/`ctrl+k` move, `ctrl+t` widens to this tree, `ctrl+r`/`F5` refresh, `ctrl+a` to everywhere, `enter` picks, `escape` or `ctrl+c` quits. A `<query>` that is an unambiguous session-id prefix, or a qualified `<source>:<sessionId>`, skips the picker; any other text pre-fills the filter. Then resume the existing session (default), fork a new session in its own agent, or hand it to another — `--to` tries a native conversion first and asks before falling back to a Markdown handoff; `--markdown` skips straight to the handoff. | `--last`, `--to claude\|codex\|droid\|opencode\|pi`, `--markdown`, `--dry-run`, `--all`, `--recursive`, `--registry`, `--operations`, `--verified` |
| `rl inbox` | The daily home TUI; bare `rl` in a terminal opens it. Home shows what's waiting and the next step. `1`-`7` jump to Home, Projects, Resume, Roundup, Prune, Harvests, Usage; `escape`/`ctrl+c` back, `q` quit (also `ctrl+c` on Home), `u` undo, `ctrl+r` rescan, `ctrl+p` palette. In triage: `a` archive, `h` harvest, `g` keep going, `s` snooze, `K` keep, `d` delete (undo within 10 s), `space`/`*` mark, `enter` detail, `r` resume, `t` triage with an agent. | `--read-only`, `--registry` |
| `rl roundup` | Scan, print the plan, then archive finished sessions, prune (**deletes**), purge expired archives and enrich. Each session is rechecked before it is touched; Ctrl-C stops at the next boundary and exits 130. Over 25 pending sessions it asks which to enrich on a terminal, else enriches the last 7 days'. | `--enrich all\|recent\|none`, `--preview`, `--yes`, `--registry` |
| `rl overview [<selector>]` | Project rollup by default; `<selector>` deep-dives one repo. | `-s/--sessions`, `--status`, `--archived`, `--snoozed`, `--kept`, `--checkouts`, `--cwd`, `--source`, `--recent`, `--no-scan`, `--json` |
| `rl triage` | Sessions not yet reviewed/harvested/parked. | `-u/--unclassified`, `--project`, `--source`, `--recent`, `--no-scan`, `--json` |
| `rl prune` | Delete tiny + stale sessions. **Previews unless `--delete`.** | `--delete`, `--older-than` (7), `--max-turns` (12), `--project` |
| `rl usage` | Token usage from local transcript files and agent databases. | `--by day\|model\|project\|session\|source`, `--breakdown`, `--source`, `--days`/`--since`/`--until` (`YYYY-MM-DD`), `--all`, `--archived`, `--limit`, `--merge`, `--json` |
| `rl usage session <id>` | One session's spend: turns (main and subagent), split, cache rate, top main-thread turns. | `--json` |
| `rl usage windows` | 5h/weekly windows. Codex from disk; Claude needs `rl install statusline`. | |
| `rl usage export` | Write usage records out, to `--merge` elsewhere. | `--out` |
| `rl analyze [<selector>]` | Index Claude and Codex transcripts and report failure signals, health, cache rebuilds and token rollups. Refreshes a SQLite index first; the first run reads every transcript. Default window is the last 7 days of data. On a terminal labels default to `--label recent` with a cost estimate and a confirmation; an agent session, a pipe, a redirected stderr or `--json` labels nothing unless `--label` is passed. `--until` alone reads all history up to that day. `--source` takes `claude` or `claude-code`. | `--project`, `--source`, `--days`/`--since`/`--until`, `--all`, `--archived`, `--by project\|folder\|skill\|plugin\|mcp_server\|tool\|model\|agent`, `--worst`, `--issues`, `--label recent\|all\|none`, `--yes`, `--no-refresh`, `--rebuild`, `--index`, `--registry`, `--json` |
| `rl analyze session <id>` | One session: outcome, health, signals, episodes with evidence, messages, compactions, context growth, skills, tools, subagents. | `--label`, `--yes`, `--no-refresh`, `--rebuild`, `--index`, `--registry`, `--json` |
| `rl analyze issues` | Error signatures that recur across sessions. Merges near-duplicates when the `embed` extra and its model are present. | scope flags as above, `--min-sessions` (2), `--limit` (20), `--download-embed-model`, `--no-refresh`, `--rebuild`, `--index`, `--registry`, `--json` |
| `rl analyze reports` | List saved reports, newest first. `--json` prints `{"schema", "reports": [...]}`. | `--kind findings\|recommendations`, `--limit`, `--json` |
| `rl analyze reports save <kind> <file\|->` | Store a report under the state dir. Exits 2 on an unreadable file, an empty body or an unknown `--based-on` id. `--json` prints `{"schema", "report_id", "path"}`. | `--title`, `--window`, `--based-on` (repeatable), `--json` |
| `rl harvests` | The pending harvest queue. | `--json` |
| `rl doctor` | Per-agent report: skills, hooks, statusline, retention. `--verify-conversion [AGENT]` runs the offline self-verification harness for installed conversion targets (or one named agent) and records a pass. | `--settings`, `--verify-conversion [AGENT]`, `--verified` |

`--status` accepts `active`, `unfinished`, `blocked`, `finished`, `abandoned`, `harvested`,
`unclassified`, or `all`; it is repeatable (OR). `-a`/`--all` is a legacy alias for
`--status all`.

In the default project rollup, `total` matches the `rl triage` count for each project. An
explicit status view counts every matching session instead.

`rl analyze` exits 0 on success. It exits 1 when more than 20% of changed transcripts failed to
parse, the index is unreadable, the index dir cannot be written and holds no current index, a
session is missing or ambiguous, `--download-embed-model` could not load the model, or an explicit
`--label recent|all` did not complete: no model, a labels store it cannot write or that a newer rl
wrote, every call failed, or the breaker tripped. A missing or ambiguous session is reported on
stderr; a missing one points to `rl usage session <id>`, since Droid, OpenCode and Pi sessions have
no analysis data.
It exits 2 on a usage error before any refresh: `--rebuild` with `--no-refresh`, `--days` with
`--all`, `--since` or `--until`, `--since` later than `--until`, or a bare-word selector that
matches no registered project (a word close to a subcommand, such as `issue`, gets a "did you
mean"). It exits 3 when `--no-refresh` finds no complete index, or one a different rl version
built. With `--json`, an index that cannot be used prints an error document whose `index.reason`
is `missing`, `mismatch`, `unreadable` or `unwritable`.
`--index DIR` points the index, its lock and the labels at another directory; saved reports and
the Codex start cache stay under the state dir. For a trial run on real data, pair `--index` with a
temporary `XDG_DATA_HOME`.

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

OpenCode supports the read-only session verbs (`refresh`, classification, harvest and data
export) plus registry-only parking decisions. File lifecycle verbs (`finish`, `complete`,
`sweep`, `archive`, `unarchive`, `delete`, and prune deletion) decline OpenCode sessions.

**Find**

| Command | Does |
|---|---|
| `rl session search <query>` | Find sessions by name. Case-insensitive substring, or an **anchored** glob when the query holds `*`/`?` — quote it, or the shell eats it. Hits are ranked: names first, then a session-id prefix, then the cached blurbs. Archived and name-filtered sessions are included and labelled. `--live`, `--archived`, `--limit` (default 50), `--json`, `--color auto\|always\|never` |

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
| `rl session relocate <id> <path>` | Re-point one session at a directory it moved to: its record and the agent's session storage, so it stays resumable. `--registry-only` |
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
| `rl data presence install` | Register Ringleader with Hooklinesinker and install status hooks. Confirms first; `--agent` is repeatable and defaults to detected agents. `--yes`, `--json` |
| `rl data presence status` | Report the active and bundled binaries, consumer registration, hooks, running-session count, and problems. `--json` |
| `rl data presence uninstall` | Remove Ringleader's consumer registration. Hooks and the shared binary remain while other consumers are registered. Confirms first. `--yes`, `--json` |

Presence installation and uninstallation require `--yes` when combined with `--json`.
`just install` and `just install-editable` also run presence
installation after installing Ringleader. A direct `uv tool install .` leaves activation
to the explicit presence command or onboarding skill.

## `rl gui`

| Command | Does |
|---|---|
| `rl gui list` | The inbox as one JSON document. |
| `rl gui scan` | Rescan sources; report sessions added and removed. |
| `rl gui tail <id>…` | The last part of up to 16 transcripts. `--max-chars` |
| `rl gui decide <action> <id>` | Apply one decision. `--mtime` (required), `--fingerprint`, `--until` |

`rl gui` is the machine interface of the circus desktop app: JSON on stdout, versioned, not for
hand use. See [tech/gui-contract.md](../tech/gui-contract.md).

## `rl workspace`

| Command | Does |
|---|---|
| `rl workspace move <old> <new>` | Move the directory, its session storage, and the registry. Confirms first; **refuses rather than merges** into a destination that already holds session storage. `--dry-run`, `--yes`, `--allow-empty`, `--no-scan`. Scans first so a session forked since the last scan is not left behind. Build artifacts inside the tree are reported, never rewritten. |
| `rl workspace retarget <old> <new>` | The last two halves, for a directory you already moved by hand. Makes `move` re-runnable after a partial failure. `--dry-run`, `--yes`, `--no-scan` |

## `rl config`

`rl config show` prints the current values and the resolved enrich, harvest and analyze models.
`rl config set <key> <value>` accepts:

| Key | Values |
|---|---|
| `ai` | `claude` \| `codex` \| `llm` — engine for `enrich`/`harvest` and `rl analyze` labels |
| `model` | model name for the chosen engine |
| `ai.command_prefix` | command the `claude`/`codex` engines launch through, shell-split (`sops-exec`); `none` clears it |
| `enrich.model` | model for `enrich`; `default` clears it (claude engine then uses `haiku`) |
| `analyze.model` | model for `rl analyze` labels; `default` clears it (claude engine then uses `haiku`) |
| `llm.provider` | provider for the `llm` engine |
| `archive.retention_days` | non-negative int; `0` = keep archives and handoff exports forever. Above `0`, `prune --delete` and `roundup` also remove `handoffs/*.md` exports older than this many days (an unscoped `prune` only) |

`roundup_stale_hours` (default 8) is read from the same file but **cannot be set with
`rl config set`** — edit `config.json` under the state dir to change it.

Stores no secrets — the `claude`/`codex` engines reuse that CLI's own auth.

### `rl config filter`

Hide sessions whose name matches a glob — see
[Filtering noisy sessions](../features/views-and-triage.md#filtering-noisy-sessions).

```sh
rl config filter add '[cadet-city]*'   # everything named "[cadet-city] ..."
rl config filter list
rl config filter remove '[cadet-city]*'
```

Patterns are anchored to the whole name: `*` matches any run of characters, `?` matches one,
and every other character — brackets included — is literal, so the `[cadet-city]` prefix above
means what it looks like rather than a character class. Quote the pattern so the shell leaves
it alone.

## Conventions

- A **`<selector>`** is a git-project label (`ringleader`) or a cwd path — never a session id.
- **`--project` is repeatable** wherever it appears, and aliases the positional selector on
  `overview`.
- **`overview` and `triage` scan the disk first** unless you pass `--no-scan`.
- **`session search` never scans.** It reads the registry as it stands, so run
  `rl session refresh` first if a session created minutes ago is missing.
- **`prune` and `mem prune` preview by default**; only `--delete` removes anything. `rl roundup`
  runs prune *with* deletion.
- State lives in `~/.local/share/ringleader/` (`$XDG_DATA_HOME/ringleader` if set), not the repo.

## Known rough edges

- `rl config --help` and `rl session mark --help` print `unknown subcommand` instead of help;
  both hand-roll their dispatch. Use `rl config` / `rl session mark` bare to list subcommands.

## Session portability

These commands read the current registry without scanning. Run `rl session refresh`
first when a source is absent. IDs accept exact native IDs, `AGENT:ID`, then unambiguous
prefixes; `claude` aliases `claude-code`. Names are not ID selectors.

```sh
rl session convert <id>... --to <agent> [--compact] [--dry-run] [--json]
rl session convert --open --to <agent> [--from <agent>] [--workspace <path>]
rl session convert --running --to <agent> [--from <agent>] [--workspace <path>]
rl session fork <id> [--to <agent>] [--compact] [--resume] [--json]
rl session handoff <id> --to <agent> [--compact] [--json]
rl session resume <id> [--json]
rl session lineage <id> [--json]
rl data transaction list [--json]
rl data transaction resume <transaction-id> [--json]
rl data transaction rollback <transaction-id> [--json]
```

Supported agent names are `claude-code`, `codex`, `opencode`, `pi`, and `droid`.
Conversion preserves the source and creates a destination session in its effective
recorded workspace. Repeating a conversion reuses the same result; an archived or
deleted result is reported with its lifecycle and is not recreated. Fork always
creates a fresh identity and defaults to the source agent. Handoff forks and then
resumes. `--compact` deterministically replaces complete tool exchanges with
omission markers and reports approximate sizes/counts; it makes no model call.

Explicit IDs, `--open`, and `--running` are mutually exclusive. `--open` selects
sessions whose effective status is active, unfinished, or blocked. It excludes
currently snoozed and name-filtered sessions, and includes kept sessions with an
open status. `--running` requires a complete authoritative presence response and
selects verified running sessions regardless of lifecycle, snooze, or name filters.
Unavailable or partial presence fails with setup guidance. `--from` filters the
source agent. `--workspace` filters resolved workspaces at or below that directory;
it does not retarget output. Explicit IDs may select hidden or archived histories;
archived bytes are read in place. A batch retains successes and exits nonzero if
any source fails. JSON conversion output has `results` and `errors` arrays.

Resume replaces `rl` with the native CLI, preserving terminal input/output and the
inherited environment. It uses the recorded effective workspace and, for converted
sessions, recorded native roots, setting a root variable only where the recorded root differs
from what the agent resolves from that environment. Fork with `--resume` and handoff validate the
executable and workspace before publication. A later launch failure retains the
target and prints its qualified ID and exact `rl session resume` command. Archived
and deleted sessions cannot be resumed.

`--dry-run`, lineage, transaction listing, and the conversion sections of `rl doctor`
do not write registry, journals, or native stores. Version probes use disposable
roots. Doctor reports writer compatibility for its current cwd/native roots and unfinished
conversion journals; success there does not establish compatibility in another
workspace. Batch preflight resolves each source workspace separately.

Every target version is either pinned from upstream source research or
self-verified on this machine; `rl doctor --verify-conversion [AGENT]` runs the
offline harness and records the result in `--verified` (default
`verified-targets.json` under the ringleader state directory). An unverified,
unpinned target version refuses with the command to run. OpenCode's profile is
additionally schema-backed and requires an initialized compatible database and
supported explicit model configuration; its native runtime request construction
remains unverified even once self-verified (CLI-acceptance tier only).

Transaction resume performs forward recovery. Rollback removes only provably
private staging and refuses after external publication may have occurred. Use the
transaction ID printed by conversion or `rl data transaction list`. Repeat the
original `--registry` override when recovering an operation: journals bind the
resolved registry path, and a mismatch fails with the required path before any
publication or cleanup.

All these commands accept `--json`, `--registry PATH` (default
`$XDG_DATA_HOME/ringleader/registry.json`, or `~/.local/share/ringleader/registry.json`),
`--operations PATH` (default `conversion/` under the ringleader data directory,
independent of a `--registry` override), and `--verified PATH` (default
`verified-targets.json` under the ringleader state directory). JSON controls rl's
output; a successful resume then hands output to the native CLI. `--compact` defaults
off; `--dry-run` exists only on convert and defaults off. Convert requires `--to`;
fork defaults `--to` to the source agent and defaults `--resume` off; handoff requires
`--to` and always launches. Convert's `--from` and `--workspace` filters apply with
any selector, and its `--compact`, `--dry-run`, and `--json` flags apply to all batch
forms. Lineage requires an ID; transaction resume/rollback require an exact operation
ID, while transaction list takes no positional arguments.

Native roots follow `HOME` and `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`,
`XDG_DATA_HOME`, `XDG_STATE_HOME`, and `TMPDIR`, with their native defaults. Agent
stores follow `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, and `PI_CODING_AGENT_DIR`.
Scanner overrides `SESSION_TRACKER_PROJECTS_DIR`, `CODEX_SESSIONS_DIR`, and
`PI_SESSIONS_DIR` can select a native store only when they end in `projects`,
`sessions`, and `sessions`, respectively, and agree with any explicit agent root.
Arbitrary leaf overrides remain readable source locations but cannot be native
publication or launch destinations. OpenCode uses `XDG_DATA_HOME/opencode/opencode.db`;
an explicit `OPENCODE_DB` must agree for publication or launch. Read-only OpenCode
sources may use a copied database at any recorded path or `OPENCODE_DB` path,
including a different filename; schema and snapshot validation still apply. Imported
sessions retain their native roots for later scans, naming and lifecycle actions,
even when the current environment selects another store.
