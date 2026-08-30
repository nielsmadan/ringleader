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
  treat archiving as ending the thread. `rl session unarchive <id>` puts it back, and nothing is deleted
  (until `archive.retention_days` elapses — default 0/off): a human can move the files back
  by hand (README → "Restoring an archived session"), at the cost of the thread's registry
  record. Once the retention cutoff passes, `rl prune`/`rl roundup` purges the archived files
  for good.
- **`rl session finish` does not archive anything on its own.** It marks the session;
  `rl session sweep` archives it once the transcript is quiet, because moving a live
  transcript is undone by the agent's next write.
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
  meant to be called by the skills in `.agents/skills/` (session track) or
  `.claude/skills/` (`memory-triage`), not by hand.
- **Skills live at `.agents/skills/ringleader-*`, agent-neutral.** Seven skills —
  `ringleader-overview`, `ringleader-init`, `ringleader-triage`, `ringleader-cleanup`,
  `ringleader-usage`, `ringleader-finish`, `ringleader-publish` — each ship an
  agent-neutral `SKILL.md` plus a bundled `references/playbook.md`, synced from
  the canonical `.agents/skills/PLAYBOOK.md`
  (`just sync-playbook`). The first four replace the old, single-purpose session skills
  (sync, overview, name, harvest, triage as separate skills) — those are gone, folded into
  these four; `ringleader-usage` explains where tokens went; `ringleader-finish` finishes
  the session you're currently in, harvesting it from live context instead of waiting for
  triage to read the transcript later. `memory-triage` is untouched and still lives in
  `.claude/skills/`. Wire them into an agent with `rl install
  skills`, which symlinks the bundled skills into `~/.agents/skills` (Codex/OpenCode/Pi)
  and `~/.claude/skills` (Claude Code); `rl update`/`rl uninstall` round out the surface,
  and `rl doctor` reports what's wired. The old Claude-only plugin manifest and installer
  are gone. See `.agents/skills/SMOKE.md` for the manual smoke checklist.
- **`rl workspace move` changes three things at once.** The directory on disk, the
  agent's session storage (Claude Code keys it by the cwd a session was *launched*
  in, so the slug dir is renamed), and the registry — which is then pinned, or the
  next scan reverts it from the transcript's stale baked-in cwd. It previews and
  confirms, and **refuses rather than merges** when the destination already holds
  session storage, because merging would rewrite a sessionId — the registry's key.
  `rl workspace retarget` does the last two halves, for a directory you moved by
  hand, and is what makes `move` re-runnable after a partial failure.
- **Scanning goes through a `Source` seam.** `ringleader/sources/` defines the
  `discover()`/`parse()` protocol plus opt-in capability protocols (native rename, picker
  hiding, workspace relocation); `claude_code.py` and `codex.py` implement it.
  Registry/overview/triage code talks to `Source`, never to a transcript format.
  See `docs/tech/sources.md`.

## Commands

```sh
rl inbox                            # TUI (Textual, shipped as a dep) to blitz dormant sessions inbox-zero style
rl roundup [--yes]                  # refresh + prune (deletes) + enrich in one shot; rl inbox runs it when stale
rl overview [<selector>] [--json]   # the view; project rollup by default
rl triage [--json]                  # sessions awaiting a decision
rl prune [--delete]                 # delete tiny + stale sessions
rl usage [--breakdown] [--by X]     # token usage across claude/codex/opencode/pi
rl usage session <id>               # where one session's tokens went
rl usage windows                    # 5h/weekly limits: codex from disk, claude if captured
rl install statusline               # capture claude's 5h/weekly limits via the statusLine hook
rl harvests [--json]                # inbox of durable knowledge harvested out of sessions
rl session <sub>                    # session plumbing: refresh, set, archive, delete, ...
rl session keep <id>                # keep long-term: out of triage for good (unkeep to undo)
rl session enrich                   # classify pending sessions via an LLM (vs. by hand in the triage/cleanup skill)
rl session harvest <id> [--dry-run] # distill a finished session into a harvest via an LLM
rl session complete <id>            # harvest, then archive (no-op archive if harvest didn't run)
rl mem <sub>                        # memory plumbing: refresh, prune, triage, overview, ...
rl data <sub>                       # export, tokens, migrate
rl config                           # select the AI engine for enrich (ai=claude/codex/llm; stores no secrets)
just test                           # tests; pytest config lives in pyproject.toml now
```

From a source checkout without installing, prefix each `rl` call with `uv run` (`uv run rl overview`).

A `<selector>` is a git-project label (`juggler`) or a cwd path — not a session id.

## Reference

`docs/overview.md` maps the docs tree. `docs/features/` is what each feature does,
`docs/tech/` is how it's built (start at their `overview.md`), `docs/decisions/` holds the
ADRs, and `docs/reference/` records how the **external** agent CLIs behave. `README.md` and
`docs/user/cli.md` document the command surface including flags.
