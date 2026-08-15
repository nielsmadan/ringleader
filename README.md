# ringleader

Curates the **dormant** Claude Code sessions sitting on disk
(`~/.claude/projects/*.jsonl`) — naming them, classifying what state they're
in, surfacing what's still in-flight vs. abandoned, and pruning the rest
before the GC clock silently deletes it.

## Install

```sh
just install-local        # migrates any legacy repo-root state, then `uv tool install .`
```

or directly: `uv tool install .`. Either way this puts `rl` on `PATH` as a real
console command — run it as `rl <command>` from anywhere, not `./rl`. To run
from a source checkout without installing, use `uv run rl <command>`.

State (the registry and `archive/`) lives under `~/.local/share/ringleader/`
(or `$XDG_DATA_HOME/ringleader` if set) — not in this repo. `just install-local`
runs `rl data migrate` first to move any pre-existing repo-root `registry.json` /
`archive/` into that location.

## Skills

`.agents/skills/` ships four agent skills that drive `rl` instead of raw commands —
`ringleader-overview`, `ringleader-init`, `ringleader-triage`, `ringleader-cleanup`:

- **`ringleader-overview`** — the morning read: what's in flight, what's stale and
  awaiting triage, what's snoozed, and an offer to jump into triage when the inbox is old.
- **`ringleader-init`** — onboard a machine: verify `rl` is installed, check
  session-retention settings (`rl doctor`), offer to wire the skills into your agent(s),
  and run a small, bounded first triage pass (`--recent 7`).
- **`ringleader-triage`** — recurring cleanup of the last few days of sessions:
  delete/finished/open per session, harvest what's finished into the repo's docs, name and
  review what's open.
- **`ringleader-cleanup`** — the same flow as triage over the *entire* backlog instead of a
  recent window; resumable across runs, since the registry records what's already been
  triaged.

Each is an agent-neutral `SKILL.md` plus a bundled `references/playbook.md` (synced from
the canonical `.agents/skills/PLAYBOOK.md`), so they read natively in Claude Code, Codex,
OpenCode, and Pi — no per-agent plugin format required. Wire them into your agent(s) with:

```sh
rl skills install     # symlink the bundled skills into ~/.claude/skills and ~/.agents/skills
rl skills status      # show what's linked for each detected agent
rl skills update      # re-sync after the bundled skill set changes
rl skills uninstall   # remove the symlinks this tool created
```

`install` and `uninstall` ask for confirmation before touching anything; pass `--yes` to
skip the prompt. See `.agents/skills/SMOKE.md` for the manual checklist used to exercise
the skills end to end.

## Everyday

The front-door commands, most useful first.

### `rl inbox [--registry <path>]`

A terminal UI (inbox-zero flow) for blitzing through dormant sessions. Needs the
optional `[tui]` extra (`pip install ringleader[tui]`); without it, prints an install
hint and exits non-zero.

On entry, if a roundup hasn't run within `roundup_stale_hours` (config, default 8) —
or never — it offers to run `rl roundup` first; on **y** it runs on a background
worker and fires a desktop notification when done, then drops you into the inbox.

Three tabs, cycled with `tab`/`shift+tab`: **inbox** → **selected** → **harvesting**.

- **inbox** — undecided live sessions, one row per session (`project: description`).
  `j`/`k` move the cursor. `h` harvests (distills) the session — an empty digest
  archives it immediately, a non-empty digest moves it to **harvesting**. `d` stages
  a delete, applied on exit; `u` undoes the last staged delete. `s` snoozes for a
  fuzzy duration (`2h`, `3d`, `2w`, `1mo`, `tomorrow`, `next monday`, or a bare
  integer = days). `enter` selects the session for today, moving it to **selected**.
  `;` opens a detail view (project, status, whatItIs, last prompt, git branch,
  condensed transcript) — `h` returns to the list.
- **selected** — sessions picked for today; `u`/`enter` un-selects (back to **inbox**).
- **harvesting** — sessions with an un-integrated digest (harvested but not yet
  archived), shown with a preview of their digest; `a` marks it integrated and
  archives it.

### `rl roundup [--yes] [--registry <path>]`

