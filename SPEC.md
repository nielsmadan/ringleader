# ringleader — Design Spec

**Date:** 2026-06-07
**Status:** Approved design, pre-implementation
**Author:** brainstormed with Claude

## One-line

A tool that curates the **dormant** Claude Code sessions sitting on disk — naming them, classifying what state they're in, surfacing what's still in-flight, capturing what mattered before it's deleted, and deliberately pruning the rest.

## Problem

Claude Code stores every session as a `<uuid>.jsonl` transcript under
`~/.claude/projects/<encoded-cwd>/`. Today there are ~219 real sessions across ~60
project dirs (~917 MB). Concrete pains:

1. **Silent loss.** Sessions are garbage-collected by mtime after
   `cleanupPeriodDays` (default 30). Work started and forgotten gets deleted. Raising
   retention just lets the folder bloat — a dead end as stated.
2. **Resume friction.** Only ~46/219 sessions have a custom title; the other ~173 show
   up in the picker as an auto-summary or first-prompt blob, making the right one hard
   to find.
3. **No sense of spread.** There's no view of "everything I have in flight / abandoned"
   across all projects, and no signal for how thin I'm spread.
4. **Knowledge evaporates.** Solved bugs, decisions, and context live in transcripts
   that eventually get culled.

### Relationship to juggler

`juggler` (existing tool, hook-based) watches sessions that are **running right now**.
`ringleader` is the complement: it curates the **dormant** state on disk — sessions
where no process is running. The two do not overlap, and ringleader must **not**
depend on or extend juggler.

## Goals

- See all dormant work threads across all projects at a glance, with what each is about,
  its state, its age, and its project.
- Get a "how spread am I" / WIP-debt readout.
- Make naming easy enough that `claude --resume <name>` becomes a reliable lookup.
- Never lose a valuable session to the GC clock again — without hoarding raw transcripts.
- Keep everything self-contained and under the user's control.

## Non-goals

- **Live/running session monitoring** — that's juggler's job.
- **An accomplishment / "what I shipped" log** — that's better sourced from git
  (`time-reconstruct`, `summary`). The only bridge: harvest closeouts (below) capture the
  non-code / didn't-ship work that git can't see.
- **Replacing native CC features.** We lean on native naming (`claude -n`, `/rename`,
  `Ctrl+R`), cross-project picker views (`Ctrl+A`/`Ctrl+W`/`Ctrl+B`), resume-by-name
  (`claude --resume <name>`), resume-by-uuid, and `claude project purge`.

## Key facts about the CC session store (verified)

- Each session = one `<uuid>.jsonl`; subagents nest under `<uuid>/subagents/`.
- Records of interest inside a transcript:
  - `{"type":"custom-title","customTitle":"…","sessionId":"…"}` — user-set name (last one wins).
  - `{"type":"summary","summary":"…","leafUuid":"…"}` — auto summary.
  - `{"type":"last-prompt","lastPrompt":"…","sessionId":"…"}` — most recent user prompt.
- Picker label priority: **custom-title → auto-summary → first prompt**.
- GC (`cleanupPeriodDays`): default 30, min 1, **cannot be disabled** (0 invalid); mtime-based.
- ⚠️ **Hand-editing/appending to `.jsonl` is undocumented and unsupported.** Naming MUST
  go through native mechanisms (`claude -n`, `/rename`, picker `Ctrl+R`), **not** by writing
  our own `custom-title` records. The registry stores names we *propose*; applying them is
  surfaced to the user / done via native commands.
  **Superseded during Phase 2:** appending one `custom-title` record at EOF *is* how naming
  works — CC writes dozens of those records itself and last-wins, so an append matches its
  own behavior. See [session-name design](docs/specs/2026-06-10-session-name-design.md).
- ⚠️ **`/clear` keeps the same session and the same file** — it only empties in-memory
  context. So **one file can contain multiple work threads separated by `/clear`
  boundaries**. A new file is created only by a fresh `claude` launch. `/compact` rewrites
  the current file with a summary.
