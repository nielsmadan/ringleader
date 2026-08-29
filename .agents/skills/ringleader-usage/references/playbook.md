# ringleader PLAYBOOK

Shared, agent-neutral conventions for the `ringleader` skills (`overview`, `triage`,
`cleanup`, `init`, `usage`). Every skill's per-agent wrapper points back here for the WHAT; the
wrapper itself only supplies the HOW for the two enhancement points below. The separate
`ringleader-publish` collection-root workflow owns its own protocol reference.

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

- `rl overview [selector] [--project X] [-s] [--snoozed] [--kept] [--archived] [--checkouts] [--recent N] [--source X] [--no-scan] [--json]`
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
  `rl session enrich` and `rl session harvest`. Stores no secrets.
- `rl harvests` — list the pending tool-native harvest queue. `snapshot`, `begin`,
  `prepare`, `consume`, `abandon`, and `recover` are publisher plumbing, not triage commands.

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
- `rl session keep ID [--yes]` — keep a session long-term: it stays live and resumable
  but leaves the triage inbox for good, even if it sees new activity.
- `rl session unkeep ID` — undo a keep; the session returns to the inbox.
- `rl session unarchive ID` — put an archived transcript back where it was so the
  agent can resume it again.
- `rl session relocate ID PATH` — re-point a session at a directory it moved to on disk.
- `rl session archive ID [--summary-file PATH]` — move a finished session's transcript out
  of the live registry; optionally attach a markdown summary.
- `rl session delete ID` — permanently delete a session's transcript.
- `rl session harvest ID [--dry-run]` — extract a finished session's durable knowledge into
  ringleader's harvest store via the configured model; a no-op that says so if none is set.
- `rl session complete ID` — harvest (as above) and then archive, in one step.
- `rl session mark reviewed ID` — record that an open session has been looked at this run.
- `rl session mark harvested ID [--paths PATHS]` — record that a session's durable
  knowledge has been integrated, and where.
- `rl session mark cleaned SELECTOR [--at ISO]` — record that a project was just triaged.

Memory subcommands (`rl mem <sub>`) — used by the separate memory track, not by these
five session-track skills, but part of the same CLI surface: `rl mem refresh`, `rl mem prune
[--delete]`, `rl mem triage`, `rl mem overview`, `rl mem mark reviewed|cleaned`.

Data subcommands (`rl data <sub>`): `rl data export ID [--out PATH]`, `rl data tokens
[--days N] [--archived] [--json]`, `rl data migrate [old_dir]`.

Usage subcommands (`rl usage`) — read-only token accounting across every harness, used by
the `usage` skill:

- `rl usage [--source X] [--days N] [--since D] [--until D] [--all] [--archived] [--by day|project|session|model|source] [--breakdown] [--limit N] [--merge DIR] [--json]`
  — token totals, ranked by the chosen grouping. `--archived` also counts sessions
  `rl session archive` moved out of the live transcript directories.
- `rl usage session ID [--json]` — one session's turns, token split, cache hit rate, top
  turns, and context growth. An ambiguous id prefix is refused, not merged.
- `rl usage windows [--json]` — Codex's 5h/weekly subscription consumption. Only current
  snapshots are reported; an expired one is named as having none, never shown as live.
  Claude windows are reported once captured via `rl install statusline`; a stale or
  expired capture reports "no current snapshot", never a stale percentage.
- `rl usage export [--out PATH]` — write this machine's records for `--merge` on another.
- `rl data tokens` is the Claude-only alias of `rl usage`.

Cost is never invented. JSON `total_cost` is `null` when nothing in scope is priced, and
`cost_partial: true` marks a scope that mixes priced records (Claude, Pi, OpenCode) with
unpriced ones (Codex); a partial figure is a floor, not the total.

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

## Opening, baseline, closeout

Bracket every run that changes state, so the user knows what is about to happen, and
afterwards can tell whether anything moved.

**Open by saying what this run will do.** Before the first command, give a short plan in
the user's terms: the steps, what will actually be changed on disk or in the registry,
what needs their decisions, and roughly how big the job is. Two or three lines — the
point is that nobody is surprised by a delete, and nobody has to guess how long this is.

**Then snapshot the baseline, before touching anything.** One scan, recorded for later:

```
rl overview --json      # live_count, unclassified, spread_projects, blocked, stale, kept
rl triage --json        # length = the inbox you are about to work
```

**At the end, report the delta, not the end state.** A short before → after table over
those same numbers, plus what the run actually did and what it cost:

```
                  before   after
live threads         261     249
unclassified         223     211
triage inbox         116      92
kept                   0       3
```

Then, in this order:
1. **What changed** — deleted/pruned (with disk reclaimed), classified, harvested,
   kept, snoozed.
2. **What needs the user** — sessions that came back `blocked`, and anything a decision
   is waiting on. Name them; this is the part worth their attention.
3. **What is left** — how much of the backlog is still untouched, so the number is not a
   surprise later.

**Always end with a concrete next step.** Never stop on a bare summary. `[SELECT]` a
short list of what to do now, with a recommendation first — e.g. keep going on the next
project batch, run `cleanup` for the full backlog, resume a specific blocked session, or
stop here. If the user picks an action, do it; the run is not over until they have chosen.

## Leaving the triage inbox

A session leaves the inbox in one of three ways, and they differ **only** in what brings
it back. Pick by asking the user what should happen next, not by how finished it looks:

