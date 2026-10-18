# Move a session from dormant to decided

> **Problem:** transcripts accumulate until the resume picker is unusable and the agent's GC
> clock deletes work you never looked at. Every dormant session needs a decision, and the
> decision needs to stick.

## Overview

Every session ringleader knows about is in exactly one lifecycle state. The states, and the verb
that reaches each:

| State | Set by | Means |
|---|---|---|
| `unclassified` | a scan | seen, never triaged |
| `active` / `unfinished` / `blocked` / `finished` | `rl session set`, `rl session enrich` | classified |
| snoozed | `rl session snooze` | live and resumable, hidden from the default view until `wake` |
| kept | `rl session keep` | live and resumable, out of the triage inbox **for good** |
| `finishOnExit` | `rl session finish` | decided; archives when the transcript goes quiet |
| archived | `rl session archive`, `rl session sweep` | transcript moved out of the agent's store |
| deleted | `rl session delete`, `rl prune` | files gone, tombstone kept |

`abandoned` and `harvested` are **effective** statuses — derived for filtering, not stored as a
classification.

These file lifecycle states apply to Claude Code, Codex, Droid and Pi — every source whose
sessions are files. OpenCode support is read-only: scan, classify, snooze, keep, detail, export
and harvest work, while verbs that would archive or delete a session decline it without mutating
`opencode.db`.

Archiving and deleting carry a session's **companion files** with the transcript — Claude Code's
`<sessionId>/` directory, droid's `<sessionId>.settings.json` sidecars — so an archived session
can be restored whole and a deleted one leaves nothing behind.

## The main path

```sh
rl triage                      # what awaits a decision
rl session harvest <id>        # distill what it taught into markdown
rl session finish <id>         # decided; leaves triage now
rl session sweep               # archives it once the transcript is quiet
```

`rl session complete <id>` composes harvest-then-archive in one verb, and does **not** archive
when the harvest was a no-op.

### Finish does not archive

`finish` marks the thread and moves nothing. Archiving a transcript the agent is still writing
to makes the agent recreate it, silently undoing the archive — so `sweep` does the move once the
file has stopped changing, either on the next `rl roundup` or within seconds via the session-end
hook (`rl install hooks`). See
[0008](../decisions/0008-sweep-archives-only-quiet-transcripts.md).

### Finish refuses an unharvested session

Archiving is cheap and harvesting costs a model call, so the natural failure is an archive full
of threads whose lessons were thrown away. `finish` refuses without a harvest; `--force` is the
explicit override ([0006](../decisions/0006-harvest-before-finish.md)).

## Parking vs. pinning

Two ways to take a session out of the queue without ending it:

- **`rl session snooze <id>`** — hidden from the default view until you `wake` it. `--until <iso>`
  or `--for <days>` auto-wakes. For "not this week".
- **`rl session keep <id>`** — leaves the triage inbox permanently, **even on new activity**.
  For a long-running thread you never want triaged. `rl overview --kept` lists them, `unkeep`
  reverses it.

## Reversibility

Archiving **moves** files and edits the registry; it deletes nothing
([0005](../decisions/0005-archive-by-moving-not-deleting.md)). `rl session unarchive <id>` puts
the transcript back and drops the tombstone, and a human can move the files by hand at the cost
of the registry record (README → "Restoring an archived session").

That holds until `archive.retention_days` elapses (default `0` = keep forever), after which
`rl prune` / `rl roundup` purges the archived files permanently. Handoff exports share the same
setting.

## Key entry points

- `registry.py` — `mark_finish_on_exit()`, `archive_session()`, `restore_session()`,
  `snooze_thread()`, `keep_thread()`, `prune_session()`.
- `commands/finish.py`, `commands/sweep.py`, `commands/complete.py`, `commands/archive.py`.

## Related

- [tech/registry.md](../tech/registry.md) — the fields these states live in
- [harvest-and-publish.md](harvest-and-publish.md) · [pruning-and-retention.md](pruning-and-retention.md)
- [user/finishing-a-session.md](../user/finishing-a-session.md)
