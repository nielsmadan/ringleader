---
name: session-triage
description: Guided cleanup of dormant Claude Code sessions across all projects — first prune the tiny+stale junk, then go session-by-session to delete, finish (harvest knowledge into the docs + archive), or leave open. Reads the inbox from `rl triage`; accepts an optional repo/dir scope. Use to triage your sessions, across everything or for one repo. Triggers "clean up my sessions", "triage my sessions", "clean up sessions for X", "triage project sessions".
---

# session-triage

Walk dormant sessions and resolve each. By default this triages **across every
project**; the user may optionally name one repo/dir to scope to instead. Deterministic
actions are ringleader CLIs; harvesting into docs is the `/doc` skill.

Requires `rl` installed (`just install-local`) or, from a source checkout, run each
command as `uv run rl <sub>`. Ask the user if they want to scope to a single
repo/dir (a git-project label or path). If they name
one, call it `<X>` and pass `--project <X>` to every command below. If they don't, skip
`--project` everywhere and triage the full cross-project inbox.

## Steps

1. **Prune the junk first.** Preview, then (with the user's OK) delete:

   ```bash
   rl prune [--project <X>]
   ```
   Show the preview to the user; if the user approves in the conversation, feed the
   confirmation automatically (stdin is non-interactive here):
   ```bash
   echo y | rl prune [--project <X>] --delete
   ```

2. **Build the triage list.** Run `rl triage [--project <X>]` — it auto-refreshes and
   prints the inbox grouped by project: every session awaiting a decision, i.e. still
   open and excluding parked, `decision == "harvested"`, and already-reviewed-since-
   last-change. This is the inbox source for the whole skill. It's a human-readable view
   and does **not** print full ids, so cross-reference its entries against
   `registry.json`'s `.threads[]` (matching on `whatItIs`/title and project/`gitProjectLabel`)
   to get each session's full `sessionId` for the CLI calls below. Order oldest-first.

3. **Triage each session (collect decisions).** Oldest-first, present each session in
   2-3 lines — what it was about, status, age, size — from the registry's
   `whatItIs`/`lastPrompt`, or `rl export <id> --out /tmp/triage-<id>.md`
   for a deeper look. Ask: **delete / finished / open?** Apply the cheap ones immediately,
   and **collect the `finished` ids into a list** for batch harvesting in step 4:

   - **delete** → `rl delete <id>`
   - **finished** → add `<id>` to the harvest list (step 4 — do NOT harvest inline).
   - **open** → classify it now so it can be named (you just read it): pick its status and
     a good kebab name, then
     `rl sync apply --session <id> --status <unfinished|blocked|active|finished> --what "<one-line>" --name "<kebab-name>"`,
     and add `<id>` to the open list (named + reviewed in step 5).

4. **Batch-harvest the finished sessions — two phases.** Harvesting writes to each
   session's own repo docs, so harvest agents must NOT run concurrently against the
   *same* repo (running concurrently across different repos is fine). Split extract
   (parallelizable) from integrate (single writer per repo):

   **Phase A — extract in parallel (read-only).** For each finished `<id>`, dispatch a
   subagent — all at once, since they only read transcripts and write their own temp file:
   - `rl export <id> --out /tmp/triage-<id>.md` (if the export exceeds
     ~300 KB, sample it: first ~140 KB + `…[elided]…` + last ~140 KB).
   - extract ONLY durable, project-specific knowledge into `/tmp/nuggets-<id>.md` under
     `## Gotchas` / `## Decisions` / `## Changes` (empty section → `(none)`), with enough
     context that a synthesizer who didn't see the session can place each item. No doc
     writes, no archive, no marking in this phase.

   **Phase B — synthesize once per repo (single writer).** Group the finished sessions by
   their target repo (`gitProjectLabel`/cwd). For each repo, once every extraction in that
   group is done:
   - concatenate that repo's `/tmp/nuggets-<id>.md` files (keep their per-source topic
     headers) into `/tmp/nuggets-<repo>-combined.md`.
   - from the **target repo's cwd**, integrate in one pass:
     `/doc --session --md /tmp/nuggets-<repo>-combined.md --report /tmp/nuggets-<repo>.report`
     — `/doc` dedups across the sessions, places gotchas/`## Why`/current-state, never
     touches `docs/product/`, and writes the touched paths to the report. Edits stay
     uncommitted for the user to review.
   - then for EACH finished `<id>` in that repo:
     `rl mark-harvested <id> --paths "<paths from the report>"` and
     `rl archive <id> --summary-file /tmp/nuggets-<id>.md`
     (the nuggets become the archived session's searchable summary sidecar).

   (Only one finished session total? Skip the fan-out — extract and synthesize it directly.)

5. **Name + review the open sessions.** Open ones stay in the resume picker, so name them,
   then mark reviewed:
   - `rl name [--project <X>]`
     — applies the names you set in step 3 (writes a `custom-title`, which bumps each
     file's mtime).
   - then for EACH open `<id>`: `rl mark-reviewed <id>` — it records
     the *live* (post-name) mtime, so a just-named open session isn't re-surfaced next run.

6. **Record the run + tally.** Stamp every repo touched this run as just-triaged, so
   `rl overview` shows it as done (and stops flagging it ⚠ until new activity lands): for
   each distinct project seen in step 2's inbox (just `<X>` if scoped), run
   `rl cleaned <project>`. Then report the tally — pruned N, deleted N,
   finished/harvested N, named+open N — and remind the user the harvested doc edits are
   uncommitted in each target repo for review. To find something later inside an archived
   session, use `rl overview --archived [--project <X>]`.

## Notes
- **Why two phases:** extraction is read-only and parallelizes (big sessions run at once);
  integration must be a single writer per repo so concurrent harvests don't clobber the
  same docs, and one pass can dedup knowledge across sessions instead of N incremental
  appends.
- "open" is not permanent — the session stays resumable and reappears next run only if it
  gets new activity. It can be finished or deleted in a later run.
- "finished" both harvests (knowledge → docs) and archives (raw transcript → `archive/`,
  out of `~/.claude/projects`). If `/doc` finds nothing worth keeping, still archive — the
  summary sidecar is then the only index to that content.
- "open" sessions are **named** (so `claude --resume <name>` works) and reviewed. Run
  `rl name` before `rl mark-reviewed` so the marker captures the post-rename mtime.
- Archived sessions keep a **summary sidecar** (`archive/<encoded>/<uuid>.md`) plus a
  one-line tombstone summary; browse/search them with
  `rl overview --archived [--project <X>]`.
- Never hand-edit `registry.json` or transcripts.
