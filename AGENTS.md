# AGENTS.md

Python CLI that curates dormant Claude Code sessions on disk — naming, classifying, and
pruning them before the GC clock deletes them.

## Gotchas

- **It operates on live session data.** The tool reads and deletes under
  `~/.claude/projects/*.jsonl` — the real transcripts for every project on this machine.
  Destructive subcommands act on actual work, not a sandbox or a copy.
- **`rl prune` previews by default.** It only deletes with `--delete`. Do not add that flag
  to a command you are running to "see what it would do."
- **`rl session archive` takes the session out of the resume picker.** The transcript moves out of
  `~/.claude/projects/` into `archive/`, so `claude --resume` genuinely cannot see it —
  treat archiving as ending the thread. There is no `rl unarchive`, but nothing is deleted:
  a human can move the files back by hand (README → "Restoring an archived session"), at
  the cost of the thread's registry record.
- **`rl` is an installed console command, not a repo-local script.** `just install-local`
  (or `uv tool install .`) puts it on `PATH`; from a source checkout without installing,
  use `uv run rl <sub>` instead. Its state — `registry.json` and `archive/` — lives under
  `~/.local/share/ringleader/` (`$XDG_DATA_HOME/ringleader` if set), not in this repo.
  `rl data migrate` (run automatically by `just install-local`) moves any legacy repo-root
  state there once.
- **`overview` and `triage` hit the disk before rendering.** Both auto-scan unless you pass
  `--no-scan`. On a large `~/.claude/projects` that scan dominates the runtime.
- **Most subcommands are plumbing.** Everything outside `overview` / `triage` / `prune` is
  grouped under `rl session`, `rl mem`, `rl data`, and `rl config`, and is
  meant to be called by the skills in `plugin/skills/` (session track) or
  `.claude/skills/` (`memory-triage`), not by hand.
- **`plugin/` is a Claude Code plugin, not a skills directory of its own.** Four skills —
  `overview`, `init`, `triage`, `cleanup` — live under `plugin/skills/<name>/SKILL.md` as
  thin, per-agent wrappers over the shared, agent-neutral flow in `plugin/PLAYBOOK.md`.
  They replace the old, single-purpose session skills (sync, overview, name, harvest,
  triage as separate skills) — those are gone, folded into these four. `memory-triage` is
  untouched and still lives in `.claude/skills/`. Install the plugin with
  `just install-plugin`; see `plugin/SMOKE.md` for the manual smoke checklist.
- **Scanning goes through a `Source` seam.** `ringleader/sources/` defines the
  `discover()`/`parse()` protocol; `sources/claude_code.py` (reading
  `~/.claude/projects/*.jsonl`) is the only implementation today. Registry/overview/triage
  code talks to `Source`, not to Claude Code transcripts directly.

## Commands

```sh
rl overview [<selector>] [--json]   # the view; project rollup by default
rl triage [--json]                  # sessions awaiting a decision
rl prune [--delete]                 # delete tiny + stale sessions
rl harvests [--json]                # inbox of durable knowledge harvested out of sessions
rl session <sub>                    # session plumbing: refresh, set, archive, delete, ...
rl session enrich                   # classify pending sessions via an LLM (vs. by hand in the triage/cleanup skill)
rl session harvest <id> [--dry-run] # distill a finished session into a harvest via an LLM
rl session complete <id>            # harvest, then archive (no-op archive if harvest didn't run)
rl mem <sub>                        # memory plumbing: refresh, prune, triage, overview, ...
rl data <sub>                       # export, tokens, migrate
rl config                           # select the LLM backend for enrich (stores no secrets)
just test                           # tests; pytest config lives in pyproject.toml now
```

From a source checkout without installing, prefix each `rl` call with `uv run` (`uv run rl overview`).

A `<selector>` is a git-project label (`juggler`) or a cwd path — not a session id.

## Reference

`README.md` documents the full command surface including flags. `SPEC.md` holds the data
model. Design docs for individual features are in `docs/specs/`.
