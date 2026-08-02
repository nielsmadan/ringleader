---
name: memory-triage
description: Guided cleanup of accumulated Claude memory (~/.claude/projects/*/memory/*.md) across all projects — auto-prune clear-obsolete entries, then classify survivors to delete, integrate into the repo's docs, flag as global, or keep. Reads the inbox from `rl mem triage`; accepts an optional repo/dir scope. Use to triage memory, across everything or for one repo. Triggers "clean up my memory", "triage memory", "triage memory for X".
---

# memory-triage

Walk accumulated memory entries and resolve each. By default this triages **across
every project**; the user may optionally name one repo/dir to scope to instead.
Deterministic actions are ringleader CLIs; integrating a fact into a repo's docs
is a judgment call you make and apply by hand.

Requires `rl` installed (`just install-local`) or, from a source checkout, run each
command as `uv run rl <sub>`. Ask the user if they want to scope to a single
repo/dir (a git-project label or path). If they name
one, call it `<X>` and pass `--project <X>` to every command below. If they don't, skip
`--project` everywhere and triage the full cross-project inbox.

## Steps

1. **Build the triage list.** Run `rl mem triage [--project <X>]` — it
   auto-reconciles and prints the inbox grouped by project, entry counts, and how many
   of those are obsolete-candidates. This is the inbox source for the whole skill.
   Work **most-cluttered project first**.

2. **Per project, auto-prune clear-obsolete.** This is the only auto-delete gate:

   ```bash
   rl mem prune --project <X>            # preview only — no deletion
   ```
   This lists the candidates (those with a deterministic `obsoleteSignals`:
   `repo-gone`, `fixed`, `status-snapshot`, `already-in-doc`). Show the preview to the
   user; if they approve, apply it — stdin is non-interactive here, so feed the
   confirmation:
   ```bash
   echo y | rl mem prune --project <X> --delete
   ```

3. **Classify the survivors.** Run `rl mem refresh --project <X>` — it emits one JSON
   memory-entry per line (`path`, `project`, `name`, `description`, `obsoleteSignals`,
   `gitProjectLabel`, etc.). For each entry decide one of:

   - **delete** — stale/no longer useful but didn't trip a deterministic signal. Apply
     these by hand the same way as integrate-repo's cleanup (step 4): `unlink` the `.md`
     and drop its `MEMORY.md` index line — confirm the batch with the user first.
   - **integrate-repo** — durable, project-specific knowledge that belongs in that
     repo's own docs.
   - **global?** — a cross-cutting behavioral rule/preference, not project-specific.
   - **keep** — still a useful standalone memory entry as-is.

   Dedup across checkouts of the same git-project: group entries by `gitProjectLabel`
   (not raw `project` path) so the same fact surfacing from two clones of one repo is
   judged once.

4. **Apply integrate-repo, with confirmation.** Collect the batch of integrate-repo
   decisions for a project and confirm with the user before writing. For each:
   - Resolve the target file in the repo: if its `CLAUDE.md` contains an `@AGENTS.md`
     import, write to `AGENTS.md`; else write to `CLAUDE.md`; else create a new
     `CLAUDE.md`.
   - Append the distilled fact under a heading that fits the file's existing style
     (match its heading conventions rather than inventing a new section per entry).
   - Delete the source `.md` file and remove its line from that project's
     `MEMORY.md` index (the memory module exposes `rewrite_index_without` for this;
     otherwise edit the index by hand).

5. **Flag global entries — never apply them yourself.** Items classified `global?`
   are cross-cutting rules that don't belong to one repo. List them in the run summary
   and **leave them in place** (do not delete, do not mark reviewed as a stand-in for
   integrating). Never touch `~/ac/global` or run `sync.py` — that promotion is a
   separate, human-driven step outside this skill.

6. **Finish each project.** Once its survivors are classified and integrations
   applied:
   - `rl mem mark reviewed <path>` for every entry you decided to **keep**
     (integrated and deleted entries no longer exist to mark; global-flagged entries
     are intentionally left unmarked so they keep surfacing until someone promotes
     them).
   - `rl mem mark cleaned <X>` to stamp the project as just-triaged, so `rl mem
     overview` stops flagging it until new memory lands.

7. **Report the tally.** For each project touched: integrated N / deleted N / kept N /
   flagged-global N. Remind the user that integration edits are left uncommitted in
   each target repo for review — this skill never commits on the user's behalf.

## Gates
- **Auto-delete**: only `rl mem prune --delete`'s clear-obsolete set
  (`obsoleteSignals` present). Everything else is a judgment call.
- **Confirm before applying**: batch of integrate-repo writes, and any judgment-call
  deletion (survivors without a deterministic obsolete signal).
- **Never commit.** Repo doc edits from integration stay uncommitted for the user to
  review.
- **Never touch `~/ac/global` or run `sync.py`.** Global-rule promotion is out of
  scope for this skill.

## Notes
- `rl mem triage` and `rl mem refresh` both auto-reconcile the registry against
  disk before reporting, so the inbox reflects the current state of every project's
  `memory/*.md` files.
- `rl mem overview [<selector>]` gives the same data as a rollup or per-repo
  deep-dive, useful for a quick before/after check without re-running the full triage.
- Never hand-edit `registry.json` or a project's `memory/*.md` files outside the flows
  above.
