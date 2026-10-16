# See what's in flight and decide what's next

> **Problem:** dozens of dormant sessions across many repos, with no way to see which projects
> are sprawling, which threads are blocked on you, and which have never been looked at.

## Overview

Three read surfaces over the same registry, at different altitudes:

| Command | Answers |
|---|---|
| `rl overview` | where is my work spread — a project rollup |
| `rl triage` | what awaits a decision — a flat inbox |
| `rl inbox` | the same queue, interactively |

All three auto-scan the disk first; `--no-scan` reports the registry as-is.

## `rl overview`

Project rollup by default, grouped by **git project** — worktrees, clones and monorepo subdirs of
one repo collapse into a single row, keyed by normalized remote (or repo root when there is no
remote). Sessions with no resolvable git project group by cwd.

The default rollup's `total` is the number of sessions still awaiting a triage decision, using
the same rules as `rl triage`. Projects with reviewed open work remain visible with `total` zero.
Explicit status views such as `--status all` count all matching sessions instead.

```sh
rl overview                    # rollup across every project
rl overview ringleader         # deep-dive one repo (a git label or a cwd path)
rl overview --sessions         # flat per-session list
rl overview --cwd              # scope to the current directory and below
rl overview --status blocked   # filter by effective status (repeatable, OR)
rl overview --archived         # archived instead of live
rl overview --snoozed          # currently snoozed
rl overview --kept             # kept long-term
rl overview --checkouts        # rollup plus each repo's checkout paths
rl overview --recent 7         # only sessions active in the last N days
```

`--status` accepts the stored classifications plus the derived `abandoned`, `harvested` and
`unclassified`, or `all`. `-a` / `--all` is a legacy alias for `--status all`.

## `rl triage`

The decision inbox: sessions that are not reviewed, harvested or parked. `-u` narrows it to
sessions that were **never** classified. Both take `--project`, `--source`, `--recent` and
`--json`.

## `rl inbox`

A Textual TUI over the same queue, in three tabs (`tab` / `shift+tab`): **inbox** (undecided),
**selected** (picked for today), **harvesting** (digested but not yet archived).

`j`/`k` move, `h` harvest, `d` stage a delete, `u` undo, `s` snooze (fuzzy: `3d`, `2w`,
`next monday`), `K` keep long-term, `a` archive an integrated digest, `enter` select for today,
`;` detail, `q` quit.

On entry it offers to run `rl roundup` when `lastRoundupAt` is older than `roundup_stale_hours`
(default 8), on a background worker with a desktop notification when done.

## `rl roundup`

Refresh, then archive finished sessions (sweep), prune (**this deletes**), purge expired
archives, and enrich, in one pass. It is the "make everything current" verb, and what `rl inbox`
triggers on stale state.

It scans first, then prints the plan — how many sessions each phase will touch, the enrich
model, and an estimated duration — and acts only on the sessions that plan named. Each one is
rechecked right before it is touched: a sweep skips a transcript written in the last two
minutes, a prune skips a session whose registry entry or transcript changed since the plan, and
both skip a session presence reports as running (when presence is not installed, roundup
proceeds as before). Skips print with their reason.

```sh
rl roundup                     # scan, print the plan, run it
rl roundup --preview           # scan and print the plan only
rl roundup --enrich recent     # all | recent | none — which pending sessions to classify
rl roundup --yes               # never prompt
```

Enrichment over 25 pending sessions asks for a scope on a terminal (`[a]ll / [r]ecent / [s]kip`,
recent = active in the last 7 days); without a terminal, or with `--yes`, it enriches the recent
ones and prints the command for the rest. The roundup is stamped (`lastRoundupAt`) whatever the
enrich scope, so skipping enrich does not leave it stale.

Ctrl-C stops at the next session or batch boundary (a second Ctrl-C aborts outright), exits
130 and leaves the roundup unstamped. Two roundups never overlap: the second exits 1 with
`another roundup is running`.

## Filtering noisy sessions

Some tools spawn agent sessions by the dozen — a fan-out orchestrator, a scheduled job — and
each one lands in the registry as another thread to triage. `rl config filter` hides them by
name ([syntax](../user/cli.md#rl-config-filter)).

A match is stamped as `filteredBy` at scan time and drops the session out of `rl overview`,
`rl triage`, `rl inbox` and the rollup counts, and out of `rl session enrich`, which would
otherwise spend a model call classifying noise. Nothing is deleted: `rl overview --filtered`
lists what is hidden and why, and `--include-filtered` puts the sessions back into any
browsing view. Edits take effect on the next scan, which every default view runs for you.

`rl session search` deliberately does not honour the filter: a query you typed is a request
for a specific session, so filtered hits are returned with a `[filtered]` label rather than
withheld.

## Selectors

A `<selector>` is a git-project label (`ringleader`) or a cwd path — **never a session id**.
`--project` is the repeatable alias, accepted across the views.

## Key entry points

- `overview.py` + `commands/overview.py` — the rollup and its filters.
- `commands/triage.py` — the inbox query.
- `projects.py` — `matches()`, `filter_by_projects()`, `project_summary()`.

## Gotchas

- **Scanning dominates runtime** on a large `~/.claude/projects`; both views scan by default.
- **`rl roundup` deletes.** It runs prune with deletion enabled, unlike bare `rl prune`.
  `--preview` shows the plan without running it, but its scan still persists.
- **Filtering hides, it does not protect.** `rl prune` still deletes a filtered session from
  disk when it is tiny and stale, even though another tool wrote it.

## Related

- [tech/scanning-and-refresh.md](../tech/scanning-and-refresh.md) · [tech/inbox.md](../tech/inbox.md)
- [user/triage-workflow.md](../user/triage-workflow.md)
