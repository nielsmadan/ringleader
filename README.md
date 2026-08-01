# ringleader

Curates the **dormant** Claude Code sessions sitting on disk
(`~/.claude/projects/*.jsonl`) — naming them, classifying what state they're
in, surfacing what's still in-flight vs. abandoned, and pruning the rest
before the GC clock silently deletes it. Run the dispatcher as `./rl <command>`
from this repo — it's not installed on `PATH`.

## Everyday

The three front-door commands, most useful first.

### `rl overview [<selector>] [-s] [-a] [--archived] [--checkouts] [--no-scan]`

The view. Auto-refreshes the registry (a disk scan) before rendering, unless
`--no-scan` is passed. Prints a `⚠ N awaiting triage` header when the inbox
is non-empty.

- (no args) — project rollup, the default. The `cleaned` column is the last
  `rl cleaned` (session triage); `mem` is the last `rl mem-cleaned` (memory
  triage), showing `—` where the project has no memory entries at all.
- `-s`, `--sessions` — flat per-session list of live threads instead of the rollup.
- `<selector>` — one-repo deep-dive (a git project label like `juggler`, or a cwd path).
- `--archived` — list archived sessions instead of live ones.
- `-a`, `--all` — include parked/snoozed sessions and every status.
- `--checkouts` — rollup only: also list each repo's checkout paths.
- `--no-scan` — skip the auto-scan, report from the registry as-is.

### `rl triage [--project <sel>] [--no-scan]`

Lists sessions awaiting your decision — not yet reviewed, harvested, or
parked. Auto-refreshes the registry first, unless `--no-scan`. `--project`
scopes the inbox to one repo/dir selector (repeatable).

### `rl prune [--project <sel>] [--delete]`

Deletes tiny + stale sessions. Preview by default; pass `--delete` to
actually delete. `--project` scopes the prune to one repo/dir selector.

## Plumbing

Mostly invoked by the skills (`session-sync`, `session-overview`,
`session-name`, `session-harvest`, `session-triage`), not by hand.

| Command | Does |
|---|---|
| `rl export <id> [--out <path>]` | Render a session to markdown. |
| `rl archive <id> [--summary-file <md>]` | Move a finished session to `archive/` (out of the resume picker). |
| `rl delete <id>` | Delete one session (files + tombstone). |
| `rl mark-harvested <id> [--paths a,b]` | Mark a session harvested (records docs). |
| `rl mark-reviewed <id>` | Mark an open session reviewed (skip until it changes). |
| `rl cleaned <selector>` | Record a project as just-triaged. |
| `rl relocate <id> <path>` | Re-point a session after moving its dir. |
| `rl snooze <id>` | Park a session (hide from default views). |
| `rl wake <id>` | Un-snooze a session. |
| `rl sync scan [--project <sel>]` | Reconcile + emit sessions to classify. |
| `rl name [--project <sel>] [--dry-run]` | Apply/refresh session names. |

## Restoring an archived session

While a session sits in `archive/` it is out of `~/.claude/projects/`, so `claude --resume`
cannot see it at all. There is no `rl unarchive` — but archiving only *moves* files and
edits the registry, so it is reversible by hand. `<slug>` below is the encoded-cwd directory
name (e.g. `-Users-you-wrksp-oss-Foo`), identical on both sides.

1. Move **both** `archive/<slug>/<id>.jsonl` and its sidecar directory
   `archive/<slug>/<id>/` back into `~/.claude/projects/<slug>/`. Leaving the sidecar
   behind still resumes, but drops the session's `subagents/` and `tool-results/` data.
   Check the live side doesn't already hold either name first — `rl archive` aborts on that
   collision and the reverse move deserves the same care.
2. Delete the session's `reason: "archived"` tombstone from `registry.json`. Re-discovery
   does not depend on this: `reconcile` re-adds any scanned session it has no thread for,
   tombstone or not. But a stale tombstone keeps the session listed by
   `rl overview --archived` forever while it is also live.
3. Re-scan — `./rl overview`, or any command run without `--no-scan`.
4. Optionally delete the leftover `archive/<slug>/<id>.md` harvest summary and the emptied
   archive directory.

Then `cd <project> && claude --resume`. **The thread's registry record does not come back.**
Archiving pops it, so the session returns as a brand-new unclassified, unnamed thread — its
name, `whatItIs`, decision, tags, `harvestPath` and reviewed/classified markers are gone.
Re-run the `session-sync` and `session-name` skills to rebuild them. Both `archive/` and
`registry.json` are git-ignored, so none of this touches tracked files.

## Selectors

`<selector>` = a git-project label (e.g. `juggler`) or a cwd path.