A combined maintenance verb: **refresh** (scan + reconcile), **prune** (delete tiny +
stale sessions), then **enrich** (LLM-classify the newly-pending sessions), then
records `lastRoundupAt`. Unlike `rl prune`, a bare `rl roundup` deletes rather than
previews. `rl inbox` runs this automatically on entry when stale; it's also a
standalone command. `--yes` is accepted (the inbox passes it after its own
confirm prompt) but is currently a no-op — `rl roundup` itself never prompts. If no
model is configured, the enrich step is a no-op and refresh + prune still run.

### `rl overview [<selector>] [-s] [-a] [--archived] [--checkouts] [--no-scan] [--json]`

The view. Auto-refreshes the registry (a disk scan) before rendering, unless
`--no-scan` is passed. Prints a `⚠ N awaiting triage` header when the inbox
is non-empty.

- (no args) — project rollup, the default. The `cleaned` column is the last
  `rl session mark cleaned` (session triage); `mem` is the last
  `rl mem mark cleaned` (memory triage), showing `—` where the project has no
  memory entries at all.
- `-s`, `--sessions` — flat per-session list of live threads instead of the rollup.
- `<selector>` — one-repo deep-dive (a git project label like `juggler`, or a cwd path).
- `--archived` — list archived sessions instead of live ones.
- `-a`, `--all` — include parked/snoozed sessions and every status.
- `--checkouts` — rollup only: also list each repo's checkout paths.
- `--no-scan` — skip the auto-scan, report from the registry as-is.
- `--json` — print the registry-wide rollup as JSON instead of text; ignores the
  selector and other view flags.

For the memory rollup instead of the session rollup, use `rl mem overview`
(same flags: `[<selector>] [--no-scan]`).

### `rl triage [--project <sel>] [--no-scan] [--json]`

Lists sessions awaiting your decision — not yet reviewed, harvested, or
parked. Auto-refreshes the registry first, unless `--no-scan`. `--project`
scopes the inbox to one repo/dir selector (repeatable). `--json` prints the
inbox as JSON instead of text (honors `-u`/`--unclassified`).

For the memory-review inbox instead of the session inbox, use `rl mem triage`
(same flags: `[--project <sel>] [--no-scan]`).

### `rl prune [--project <sel>] [--delete]`

Deletes tiny + stale sessions. Preview by default; pass `--delete` to
actually delete. `--project` scopes the prune to one repo/dir selector.

### `rl harvests [--json]`

Lists the durable knowledge harvested out of completed sessions (`rl session
harvest`/`rl session complete`) — folder path, repo url, and content for
each. `--json` prints the full list (including `content`) as JSON instead of
text. Harvests are stored as markdown files under `state_dir()/harvests/`
(`~/.local/share/ringleader/harvests/` by default); `rl harvests` is the
inbox the `ringleader-*` skills read from to fold that knowledge into a repo's docs.

## Plumbing

Mostly invoked by the `ringleader-*` skills (`overview`, `init`, `triage`, `cleanup`, plus
the separate `memory-triage` skill for the memory track), not by hand.

Commands are grouped under `rl session`, `rl mem`, `rl data`, and
`rl config`. `rl <group>` with no subcommand lists that group's verbs.

