# 0008: Separate marking a session finished from archiving it

## Context

The obvious design is for `rl session finish` to archive immediately. It does not work: you
finish a session from *inside* that session, and the agent writes to the transcript again on
the way out. Moving a live transcript means the agent recreates it at the old path, and the
archive silently undoes itself (see [0005](0005-archive-by-moving-not-deleting.md)).

## Decision

Split the intent from the act.

- `rl session finish` records `finishOnExit` on the thread. The session leaves triage
  immediately, and nothing moves.
- `rl session sweep` archives every `finishOnExit` thread whose transcript has stopped
  changing, subject to a grace period.

Sweep runs from `rl roundup` and `rl session refresh` for the eventual path, and from a
session-end hook (`rl session sweep --from-hook`, installed by `rl install hooks`) for the
seconds-after-exit path.

## Consequences

- Finishing is safe from inside the session it finishes — the case that motivated it.
- Archiving is eventually-consistent: without the hook, it lands on the next roundup.
- `finish` doing nothing visible to the filesystem is surprising, so it is called out in
  `AGENTS.md` and in [features/session-lifecycle.md](../features/session-lifecycle.md).
