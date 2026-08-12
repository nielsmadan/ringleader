# ringleader PLAYBOOK

Shared, agent-neutral conventions for the `ringleader` skills (`overview`, `triage`,
`cleanup`, `init`). Every skill's per-agent wrapper points back here for the WHAT; the
wrapper itself only supplies the HOW for the two enhancement points below.

This file must stay agent-neutral: no mechanism names, only the two placeholders
`[SELECT]` and `[FAN-OUT]`, defined below. A per-agent wrapper maps each placeholder to
that agent's own facility.

## Calling `rl`

- Prefer the installed console command, `rl`. From a source checkout that hasn't been
  installed, use `uv run rl` instead — every command below works the same either way.
- Read structured data from the `--json` forms (`rl overview --json`, `rl overview -s
  --json`, `rl overview --snoozed --json`, `rl triage --json`). JSON output includes each
  session's `session_id`; use that field to address a session in follow-up commands.
- Never hand-edit `registry.json` or anything else under ringleader's data directory.
  All registry state changes go through an `rl` subcommand.

### Command surface the skills use

Flat commands:

- `rl overview [selector] [--project X] [-s] [--snoozed] [--archived] [--checkouts] [--recent N] [--source X] [--no-scan] [--json]`
  — project rollup by default, `-s` for a flat per-session list, `--snoozed` for
  currently-snoozed sessions, a `selector` (git-project label or cwd path) for a
  single-repo deep-dive.
- `rl triage [--project X] [--recent N] [--unclassified] [--source X] [--no-scan] [--json]`
  — the inbox of sessions awaiting a decision.
- `rl prune [--project X] [--older-than N] [--max-turns N] [--delete]`
  — deletes tiny, stale sessions. Previews by default; only `--delete` actually deletes.
- `rl doctor [--settings PATH]` — checks each installed harness's session-retention
  setting so live sessions aren't garbage-collected before you get to them.
- `rl config show` / `rl config set …` — select the LLM backend used by
  `rl session enrich`. Stores no secrets.

Session subcommands (`rl session <sub>`):

- `rl session refresh [--project X] [--tail N]` — rescan disk and update the registry.
- `rl session set --session ID --status {active,unfinished,blocked,finished} --what WHAT --name NAME [--confidence N]`
  — record a classification for one session.
- `rl session enrich [--project X] [--limit N]` — classify pending sessions via an LLM.
- `rl session name [--project X] [--limit N] [--active-window N] [--dry-run]`
  — (re)name sessions so the resume picker is readable.
- `rl session snooze ID [--until ISO] [--for N]` — set a session aside; it drops out of
  the default overview until woken.
- `rl session wake ID` — un-snooze a session.
- `rl session relocate ID PATH` — re-point a session at a directory it moved to on disk.
- `rl session archive ID [--summary-file PATH]` — move a finished session's transcript out
  of the live registry; optionally attach a markdown summary.
- `rl session delete ID` — permanently delete a session's transcript.
- `rl session mark reviewed ID` — record that an open session has been looked at this run.
- `rl session mark harvested ID [--paths PATHS]` — record that a session's durable
  knowledge has been integrated, and where.
- `rl session mark cleaned SELECTOR [--at ISO]` — record that a project was just triaged.

Memory subcommands (`rl mem <sub>`) — used by the separate memory track, not by these
four skills, but part of the same CLI surface: `rl mem refresh`, `rl mem prune
[--delete]`, `rl mem triage`, `rl mem overview`, `rl mem mark reviewed|cleaned`.

Data subcommands (`rl data <sub>`): `rl data export ID [--out PATH]`, `rl data tokens
[--days N] [--json]`, `rl data migrate [old_dir]`.

## Strategic ordering

Work in this order, every time, regardless of which skill is running:

1. **Auto-prune first.** Run `rl prune` (preview), show the user what would go, then
   `rl prune --delete` once confirmed. This clears tiny/stale junk before anyone spends
   attention on it.
2. **Then project-by-project, in bounded chunks.** Never present the whole inbox as one
   flat wall of sessions. Group by project and work one project at a time, oldest activity
   first, so early progress is visible and a run can be paused and resumed cleanly.
3. **Within a project, oldest-first.** The longest-dormant sessions are the most likely to
   be truly finished or truly abandoned — resolve those before newer, still-active threads.

## Progress tally

Print a running tally at every step so the scale of the job — and how far through it the
user is — stays visible. Format:

```
Project <i>/<N> · <done>/<total> triaged · <H> harvested
```

Example: `Project 3/12 · 47/210 triaged · 12 harvested`. Update it after every project,
not just at the end of the run.

## Harvest sub-protocol

Used whenever `triage` or `cleanup` finds a session whose work is finished and contains
knowledge worth keeping (a decision, a gotcha, a fix) before the session is archived.

1. **Integration target is prompted, not hardcoded.** Ask the user how they want durable
   knowledge integrated into the target repo. Default, and this user's standing answer: the
   `/doc` skill, keeping its existing layout. Whatever the user picks, treat it as the run's
   harvest method — do not hardcode a single tool as the only option, since other users of
   this playbook may answer differently.
2. **Where to write — reuse-clean, else fresh worktree.** Locate a checkout of the
   session's repo (the registry's recorded `cwd`/`gitRoot`, or `rl overview --checkouts`).
   - If a checkout exists and its working tree is **clean**, write the doc edits there.
   - If it's **dirty, or no checkout exists**, create an isolated `git worktree` in a
     temporary directory, write the edits there, and report the path to the user.
   - Either way, **confirm with the user before writing**, and leave the edits
     **uncommitted** for their review — this playbook never commits on the user's behalf.
3. **Two phases, in order:**
   - **`[FAN-OUT]` extract.** For each finished session in the batch, independently pull
     out durable knowledge (decisions, gotchas, what-changed) into a temporary per-session
     notes file. This phase is read-only against the target repo — nothing is written to
     real docs yet.
   - **Synthesize once per repo.** With all of a repo's extracted notes in hand, write them
     into the target repo a single time, through the chosen integration target, avoiding
     duplicate or conflicting edits from multiple sessions touching the same doc.
   - **Record and close out.** For each harvested session: `rl session mark harvested <id>
     --paths <comma-separated doc paths>`, then `rl session archive <id> --summary-file
     <path to its extracted notes>`.

## Enhancement placeholders

Two points in the flow above need a facility beyond plain text — presenting choices and
running independent work concurrently. Each is named here as a placeholder; the per-agent
wrapper defines exactly how that agent fulfills it, and every wrapper must also work with
the fallback below if no better mechanism is available.

### `[SELECT]`

**What it does:** present a batch of items to the user and collect their choice for each
one individually (e.g. per session: delete / finished / open).

**Fallback:** print the batch as a numbered list with the available choices spelled out,
then ask the user to reply with the numbers or letters mapped to each choice (e.g.
`1d, 2f, 3o`).

### `[FAN-OUT]`

**What it does:** run several independent, read-only extraction tasks concurrently — for
example, extracting durable knowledge from multiple finished sessions at once before the
single synthesis write.

**Fallback:** run the same extractions one at a time, in sequence.