| Command | Does |
|---|---|
| `rl session refresh [--project <sel>]` | Reconcile + emit sessions to classify. |
| `rl session set` | Classify a session (whatItIs/decision/tags). |
| `rl session enrich [--project <sel>] [--limit N]` | Classify pending sessions (status/whatItIs/name) with a configured LLM — a tool-native alternative to classifying by hand in the `triage`/`cleanup` skill flow. No-op if no model is configured. |
| `rl session archive <id> [--summary-file <md>]` | Move a finished session to `archive/` (out of the resume picker). |
| `rl session delete <id>` | Delete one session (files + tombstone). |
| `rl session harvest <id> [--dry-run]` | Distill a finished session into a durable markdown harvest (summary/gotchas/decisions/whatChanged) with a configured LLM, store it, and mark the session harvested. `--dry-run` prints the markdown instead of writing it. No-op if no model is configured. |
| `rl session complete <id>` | Harvest, then archive — the tool-native "I'm done with this session" verb. Does not archive if the harvest was a no-op (no model configured). |
| `rl session snooze <id>` | Park a session (hide from default views). |
| `rl session wake <id>` | Un-snooze a session. |
| `rl session name [--project <sel>] [--dry-run]` | Apply/refresh session names. |
| `rl session relocate <id> <path>` | Re-point a session after moving its dir. |
| `rl session mark harvested <id> [--paths a,b]` | Mark a session harvested (records docs). |
| `rl session mark reviewed <id>` | Mark an open session reviewed (skip until it changes). |
| `rl session mark cleaned <selector>` | Record a project as just-triaged. |
| `rl mem refresh` | Reconcile + emit memory entries to review. |
| `rl mem prune` | Delete obsolete-candidate memory entries. |
| `rl mem triage [--project <sel>] [--no-scan]` | The memory-review inbox. |
| `rl mem overview [<selector>] [--no-scan]` | Memory rollup (or `<selector>` deep-dive). |
| `rl mem mark cleaned <selector>` | Record a project's memory as just-triaged. |
| `rl mem mark reviewed <name>` | Mark a memory entry reviewed. |
| `rl data export <id> [--out <path>]` | Render a session to markdown. |
| `rl data tokens` | Report token/size stats. |
| `rl data migrate` | One-time move of legacy repo-root `registry.json`/`archive/` into `~/.local/share/ringleader/`. Run by `just install-local`; safe to re-run (no-op once state already lives there). |
| `rl config show` / `rl config set <key> <value>` | Select the LLM backend for `enrich` (`backend` = `claude`/`codex`/`api`, plus `model`, `api.provider`). Stores no secrets. |

## LLM backend

`rl session enrich` classifies sessions by calling a model through a small `Model`
seam (`ringleader/models/`). By default it **auto-picks the first installed agent CLI**
(`claude`, then `codex`) and drives it one-shot, reusing that CLI's own auth — so
ringleader stores **no API keys or secrets**. `rl config set backend <claude|codex|api>`
pins a choice. For a direct API backend instead of an agent CLI, install the extra
(`pip install ringleader[api]`, which pulls in the `llm` library — key managed by `llm`,
not ringleader) and `rl config set backend api`. When no backend is available, `enrich`
is a no-op and classifying sessions by hand — the `triage`/`cleanup` skill flow's
per-session `[SELECT]` step — remains the way to do it.

## Sources

Scanning is behind a `Source` protocol (`ringleader/sources/`): `discover()` lists
raw session refs cheaply (path + mtime/size, no parse), `parse()` reads one into a
`NormalizedSession`. Claude Code sessions come from `~/.claude/projects/*.jsonl`;
Codex sessions come from `~/.codex/sessions/**/*.jsonl`, with native names read from
`~/.codex/session_index.jsonl`. Both sources support native name writeback.

## Restoring an archived session

While a session sits in `archive/` it is out of `~/.claude/projects/`, so `claude --resume`
cannot see it at all. There is no `rl unarchive` — but archiving only *moves* files and
edits the registry, so it is reversible by hand. `<slug>` below is the encoded-cwd directory
name (e.g. `-Users-you-wrksp-oss-Foo`), identical on both sides.

1. Move **both** `archive/<slug>/<id>.jsonl` and its sidecar directory
   `archive/<slug>/<id>/` back into `~/.claude/projects/<slug>/`. Leaving the sidecar
   behind still resumes, but drops the session's `subagents/` and `tool-results/` data.
   Check the live side doesn't already hold either name first — `rl session archive` aborts
   on that collision and the reverse move deserves the same care.
2. Delete the session's `reason: "archived"` tombstone from `registry.json`. Re-discovery
   does not depend on this: `reconcile` re-adds any scanned session it has no thread for,
   tombstone or not. But a stale tombstone keeps the session listed by
   `rl overview --archived` forever while it is also live.
3. Re-scan — `rl overview`, or any command run without `--no-scan`.
4. Optionally delete the leftover `archive/<slug>/<id>.md` harvest summary and the emptied
   archive directory.

Then `cd <project> && claude --resume`. **The thread's registry record does not come back.**
Archiving pops it, so the session returns as a brand-new unclassified, unnamed thread — its
name, `whatItIs`, decision, tags, `harvestPath` and reviewed/classified markers are gone.
Re-run the `triage` or `cleanup` skill (which classifies and names sessions as part of its
flow) to rebuild them. Both `archive/` and
`registry.json` live under `~/.local/share/ringleader/`, outside this repo, so none of
this touches tracked files.

## Selectors

`<selector>` = a git-project label (e.g. `juggler`) or a cwd path.
