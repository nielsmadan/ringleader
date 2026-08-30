# 0010: Keep machine-local state outside the repo

## Context

`registry.json` and `archive/` originally sat at the repo root. That was convenient while the
tool was a set of repo-local scripts and wrong as soon as it became an installed console
command: the state describes *this machine's* sessions, while the repo is source code that gets
cloned, moved, and checked out more than once.

Archived transcripts made it worse — real work sitting in a directory a `git clean` would
remove.

## Decision

All runtime state lives under `~/.local/share/ringleader/` (`$XDG_DATA_HOME/ringleader` when
set), resolved on every command through `ringleader/paths.py`:

```
registry.json     the hybrid cache: threads, decisions, archived, deleted
archive/          transcripts moved out of the agents' stores
harvests/         the pending harvest queue
publish/receipts/ recovery state while publishing into a repo
```

`rl data migrate` performs the one-time move of legacy repo-root state and is a no-op once
state already lives there; `just install-local` runs it.

## Consequences

- The repo holds only source and skills; multiple checkouts of ringleader share one registry,
  which is correct — there is one machine's worth of sessions.
- `paths.py` is the single place that knows where anything lives, and tests point it elsewhere.
- State is outside the working tree, so it survives `git clean` and is never accidentally
  committed.
