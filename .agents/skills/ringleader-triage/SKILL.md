---
name: ringleader-triage
description: Recurring cleanup of dormant agent sessions from the last few days — decide delete/finished/open/snooze/keep per session, harvest what's finished, name and review what's open. Use when the user says "triage sessions", "triage my sessions", "clean up my sessions", or invokes triage.
---

# Triage

Scope: **recent** — the inbox is bounded to the last few days of activity
(`--recent`), so a run stays small and finishable. For the full, unbounded backlog use
the `cleanup` skill instead; it runs this same flow without the bound. `triage` and
`cleanup` differ only in scope (recent window vs. whole backlog).

This skill uses two mechanisms your agent provides. `[SELECT]`: use your native
structured-choice tool (Claude Code: `AskUserQuestion`) to get a per-item decision (one
question per session, offering delete/finished/open); if you have none, present a
**numbered list** and read back the choices. `[FAN-OUT]`: run the harvest sub-protocol's
extract phase for the finished sessions **in parallel** if your agent supports subagents,
otherwise **sequentially, one at a time**. Everything else — calling `rl`, strategic
ordering, the progress tally, the harvest sub-protocol — is in `references/playbook.md`;
this file only adds the flow.

## Flow

1. **Say what this run will do, then snapshot the baseline.** Per
   `references/playbook.md` § Opening, baseline, closeout — name the scope (how many
   sessions in the recent window, across how many projects), say that the run starts by
   deleting tiny/stale junk and then asks for a decision per session, and record the
   baseline numbers before anything changes.

2. **Auto-prune junk.** Run `rl prune` (preview). Show the user what would go, and on
   their OK run `rl prune --delete` — feed the confirmation non-interactively per
   `references/playbook.md`.

3. **Build the inbox.** Run `rl triage --recent <N> --json` (default `N=7` — the last
   week — unless the user asks for a different window) and group the results by their
   `project` field.

4. **Per project, with the progress tally.** Working project by project, oldest activity
   first (see `references/playbook.md` § Strategic ordering), `[SELECT]` **delete /
   finished / open / snooze / keep** for each session in the project.

5. **Apply the choices.**
   - *delete* → `rl session delete <id>` now.
   - *finished* → add to this run's harvest queue (don't archive yet).
   - *open* → classify it: `rl session set --session <id> --status <status> --what
     "<what>" --name "<name>"`.
   - *snooze* → not now, ask again on a date: `rl session snooze <id> --for <days>`
     (ask how many days).
   - *keep* → wanted long-term, stop asking: `rl session keep <id>`. It stays live and
     resumable but never returns to the inbox — see `references/playbook.md`
     § Leaving the triage inbox for how the three differ.

6. **Harvest the finished set.** Follow the harvest sub-protocol in `references/playbook.md`.
   When a model is configured, that means `rl session complete <id>` per finished session
   (harvest + archive in one step); otherwise `[FAN-OUT]`-extract each queued session and do
   the single synthesis write per repo.

7. **Name + review the open set.** Run `rl session name` to (re)name the sessions just
   classified, then `rl session mark reviewed <id>` for each.

8. **Record and close out.** Run `rl session mark cleaned <project>` for each project
   completed this run, then run the closeout from `references/playbook.md` § Opening,
   baseline, closeout: the before → after table against step 1, what changed, what came
   back **blocked on the user**, and what's left. End by `[SELECT]`-ing the next step —
   next project batch, `cleanup` for the whole backlog, resume a named blocked session, or
   stop — and act on their choice.
