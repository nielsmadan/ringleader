# Keep what a session taught before it disappears

> **Problem:** the durable value of a finished session is a handful of gotchas and decisions
> buried in a long transcript. Archiving without extracting them throws the value away silently.

## Overview

A **harvest** is markdown distilled out of a session — summary, gotchas, decisions, what
changed. It lands in a machine-local queue, and is later **published** into a repository's living
documentation.

```sh
rl session harvest <id>              # distill via the configured model
rl session harvest <id> --dry-run    # print the markdown instead of storing it
rl harvests                          # the pending queue
rl session complete <id>             # harvest, then archive
```

## Two ways in

- **Model-distilled** — `rl session harvest <id>` reads the transcript and calls the configured
  engine. A no-op when no model is available.
- **Agent-authored** — `rl session harvest --current --from-file <path|->` stores markdown
  as-is, skipping the model. This is what the `ringleader-finish` skill uses from *inside* a live
  session, harvesting from context rather than re-reading a transcript later.

`rl session mark harvested <id> [--paths a,b]` records a harvest that happened elsewhere, for
when an agent integrated the knowledge directly into docs.

## Publishing

`rl harvests` is the inbox of what has been distilled but not yet integrated. The
`ringleader-publish` skill drains it into repository documentation, guarded:

- refuses a checkout with an operation in progress (merge, rebase, cherry-pick, revert);
- refuses to write `AGENTS.md` or `CLAUDE.md`;
- rejects any path that escapes the checkout;
- records a receipt first, so an interrupted publish can be resumed rather than half-applied.

## `harvestPath` holds two kinds of path

The field is an array and may contain both at once:

- **queue item paths** — tool-native harvests still pending under the harvest store;
- **living document paths** — where an agent already wrote the knowledge.

Publishing clears only the queue entries, using the harvest store root to tell them apart.
Living-document paths survive as a permanent record of where the knowledge went.

## Key entry points

- `commands/harvest.py` — `cmd_harvest()`, including `--current` and `--from-file`.
- `harvests.py` — the queue: `write_harvest()`, `snapshot_harvests()`.
- `publish_state.py` / `publish_git.py` — receipts and git guards.

## Gotchas

- **The queue is durable only until integration** — it is a work queue, not an archive.
- **A malformed harvest becomes a listed problem**, not an exception; `rl harvests` shows it.
- **`finish` refuses an unharvested session** ([0006](../decisions/0006-harvest-before-finish.md)).

## Related

- [tech/publish.md](../tech/publish.md) · [tech/models.md](../tech/models.md)
- [session-lifecycle.md](session-lifecycle.md)