- **`.session-aliases` controls picker grouping** (verified Jul 2026, CC 2.1.215). Each
  `~/.claude/projects/<slug>/` may hold a hidden `.session-aliases` file — a newline list of
  *other* slug-dir paths. The picker unions the current dir's sessions with every dir listed
  there, so *all* of an aliased project's sessions show up under an unrelated cwd, even in
  default (non-`Ctrl+A`) scope. Undocumented; it is **not** git identity, cwd, remote,
  root-commit, name-hash, submodule, or `~/.claude.json`. Best assessment is that CC writes it
  when a session is given access to another dir (most likely `/add-dir`). Inspect with
  `find ~/.claude/projects -maxdepth 2 -name .session-aliases` (the slug paths start with `-`,
  which breaks bare `cp`/`rm`/`ls`). Deleting the file only re-groups the picker and touches no
  session data.

## Architecture

### Approach C — hybrid registry (cache, not master)

`registry.json` is the source of truth for **decisions and history**, but it is a **cache**
of disk reality, reconciled cheaply on each run:

1. Enumerate top-level `*.jsonl` (exclude `*/subagents/*`); read cheap metadata only
   (mtime, size).
2. For each file that is **new** or whose **mtime changed** since last classification:
   read the tail (+ detect `/clear` boundaries), (re)classify, refresh metadata.
   Unchanged files keep their cached classification — no LLM cost.
3. Files that vanished from disk → moved to `deleted` (preserving any decision /
   harvest pointer).
4. **User decisions are never silently overwritten** by re-classification.

This is the only approach that supports both "remember I abandoned this" and trend metrics
("how spread am I over time") without re-reading everything every run.

### Skills-first, CLI-later

The user will **experiment with skills first**, then crystallize into a CLI once happy.

- **Now (experiment):** capabilities are CC **skills** living in this repo's `skills/`.
  The skill agent does the smart work (reading tails, classifying, naming, harvesting)
  directly; deterministic scanning is a bundled script. Skills read the absolute path
  `~/.claude/projects`, so they work regardless of cwd; they are only *discoverable* when
  CC runs with this repo as cwd (acceptable for experimentation; symlink into
  `~/.claude/skills` if global access is wanted sooner).
- **Later (crystallize) — realized.** The validated behavior now lives in a packaged CLI
  (`rl`, a `pyproject.toml` console-script entry point, installed via `uv tool install .` /
  `just install-local`); the LLM parts (classification, naming, harvesting) stay in the
  skills, which now just call `rl <sub>` instead of a repo-local script. Scanning itself
  moved behind a `Source` seam (`ringleader/sources/`) so it isn't hardwired to Claude
  Code's transcript format. See `docs/plans/2026-08-02-tooling-and-packaging.md` and
  `docs/specs/2026-08-02-tooling-and-packaging-design.md` for the crystallization plan.

### The lifecycle classifier (engine, ①)

Reads the **tail** of a transcript (and notes `/clear` boundaries — classify the latest
segment for status). Produces, per session:

- `status`: one of `active | unfinished | blocked | finished | abandoned`
  - `blocked` = last turn was a question/error awaiting the user.
  - `abandoned` = unfinished **and** untouched beyond a staleness threshold.
- `statusConfidence`: 0–1.
- `whatItIs`: one-line description of the work.
- `suggestedName`: kebab-case, ≤ ~40 chars (a *proposal*; applied via native naming).

During experimentation the skill agent classifies inline (batched / via parallel subagents
for the one-time backfill of ~219 files; incremental runs touch only changed files).

### Homes

Code and runtime state now live in separate places — the repo holds only source and
skills; the CLI's state is machine-local under the user's home dir, resolved at every
command via `ringleader/paths.py`.

