# Archives and retention

## What archiving does

For file-backed Claude Code and Codex sessions, `rl session archive <id>` **moves** the transcript out of the agent's store into
`~/.local/share/ringleader/archive/`. Because Claude Code builds its resume picker by scanning
the project directory, a moved file is genuinely invisible to `claude --resume` — treat
archiving as ending the thread.

Nothing is deleted. `rl session unarchive <id>` moves it back and drops the tombstone.

OpenCode sessions are rows rather than files. Ringleader opens that database read-only, so
archive, unarchive, delete, prune, finish, complete and sweep explicitly decline OpenCode
sessions instead of changing its archival timestamp or deleting rows.

## The retention cutoff

Archived files accumulate. `archive.retention_days` bounds that:

```sh
rl config show
rl config set archive.retention_days 90     # 0 (the default) = keep forever
```

Once set, `rl prune` / `rl roundup` purges archived sessions past the cutoff. **That purge is
permanent** — after it, neither `rl session unarchive` nor a hand restore can bring the session
back, and the record moves to `deleted` with reason `purged`.

Pending harvest-queue files survive an archive purge only until they're published; afterward the
durable knowledge lives in the repository docs and the handled queue items are deleted.

## Restoring an archived session by hand

`rl session unarchive <id>` is the supported path. If you need to do it manually — say the
registry record is gone — here is the full procedure. `<slug>` is the encoded-cwd directory name
(e.g. `-Users-you-wrksp-oss-Foo`), identical on both sides.

1. **Move both** `archive/<slug>/<id>.jsonl` **and** its sidecar directory
   `archive/<slug>/<id>/` back into `~/.claude/projects/<slug>/`.

   Leaving the sidecar behind still resumes, but drops the session's `subagents/` and
   `tool-results/` data. Check the live side doesn't already hold either name first —
   `rl session archive` aborts on that collision, and the reverse move deserves the same care.

2. **Delete the session's entry from `archived` in `registry.json`. Not optional.**

   A scan skips any session that still carries an `archived` record — that is what stops an
   agent from undoing an archive by recreating its transcript. Leave the record in place and
   `rl session refresh` never mentions the restored session at all: absent from `pending`,
   uncounted in `total`, never re-registered. `rl prune` will then list the file you just moved
   back as a resurrected stub, and `rl prune --delete` will unlink it — and since restoring
   *moved* the transcript out of `archive/`, that is the only copy.

3. **Re-scan** — `rl overview`, or any command run without `--no-scan`.

4. Optionally delete the leftover `archive/<slug>/<id>.md` harvest summary and the emptied
   archive directory.

Then `cd <project> && claude --resume`.

### The registry record does not come back

Archiving pops the thread record, so a restored session returns as a **brand-new unclassified,
unnamed thread**. Its name, `whatItIs`, decision, tags, `harvestPath` and reviewed/classified
markers are gone. Re-run the `triage` or `cleanup` skill to rebuild them.

Both `archive/` and `registry.json` live under `~/.local/share/ringleader/`, outside the repo,
so none of this touches tracked files.

## Pruning live sessions

Separately from archives, `rl prune` removes sessions that are both **tiny** and **stale**:

```sh
rl prune                                    # preview only — this is the default
rl prune --delete                           # actually delete
rl prune --older-than 14 --max-turns 5
rl prune --project ringleader
```

Defaults are 7 days and 12 messages; a session must exceed the age **and** fall under the turn
ceiling.

> **`--delete` is not a preview flag.** Don't add it to see what a command would do.
>
> **`rl roundup` deletes** — it runs prune with deletion enabled.

Deleting leaves a tombstone in `deleted` with the reason and any `harvestPath`, so a pruned
session's knowledge outlives its transcript and a later scan won't re-register it as new.
