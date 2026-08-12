---
name: cleanup
description: Work through the full backlog of dormant Claude Code sessions, not just recent ones — bounded chunks, resumable across runs. Use when the user says "clean up session history", "the big backlog", "clean up old sessions", or invokes cleanup.
---

# Cleanup

Scope: **all** — the whole backlog, not just recent activity. `cleanup` is the exact same
flow as the `triage` skill, differing only in scope: its inbox omits `--recent`, so
`rl triage --json` (no recency bound) covers every session still awaiting a decision,
not just the last few days. For everything else — the auto-prune step, per-project
selection, applying choices, harvesting, naming/review, and recording — follow the
`triage` skill's flow via `plugin/PLAYBOOK.md` unchanged; it is not repeated here.

This is the Claude wrapper: map `[SELECT]` to the `AskUserQuestion` tool; map `[FAN-OUT]`
to dispatching parallel subagents, one per finished session's extraction. Same mapping as
`triage` — see `plugin/PLAYBOOK.md` for the mechanics and fallbacks.

## Flow

Run the `triage` skill's flow (`plugin/skills/triage/SKILL.md`) end to end, with its Step
2 changed to build the inbox from `rl triage --json` — no `--recent` bound.

Because the backlog can be large, lean hard on `PLAYBOOK.md`'s strategic ordering: work
**project by project, in bounded chunks**, oldest activity first, and report the progress
tally after every project rather than trying to clear the whole backlog in one sitting.

**Resumable by design.** It's fine to stop partway through and pick this back up later —
`rl session mark cleaned <project>` and the per-session `reviewed`/`harvested` marks are
recorded in the registry as you go, so re-running `cleanup` continues from wherever the
last run left off instead of re-presenting sessions already triaged.