```
~/wrksp/incubation/ringleader/            # the project repo (source; installed elsewhere via `rl`)
├── SPEC.md                                 # this file
├── ringleader/                             # the package (installed as the `rl` console script)
│   ├── paths.py                            # state_dir() / default_registry() / archive_root()
│   ├── sources/                            # Source seam: discover()/parse() per transcript store
│   │   └── claude_code.py                  # ~/.claude/projects/*.jsonl (the only source today)
│   └── commands/                           # one module per `rl <sub>` dispatch entry
└── .claude/skills/                         # skills that shell out to the installed `rl`
    ├── session-overview/
    ├── session-sync/
    ├── session-name/
    ├── session-harvest/
    ├── session-triage/
    └── memory-triage/

~/.local/share/ringleader/                # state_dir() — $XDG_DATA_HOME/ringleader if set
├── registry.json                           # hybrid cache: threads + decisions + archived/deleted
└── archive/                                # archived sessions moved out of ~/.claude/projects/
```

`registry.json` and `archive/` are machine-local, derived data — never committed, and no
longer inside the repo at all (no `.gitignore` entry needed for them there). There is no
`harvests/` directory: `session-harvest` integrates knowledge directly into the *target*
repo's own docs (via the `/doc` skill) and records the resulting doc paths on the thread's
`harvestPath`, rather than writing a ringleader-local copy. `rl data migrate` moves any
pre-refactor repo-root `registry.json`/`archive/` into `state_dir()` once, on install.

## registry.json schema (v1)

```jsonc
{
  "version": 1,
  "lastScan": "<iso timestamp, passed in by caller>",
  "threads": [
    {
      "sessionId": "uuid",
      "file": "/abs/path/to/<uuid>.jsonl",
      "project": "/decoded/cwd",            // decoded from the encoded dir name
      "title": "custom-title or null",      // native title if present
      "summary": "auto summary or null",
      "lastPrompt": "string or null",
      "gitBranch": "branch or null",        // from transcript records
      "gitProjectKey": "remote-slug | repo-root | null",  // grouping identity
      "gitProjectLabel": "repo name or null",
      "gitRoot": "/repo/toplevel or null",
      "gitRemote": "host/org/repo or null",
      "mtime": "<iso>",
      "sizeBytes": 0,
      "messageCount": 0,
      "clearBoundaries": 0,                 // count of /clear within the file (Phase 2)
      "status": "active|unfinished|blocked|finished|abandoned",
      "statusConfidence": 0.0,
      "whatItIs": "one-line description",
      "suggestedName": "kebab-case-name or null",
      "classifiedAtMtime": "<iso mtime when last classified>",
      "decision": "keep|abandon|harvested|null",   // user decision; never auto-overwritten
      "harvestPath": "comma-joined target-repo doc paths, or null",
      "todoId": "todoist id or null",       // reserved for Phase 3
      "tags": []
    }
  ],
  "deleted": [   // transcript is gone; `archived` is the separate, restorable list
    { "sessionId": "uuid", "reason": "prune|deleted|gc|purged",
      "deletedAt": "<iso>", "harvestPath": "… or null" }
  ],
  // Separate list: the transcript still exists in archive/ and is restorable with
  // `rl session unarchive`, so these are NOT deletions.
  "archived": [
    { "sessionId": "uuid", "archivedAt": "<iso>", "archivePath": "…",
      "originalPath": "…", "thread": { /* the full record, for restore */ },
      "summary": "…", "harvestPath": "… or null" }
  ]
}
```

> Note: keep timestamp generation at the edge (the skill/agent or CLI entrypoint) and pass
> values into pure scan/reconcile logic, so that logic stays deterministic and testable.

## Capabilities → skills

| # | Skill | Does | Maps to |
|---|-------|------|---------|
| 1 | `session-sync` | Scan disk, reconcile registry (hybrid C), classify changed sessions, record vanished ones as deleted. | engine ① |
| 2 | `session-overview` | Render dormant threads grouped by **git project** (worktrees/clones/monorepo-subdirs collapse together; cwd is the fallback) and status; show spread / WIP-debt metrics, oldest-untouched, blocked-waiting-on-me. | #1, ④ |
| 3 | `session-name` | Propose names for unnamed sessions (batch retroactive + current); surface them for the user to apply via native `/rename` / `claude -n` / `Ctrl+R`. | #2 |
| 4 | `session-harvest` | Distill a chosen session/thread (what was done, key decisions, files touched, where it stopped, follow-ups) and integrate it into the *target repo's* own docs via the `/doc` skill; record the touched doc paths on `harvestPath` and mark `decision=harvested`. | ②, #3 |
| 5 | `session-triage` | Guided cleanup: run `rl prune` for the tiny+stale junk, then walk the triage inbox (`rl triage`) session-by-session to delete, harvest, or leave open. | #3 |
| 6 | `memory-triage` | Per-project curation: auto-prune obsolete memory entries via `rl mem prune`, classify survivors, integrate durable items into repo's `CLAUDE.md`/`AGENTS.md`, mark project cleaned. Flags global-worthy items for manual integration. | #5 |

