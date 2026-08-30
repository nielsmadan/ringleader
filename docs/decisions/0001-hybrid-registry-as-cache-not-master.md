# 0001: Treat the registry as a cache of disk, not the master record

## Context

Ringleader curates session transcripts it does not own: the agent writes them, and its GC
clock deletes them. Two designs were available. A **master registry** would own the session
list outright and treat disk as an import source — simple to reason about, but it drifts the
moment an agent creates or removes a transcript behind our back, and every drift is a silent
wrong answer. **Re-deriving everything from disk on each run** never drifts, but it cannot
remember anything the transcript does not contain: that you abandoned a thread, that you
already harvested it, or any trend over time.

Neither alone supports the two things the tool exists to do — *remember my decision* and
*show me how spread out I am* — without re-reading every transcript on every run.

## Decision

`registry.json` is the source of truth for **decisions and history**, and a **cache** of
everything else. Each run reconciles it against disk:

1. Enumerate transcripts, reading cheap metadata only (path, mtime, size) — `discover()`.
2. Parse only files that are new or whose fingerprint changed; unchanged files keep their
   cached classification, so re-classification costs no LLM calls.
3. Transcripts that vanished move to `deleted`, preserving any decision or harvest pointer.
4. **User decisions are never silently overwritten** by re-classification.

Timestamps are generated at the edge (CLI entry point) and passed into the scan/reconcile
functions, so that logic stays pure and testable.

## Consequences

- Cheap incremental runs: the common case stats files and parses nothing.
- Cache invalidation becomes a real concern — handled by `fileFingerprint` plus a
  `cacheVersion` parser stamp, so a parser change invalidates every cached parse at once.
- A registry deleted by hand loses decision history but is otherwise self-healing: the next
  scan rebuilds every derivable field.
- The registry can assert things disk cannot (archived, kept, snoozed), so archived sessions
  need explicit protection against a re-scan resurrecting them.

See [tech/registry.md](../tech/registry.md) and [tech/scanning-and-refresh.md](../tech/scanning-and-refresh.md).
