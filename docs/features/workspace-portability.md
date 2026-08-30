# Move a checkout without losing its sessions

> **Problem:** Claude Code keys session storage by the directory a session was *launched* in.
> Move a workspace and its whole history becomes unresumable from the new path — and a plain
> `rl` scan makes it worse by reverting the registry to the stale path baked into the transcript.

## Overview

`rl workspace move` changes **three things at once**:

1. the directory on disk;
2. the agent's session storage (the slug directory is renamed);
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

`rl session relocate <id> <path>` re-points a **single** registry record. It does **not** move
session storage, so the session stays unresumable from the new path. For a whole directory, use
`workspace retarget`, which does both.

## Key entry points

- `commands/move_workspace.py` — `cmd_move()`, `cmd_retarget()`.
- `sources/base.py` — `WorkspaceRelocatingSource` (`storage_for`, `relocate_storage`,
  `relocate_session`); both relocate methods raise rather than merge.
- `registry.pin_thread_location()` / `relocate_thread()`.
- `slug.py` — reproduces Claude Code's directory encoding to find the storage.

## Gotchas

- **`rl session relocate` is registry-only** and leaves the session unresumable. Easy to reach
  for by mistake.
- **`slug.py` reimplements a private function** of Claude Code's. It can break on an upgrade —
  re-verify against [reference/agent-session-management.md](../reference/agent-session-management.md).
- **Sources with their own index need the picker cleared first**, or it points at a moved file.

## Related

- [tech/scanning-and-refresh.md](../tech/scanning-and-refresh.md) · [tech/sources.md](../tech/sources.md)
