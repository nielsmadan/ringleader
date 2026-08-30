# 0006: Refuse to finish a session that was never harvested

## Context

The whole point of curating sessions is that some of them taught you something. Archiving is
easy and harvesting takes a model call, so the natural failure mode is a tidy archive full of
threads whose lessons were thrown away — and the loss is silent, because an archived session
looks the same either way.

## Decision

`rl session finish` **refuses** a session with no recorded harvest, and says so. `--force`
overrides it for the case where there genuinely was nothing to learn.

`rl session complete` composes the two in the safe order — harvest, then archive — and does
**not** archive if the harvest was a no-op (no model configured), rather than archiving
something it failed to read.

## Consequences

- The default path cannot silently discard knowledge; discarding it is an explicit `--force`.
- With no model configured, `complete` degrades to doing nothing rather than to a bare archive.
- `finish` marks `finishOnExit` and archives nothing itself — see
  [0008](0008-sweep-archives-only-quiet-transcripts.md).
