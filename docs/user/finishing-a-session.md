# Finishing a session

Two ways to end a thread: from *inside* it while you're still there, or later, during triage.
The first one is better, and the difference is not obvious.

## From inside the session

Invoke the `ringleader-finish` skill in the session you want to end. It runs:

```sh
rl session harvest --current --from-file -    # harvest from live context
rl session finish --current                   # leaves triage immediately
```

The point is **what gets harvested**. A model reading the transcript afterward sees what was
written; the agent that was in the room saw what you actually decided, what you tried that
failed, and what turned out not to matter. Those are the durable parts, and they are largely
absent from the transcript.

## From triage, later

```sh
rl session harvest <id>      # a model reads the transcript
rl session finish <id>
```

or both at once:

```sh
rl session complete <id>     # harvest, then archive
```

`complete` does **not** archive if the harvest was a no-op (no model configured) — it won't
archive something it failed to read.

## Finishing refuses without a harvest

```
$ rl session finish abc123
refusing: session has no harvest (use --force to finish anyway)
```

This is deliberate. Archiving is cheap and harvesting costs a model call, so the natural failure
mode is a tidy archive full of threads whose lessons were thrown away — and it's silent, because
an archived session looks the same either way. `--force` is the explicit "there was genuinely
nothing to learn here".

## Finishing doesn't archive anything

`rl session finish` marks the thread and moves no files. The session leaves the triage inbox
immediately, but the transcript stays where it is.

It has to work this way: you finish a session from inside that session, and the agent writes to
the transcript again on the way out. Moving a live transcript just makes the agent recreate it
at the old path, silently undoing the archive.

`rl session sweep` does the actual move, once the transcript has stopped changing:

- **Within seconds of quitting** if you've run `rl install hooks` — a session-end hook fires
  `rl session sweep --from-hook`. Wired for Claude Code, Codex and Pi.
- **At the next `rl roundup`** otherwise, or on OpenCode, which has no session-end event to hook.

`rl doctor` reports per agent whether the hook is installed.

## Where the harvest goes

Harvests land in a machine-local queue under `~/.local/share/ringleader/harvests/`:

```sh
rl harvests           # what's pending
```

They're durable only until they're integrated into real documentation. The `ringleader-publish`
skill drains the queue into the living docs of matching repositories — it skips dirty or
ambiguous checkouts, commits locally only after integration verifies, and never pushes.

```text
cd ~/wrksp/all
/ringleader-publish using the /doc skill
```

## Next

- [Archives and retention](archives-and-retention.md) — what happens to the transcript after.
