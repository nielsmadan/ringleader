---
name: ringleader-finish
description: Finish the session you are in — harvest what it learned and mark it to be archived when you quit, so it never reaches triage. Use when the user says "finish this session", "I'm done here", "wrap this up", or invokes finish.
---

# Finish

Scope: **this session**. Every other ringleader skill curates sessions that are already
dormant; this one runs inside a live session and captures it while its context is still
here. That context is the whole point — you know things about this session that were
never written to the transcript, and a model reading it later will not.

This skill uses one mechanism your agent provides. `[SELECT]`: use your native
structured-choice tool (Claude Code: `AskUserQuestion`) to confirm; if you have none, ask
and read back the answer. Everything else — calling `rl`, the harvest sub-protocol — is in
`references/playbook.md`.

## Flow

1. **Identify the session.** Run `rl session whoami`. If it reports that this is not a
   recognised agent session, say so and stop — pass the id explicitly to the commands
   below only if the user supplies one.

2. **Confirm the work is finished.** `[SELECT]` between:
   1. **Finished — harvest and archive it.**
   2. **Not finished — remind me in N days.** Ask how many, then run
      `rl session snooze <id> --for <days>` and stop.

   Do not offer any other outcome. A session that is neither finished nor worth revisiting
   is a normal triage decision, not a finishing one.

3. **Write the harvest.** From this session's own context, not by re-reading the
   transcript, following `references/playbook.md` § Harvest sub-protocol for what counts as
   durable. Cover what was decided and why, what surprised you, and what changed. Omit
   routine steps. Then store it:

   `rl session harvest --current --from-file - <<'EOF' … EOF`

4. **Mark it.** Run `rl session finish --current`.

5. **Say what happens next.** The session leaves triage immediately. Its transcript is
   archived once it goes quiet — within seconds of quitting if session-end hooks are
   installed (`rl install hooks`), otherwise at the next `rl roundup` (or `rl session
   refresh`); `rl doctor` reports which applies. Either way it will not appear in triage in
   the meantime.
