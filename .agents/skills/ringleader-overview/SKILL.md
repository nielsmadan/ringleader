---
name: ringleader-overview
description: Morning read across dormant agent sessions — what's in flight, what's stale and awaiting triage, what's snoozed, and what's kept long-term. Use when the user asks for a "morning overview", "what's in flight", "what am I working on", "session overview", or invokes overview.
---

# Overview

This skill uses one mechanism your agent provides. `[SELECT]`: use your native
structured-choice tool (Claude Code: `AskUserQuestion`) to get a per-item decision; if you
have none, present a **numbered list** and read back the choices. This skill has no
fan-out step. Everything else — calling `rl`, JSON conventions, addressing a session by
its id field — is in `references/playbook.md`; this file only adds the flow and the
select mechanism.

Read-only except the snooze action in Step 5.

## Flow

1. **Refresh.** Run `rl overview`. It auto-scans disk and prints the project rollup — this
   is the baseline read for the rest of the flow.

2. **Stale-triage check.** Run `rl triage --json` (the inbox of sessions awaiting a
   decision — see `references/playbook.md`). Filter to entries whose age is over 24h. If
   any exist, surface the count to the user and `[SELECT]` offer to drop into the `triage`
   skill now versus continue the overview first.

3. **Recent open sessions.** Run `rl overview -s --recent 3 --json` to list open sessions
   active in the last 3 days. For each, summarize what it is and what was last worked on,
   using the JSON's classification and timestamp fields.

4. **Parked sessions.** Run `rl overview --snoozed` and list each with its wake time.
   Then run `rl overview --kept` for the ones kept long-term — those never return to
   triage on their own, so this view is the only place they surface.

5. **Next action.** `[SELECT]` let the user choose, per session (from Steps 3–4): pick it
   as next, snooze it (`rl session snooze <id> --for <days>`), or send it to the `triage`
   skill.
