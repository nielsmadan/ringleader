# Delete what isn't worth keeping

> **Problem:** most dormant sessions are three-message dead ends. They bury the ones that matter,
> and reviewing each by hand costs more than they are worth.

## Overview

Two independent deletion paths:

- **`rl prune`** — remove live sessions that are both **tiny** and **stale**.
- **Archive purge** — permanently delete archived sessions once `archive.retention_days` elapses.

```sh
rl prune                       # preview only
rl prune --delete              # actually delete
rl prune --older-than 14 --max-turns 5
rl prune --project ringleader
```

## Preview by default

`rl prune` prints what it *would* remove and exits. Only `--delete` removes anything. The
defaults are `--older-than 7` days and `--max-turns 12` messages; a session must exceed the age
**and** fall under the turn ceiling to qualify.

`rl roundup` runs prune **with deletion enabled** — it is the one everyday verb that deletes
without a separate flag.

Deletion rechecks each session against disk first: when the transcript's size or modification
time differs from what the last scan recorded, the session is skipped with `session changed
since selection` rather than deleted. Activity after the scan means the session is no longer
the dead end it looked like. `rl roundup` additionally skips a session presence reports as
running.

OpenCode's SQLite-backed sessions can appear in the preview but are skipped during deletion;
ringleader never mutates `opencode.db`.

Deleting a session removes its **companion files** with the transcript — Claude Code's
`<sessionId>/` directory of subagent and tool-result payloads, droid's `<sessionId>.settings.json`
and `.settings.json.bak` — so a prune leaves no orphaned sidecars behind.

## Archive retention

Archiving moves files rather than deleting them, so archived sessions accumulate. `archive.
retention_days` bounds that:

```sh
rl config set archive.retention_days 90    # 0 (the default) = keep forever
```

Once set, `rl prune` / `rl roundup` purges archived sessions past the cutoff. That purge is
**permanent** — after it, `rl session unarchive` cannot bring the session back, and neither can
a human moving files by hand.

## Tombstones

Deleting records a `deleted` entry with the session id, a reason (`prune`, `deleted`, `gc`,
`purged`), a timestamp, and any `harvestPath`. So a pruned session's *knowledge* survives its
transcript, and a later scan does not re-register it as new.

## Key entry points

- `prune.py` — `prune_candidates()`, `delete_sessions()`, `archive_purge_candidates()`,
  `purge_archived()`; `DEFAULT_OLDER_THAN_DAYS = 7`, `DEFAULT_MAX_TURNS = 12`.
- `registry.prune_session()` / `purge_archived_session()`.

## Gotchas

- **`rl roundup` deletes** — the one place `--delete` is implied.
- **Purge is unrecoverable**, unlike archiving.
- **`--delete` is not a preview flag.** Do not add it to see what a command would do.

## Related

- [tech/registry.md](../tech/registry.md) · [session-lifecycle.md](session-lifecycle.md)
- [user/archives-and-retention.md](../user/archives-and-retention.md)