### Retention strategy (resolves the core dilemma)

- Set `cleanupPeriodDays` high (e.g. **365**) in `~/.claude/settings.json` so the clock
  never silently deletes anything.
- Replace time-based loss with **deliberate curation**: `session-harvest` captures the
  knowledge, then `session-prune` removes the raw transcript on purpose.
- Valuable threads persist (as harvests, forever); dead threads get culled intentionally.

### Memory curation (Phase 2+)

Parallel to session curation, ringleader curates the per-project **auto-memory** stored
under `~/.claude/projects/*/memory/` — a `MEMORY.md` index plus per-topic entry files.
`memory.py` scans these entries, detects obsolete signals (repo gone, FIXED/DONE markers,
already-integrated into the repo's docs), and feeds them into the registry as `memoryEntries`
(schema v2). The `memory-triage` skill orchestrates cleanup: deterministic plumbing commands
(`rl mem refresh`, `rl mem prune`, `rl mem mark reviewed`, `rl mem mark cleaned`) handle scanning,
preview/deletion of obsolete entries, and state tracking; the skill agent classifies
survivors and integrates durable items into the repo's `CLAUDE.md` or `AGENTS.md` (if
present). Entries flagged as globally useful are reported only — never auto-edited into
global config. Read access and triage are via `--memory` modes of the existing `rl overview`
and `rl triage` commands.

## Build phases

- **Phase 1 — Core experiment (delivers the headline value):**
  `session-sync` + `session-overview`. The classifier + registry + the "see everything
  in-flight / abandoned + how spread am I" view. (engine ①, #1, ④)
- **Phase 2 — Curation:** `session-name`, then `session-harvest` + `session-prune` +
  the `cleanupPeriodDays` bump. (#2, #3, ②)
- **Phase 3 — Todo linking (later):** populate `todoId`; reconcile sessions vs Todoist
  (untracked work / unstarted todos) via the Todoist MCP. (#4)
- **Later — Crystallize:** migrate validated skills into the `rl` CLI; optional thin
  global wrapper skill.

## Open questions / decisions deferred to implementation

- Staleness threshold for `abandoned` (proposal: untouched > 21 days **and**
  `status != finished`).
- Tail size the classifier reads (proposal: last ~30 messages or ~8 KB, whichever first;
  plus first user prompt for context).
- Whether `session-overview` writes a refreshed `lastScan` or relies on `session-sync`
  having run (proposal: overview triggers a sync if registry is stale).
  **Resolved:** `rl overview` and `rl triage` now auto-refresh (a structural disk scan)
  on every read, unless `--no-scan` is passed.
- `.gitignore` policy confirmation. **Resolved (superseded):** `registry.json` and
  `archive/` moved out of the repo entirely, into `~/.local/share/ringleader/` (see
  Homes above); there is no `harvests/` to decide on, since harvesting now writes into
  the target repo's own docs instead of a ringleader-local copy.

## Parked

Considered and deliberately not built (yet):

- **`/clear`-boundary splitting** — needs `uuid`/`parentUuid`/`leafUuid` preservation to be safe.
- **Todoist ↔ session linking** — the Phase 3 item above; parked, not scheduled.
- ~~**Putting `rl` on `PATH`**~~ — **done**: `rl` is now a `pyproject.toml` console-script
  entry point, installed via `uv tool install .` / `just install-local`.
- **A `.session-aliases` sweep** — see the key-facts bullet above; would make stale picker
  grouping self-heal instead of needing a manual `find` + delete.