| outcome | command | what happens to the transcript | comes back when |
|---|---|---|---|
| done | `rl session complete ID` | leaves a pending central harvest item, then moves the transcript out of the picker | never (it's archived; `rl session unarchive` undoes it) |
| still going | `rl session set …` + `rl session mark reviewed ID` | untouched, stays resumable | the session gets **new activity** |
| remind me | `rl session snooze ID --for N` | untouched, stays resumable | **N days** pass (it auto-wakes) |
| keep long-term | `rl session keep ID` | untouched, stays resumable | **never** — only `rl session unkeep`, or `rl overview --kept` to look |
| delete | `rl session delete ID` | **gone for good, nothing harvested** | never |

Two of these are one-way and worth stating out loud when you offer them: **delete**
destroys the transcript and harvests nothing first, and **done** takes the session out of
the user's resume picker. The other three leave it exactly where it was.

Use **open** for "still going, ping me when it moves", **snooze** for "not now, ask me
again on a date", and **keep** for "I want this thread around indefinitely; stop asking".
Kept sessions stay live and resumable and still show in `rl overview` — keep silences
triage only, and never auto-expires.

## Harvest sub-protocol

Used whenever `triage` or `cleanup` finds a session whose work is finished and contains
knowledge worth keeping (a decision, a gotcha, a fix) before the session is archived. There
are two ways to do it — choose per the user's setup.

### Preferred: tool-native (`rl session complete`)

When a model is configured (`rl config show`), run **`rl session complete <id>`** per
finished session. It extracts durable knowledge into the central pending queue and archives
the session in one step — no agent extraction, no checkout, no `[FAN-OUT]`. It both harvests
and archives, so **do not** also call `rl session mark harvested` / `rl session archive` for
that session. Browse the pending results with `rl harvests`; run `ringleader-publish` later
from a checkout collection to integrate and consume them. If no model is configured,
`complete` does nothing and says so — fall back to the agent-driven flow below.

Prefer this when the central queue is the desired staging point before later publication.

### Alternative: agent-driven, immediately into the repo's own docs

Use this when the user wants knowledge integrated into the target repo's **living docs**
(their `/doc` setup) rather than the harvest store, or when no model is configured.

1. **Integration target is prompted, not hardcoded.** Ask the user how they want durable
   knowledge integrated into the target repo. Default, and this user's standing answer: the
   `/doc` skill, keeping its existing layout. Whatever the user picks, treat it as the run's
   harvest method — do not hardcode a single tool as the only option, since other users of
   this playbook may answer differently.
2. **Where to write — clean checkout only.** Locate a checkout of the
   session's repo (the registry's recorded `cwd`/`gitRoot`, or `rl overview --checkouts`).
   - If a checkout exists and its working tree is **clean**, write the doc edits there.
   - If it is dirty, missing, or ambiguous, skip and report it. This triage flow never
     creates a worktree fallback.
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

`ringleader-publish` is not a fallback for this triage flow. It is the separate
collection-root workflow for central queue items: it skips dirty or ambiguous checkouts,
may offer one safe clone, commits verified documentation locally as `chore: update docs`,
and never pushes.

## Enhancement placeholders

Two points in the flow above need a facility beyond plain text — presenting choices and
running independent work concurrently. Each is named here as a placeholder; the per-agent
wrapper defines exactly how that agent fulfills it, and every wrapper must also work with
the fallback below if no better mechanism is available.

### `[SELECT]`

**What it does:** present a batch of items to the user and collect their choice for each
one individually (e.g. per session: done / still going / remind me / keep / delete —
see § Leaving the triage inbox for what each one does).

**Fallback:** print the batch as a numbered list with the available choices spelled out,
then ask the user to reply with the numbers or letters mapped to each choice (e.g.
`1d, 2f, 3o`).

**Phrase every option as what will happen, never as a command name.** The user has not
read this playbook and does not know ringleader's vocabulary — "enrich?", "prune?",
"harvest?" are jargon to them. Say what the option does, to how many sessions, and
whether it is reversible: *"Have an LLM read the 111 unclassified sessions and give each
a name and status?"* rather than *"Run enrich?"*. Two specific duties:

- **Say when something costs money or calls a model.** Anything routed through
  `rl session enrich` / `rl session harvest` / `rl session complete` spends an API call
  per session; the user should learn that before choosing, not from the bill.
- **Say when something deletes.** Name the count and whether it can be undone —
  `rl prune --delete` and `rl session delete` remove transcripts for good, while
  `rl session archive` only moves them and `rl session unarchive` puts them back.

### Presenting a batch

Before asking for decisions, list the sessions. **One session per entry, never several
per sentence** — running prose like *"two finished: `a` (1.9d — did X, Y and Z) and `b`
(1.7d — did P and Q). Plus four unfinished: …"* is unreadable at terminal width, because
the structure disappears the moment it wraps.

Use a stable shape, and keep entries short enough not to wrap:

```
notes/home — 6 awaiting a decision

  1. shopping-1 · 1.9d · finished
     Jina proxy flags verified, Reddit guidance updated

  2. add-antoine-dufour-records · 1.7d · finished
     five solo albums added with metadata and Qobuz links

  3. shopping-2 · 2.4d · unfinished
     desk speaker research; open question about the recording
```

The rules that matter:

- **A heading per project**, with how many are in this batch.
- **A numbered entry per session** — the number is what the user answers with if
  `[SELECT]` falls back to a numbered list.
- **First line: name · age · status.** Nothing else; it must not wrap.
- **Second line: one clause** on what the session was about, ~70 characters. If it needs
  a semicolon list of everything that happened, it is too long — say the part that
  informs the decision, not a summary of the work.
- **Blank line between entries.** The whitespace is what makes it scannable.
- **At most 8 entries per batch.** More than that is a wall regardless of formatting;
  split the project across batches and let the tally show the progress.

### `[FAN-OUT]`

**What it does:** run several independent, read-only extraction tasks concurrently — for
example, extracting durable knowledge from multiple finished sessions at once before the
single synthesis write.

**Fallback:** run the same extractions one at a time, in sequence.
