# Curate accumulated agent memory

> **Problem:** agent memory files accumulate across every project — obsolete facts, notes that
> belong in the repo's docs, duplicates of what the code already says.

## Overview

A parallel track to the session pipeline, over memory entries instead of transcripts, with the
same scan/reconcile/triage shape.

```sh
rl mem refresh                  # reconcile + emit entries to review
rl mem triage                   # the memory-review inbox
rl mem overview [<selector>]    # rollup, or a per-project deep dive
rl mem prune                    # delete obsolete candidates (preview by default)
rl mem mark reviewed <path>     # one entry handled
rl mem mark cleaned <selector>  # a project just triaged
```

State lives in the same registry, under `memoryEntries` and `memoryProjectsCleanedAt`.

## Driven by a skill

These are plumbing for the `memory-triage` skill, which reads the inbox from `rl mem triage` and
classifies each survivor: delete, integrate into the repo's docs, flag as global, or keep. It
accepts an optional repo/dir scope.

## Gotchas

- **`rl mem prune` previews by default**, like `rl prune`; only `--delete` removes anything.
- **`memory-triage` still lives in `loadout/skills/`**, not with the agent-neutral
  `ringleader-*` skills.

## Key entry points

- `memory.py` — scan and classification helpers.
- `commands/mem_scan.py`, `mem_prune.py`, `mem_cleaned.py`, `mark_mem_reviewed.py`.
- `registry.reconcile_memory()`, `memory_needing_review()`.

## Related

- [tech/registry.md](../tech/registry.md) · [views-and-triage.md](views-and-triage.md)
