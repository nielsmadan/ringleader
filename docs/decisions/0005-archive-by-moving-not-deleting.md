# 0005: Archive by moving files, not by deleting them

## Context

A session that is done should leave the resume picker — otherwise the picker fills with
finished work and stops being useful. But "done" is a judgment made from a transcript tail, and
it is sometimes wrong. A destructive archive makes every misjudgement unrecoverable.

The agents give no help here: Claude Code has no archive concept at all, and scopes its picker
to whatever transcripts sit in the project directory.

## Decision

Archiving **moves** the transcript out of the agent's store into
`~/.local/share/ringleader/archive/`, and records the original path in the registry. Because
the picker scans the directory, a moved file is genuinely invisible to `claude --resume` — no
cooperation from the agent required.

`rl session unarchive` moves it back and drops the tombstone. Nothing is deleted until
`archive.retention_days` elapses (default `0` = keep forever), at which point
`rl prune` / `rl roundup` purges it.

## Consequences

- Archiving is reversible, by the tool or by hand — the README documents the manual path.
- Files leave the agent's directory, so any agent-side feature keyed to that location stops
  working on them. This is the point, but it also means archiving is not a display-only flag.
- An agent that writes to a session after we archived it **recreates the transcript**.
  `reconcile()` therefore skips re-registering a session that is in `archived` but absent from
  `threads`, or the next scan would silently undo the archive.
- Sources with their own index need `hide_from_picker` first, or the index is left pointing at
  a moved file — see [0003](0003-scan-behind-a-source-seam.md).
