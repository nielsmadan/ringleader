---
name: ringleader-cleanup
description: Work through the full backlog of dormant agent sessions, not just recent ones — bounded chunks, resumable across runs. Use when the user says "clean up session history", "the big backlog", "clean up old sessions", or invokes cleanup.
---

# Cleanup

Scope: **all** — the whole backlog, not just recent activity. `cleanup` is the exact same
flow as the `triage` skill, differing only in scope: its inbox omits `--recent`, so
`rl triage --json` (no recency bound) covers every session still awaiting a decision,
not just the last few days. For everything else — the auto-prune step, per-project
selection, applying choices, harvesting, naming/review, and recording — follow the
`triage` skill's flow via `references/playbook.md` unchanged; it is not repeated here.

This skill uses the same two mechanisms as `triage`, your agent provides them. `[SELECT]`:
use your native structured-choice tool (Claude Code: `AskUserQuestion`) to get a per-item
decision; if you have none, present a **numbered list** and read back the choices.
`[FAN-OUT]`: run each finished session's extraction **in parallel** if your agent supports
subagents, otherwise **sequentially, one at a time**. See `references/playbook.md` for the
mechanics and fallbacks.

## Flow

Run the `triage` skill's flow (`../ringleader-triage/SKILL.md`) end to end, with its Step
3 changed to build the inbox from `rl triage --json` — no `--recent` bound. Its opening
plan (Step 1) matters more here than anywhere: say up front how big the backlog is, that
the run is chunked by project, and that it can be stopped and resumed at any point.

Because the backlog can be large, lean hard on `references/playbook.md`'s strategic
ordering: work **project by project, in bounded chunks**, oldest activity first, and
report the progress tally after every project rather than trying to clear the whole
backlog in one sitting. Run the closeout (`references/playbook.md` § Opening, baseline,
closeout) when the user stops, not only when the backlog is empty — a partial run still
owes them a delta and a next step.

**Resumable by design.** It's fine to stop partway through and pick this back up later —
`rl session mark cleaned <project>` and the per-session `reviewed`/`harvested` marks are
recorded in the registry as you go, so re-running `cleanup` continues from wherever the
last run left off instead of re-presenting sessions already triaged.
