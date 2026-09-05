# Move a checkout without losing its sessions

> **Problem:** agents bind a session to the directory it was *launched* in — Claude Code by
> the storage path it files the transcript under, Codex by a cwd column its resume picker
> filters on. Move a workspace and its whole history becomes unresumable from the new path —
> and a plain `rl` scan makes it worse by reverting the registry to the stale path baked into
> the transcript.

## Overview

`rl workspace move` changes **three things at once**:

1. the directory on disk;
2. the agent's session storage, in whichever shape the source keeps it — Claude Code keys
   it by launch cwd, so the slug directory is renamed; Codex files rollouts by date and
   records the cwd in its own state db, so nothing moves and that entry is rewritten in
   place;
3. the registry — which is then **pinned**, or the next scan reverts it.

```sh
rl workspace move <old> <new>        # all three
rl workspace retarget <old> <new>    # the last two, for a directory you moved by hand
```

Both preview and confirm; `--dry-run` stops after the plan, `-y` skips the prompt.

## Why pinning is required

A transcript records the cwd it was launched in, permanently. After a move that value is stale,
so an ordinary scan would read it and revert the registry to the old path on the very next run.
`move` and `retarget` set `locationPinned`, which tells reconcile to leave the location alone.

## It refuses rather than merges

If the destination already holds session storage, the move **refuses**. Merging would require
rewriting a session id — the registry's primary key — and a half-merged store cannot be safely
undone. `--allow-empty` covers the opposite case: proceeding when no sessions match.

## Build artifacts are reported, not rewritten

A move invalidates anything inside the tree that baked in the old absolute path — `.venv`,
`node_modules`, `target`. The preview names each one and the command that regenerates it, and
that is all it does. The line is who else can fix it: an editable `.pth` under
`~/.local/share/uv/tools` lives *outside* the tree and only this command knows the move
happened, so it is repointed; a virtualenv has a package manager that owns it.

## Re-runnable after a partial failure

`retarget` is exactly the last two halves of `move`. If a move fails after relocating the
directory, run `retarget` to finish the job — which is why the operation is decomposed this way
rather than being one atomic verb.

## One session at a time

`rl session relocate <id> <path>` is `retarget` scoped to a **single** session: it re-points the
registry record *and* takes the agent's own session storage with it, through the same source
capabilities — a rebinding source has its recorded cwd rewritten, a relocating source has that one
transcript moved. Use `workspace retarget` for a whole directory. `--registry-only` re-points the
record alone, for storage already moved by other means.

A source with neither capability (opencode, pi) is not an error: the record is still worth
re-pointing, and the command says it went registry-only.

## Key entry points

- `commands/move_workspace.py` — `cmd_move()`, `cmd_retarget()`.
- `commands/relocate.py` — `cmd_relocate()`; carries the same pin-vs-refuse decision for
  one session, and is re-runnable because it skips a transcript already at the destination.
- `sources/base.py` — `WorkspaceRelocatingSource` (`storage_for`, `relocate_storage`,
  `relocate_session`); both relocate methods raise rather than merge.
- `sources/base.py` — `WorkspaceRebindingSource` (`rebind_workspace`); rewrites the agent's
  recorded cwd in place, so nothing collides and no transcript path changes.
- `registry.pin_thread_location()` / `remap_cached_git_root()`.
- `slug.py` — reproduces Claude Code's directory encoding to find the storage.

## Gotchas

- **`move` and `retarget` scan before planning.** The plan is built from the registry, so a
  session started or forked since the last scan would otherwise be left behind at the old path —
  the freshest session is the likeliest to be missing. `--no-scan` opts out. The planning scan is
  never written: `_retarget_registry` re-runs it inside the lock when the run commits, so a dry
  run, a refusal or a declined confirmation leaves the registry untouched, and a write that landed
  while the confirmation prompt was open survives.
- **`slug.py` reimplements a private function** of Claude Code's. It can break on an upgrade —
  re-verify against [reference/agent-session-management.md](../reference/agent-session-management.md).
- **Sources with their own index need the picker cleared first**, or it points at a moved file.

## Related

- [tech/scanning-and-refresh.md](../tech/scanning-and-refresh.md) · [tech/sources.md](../tech/sources.md)
