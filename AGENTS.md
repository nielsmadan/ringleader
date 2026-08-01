# AGENTS.md

Python CLI that curates dormant Claude Code sessions on disk — naming, classifying, and
pruning them before the GC clock deletes them.

## Gotchas

- **It operates on live session data.** The tool reads and deletes under
  `~/.claude/projects/*.jsonl` — the real transcripts for every project on this machine.
  Destructive subcommands act on actual work, not a sandbox or a copy.
- **`rl prune` previews by default.** It only deletes with `--delete`. Do not add that flag
  to a command you are running to "see what it would do."
- **`rl archive` takes the session out of the resume picker.** The transcript moves out of
  `~/.claude/projects/` into `archive/`, so `claude --resume` genuinely cannot see it —
  treat archiving as ending the thread. There is no `rl unarchive`, but nothing is deleted:
  a human can move the files back by hand (README → "Restoring an archived session"), at
  the cost of the thread's registry record.
- **`./rl` is repo-local and not on `PATH`.** Run it from the repo root or by absolute path.
  It dispatches through `.venv/bin/python`, so the venv must exist — a missing `.venv` fails
  with a bare "no such file" rather than anything useful.
- **`overview` and `triage` hit the disk before rendering.** Both auto-scan unless you pass
  `--no-scan`. On a large `~/.claude/projects` that scan dominates the runtime.
- **Most subcommands are plumbing.** Everything outside `overview` / `triage` / `prune` is
  meant to be called by the skills in `.claude/skills/`, not by hand.

## Commands

```sh
./rl overview [<selector>]   # the view; project rollup by default
./rl triage                  # sessions awaiting a decision
./rl prune [--delete]        # delete tiny + stale sessions
pytest                       # tests; config in pytest.ini (testpaths=tests, pythonpath=.)
```

A `<selector>` is a git-project label (`juggler`) or a cwd path — not a session id.

## Reference

`README.md` documents the full command surface including flags. `SPEC.md` holds the data
model. Design docs for individual features are in `docs/specs/`.
